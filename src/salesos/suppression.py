"""Central suppression and opt-out management for NexAgentLab SalesOS.

Provides global and per-lead suppression, email normalization, opt-out recording,
immediate pre-send checks, and automatic cancellation of queued messages for suppressed
recipients.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from .db import audit_log, transaction

logger = logging.getLogger(__name__)

ALLOWED_REASONS = {
    "opt_out",
    "unsubscribe",
    "bounce",
    "complaint",
    "manual",
    "invalid",
    "rejected",
}


def normalize_email(email: str | None) -> str:
    """Normalize an email address: strip whitespace and lowercase."""
    if not email:
        return ""
    return email.strip().lower()


def is_email_suppressed(
    conn: sqlite3.Connection,
    email: str | None,
) -> tuple[bool, Optional[str]]:
    """Check if an email address is currently suppressed.

    Returns:
        tuple[bool, str | None]: (is_suppressed, reason)
    """
    norm_email = normalize_email(email)
    if not norm_email:
        return True, "invalid"

    row = conn.execute(
        "SELECT reason FROM suppression WHERE lower(trim(email)) = ? LIMIT 1",
        (norm_email,),
    ).fetchone()

    if row:
        return True, row["reason"]
    return False, None


def record_suppression(
    conn: sqlite3.Connection,
    email: str,
    reason: str = "manual",
    source: str = "cli",
    original_lead_id: int | None = None,
) -> dict:
    """Add an email to the suppression list and cancel any pending messages.

    Args:
        conn: SQLite database connection.
        email: Email address to suppress.
        reason: One of allowed reasons ('opt_out', 'unsubscribe', 'bounce', etc.).
        source: Source of suppression ('cli', 'inbound_reply', 'bounce_detector', etc.).
        original_lead_id: Optional ID of the associated lead.

    Returns:
        dict: Result summary with counts of cancelled messages and status.
    """
    norm_email = normalize_email(email)
    if not norm_email:
        raise ValueError("Cannot suppress empty or invalid email address")

    if reason not in ALLOWED_REASONS:
        reason = "manual"

    with transaction(conn):
        # 1. Look up lead ID if not supplied
        if original_lead_id is None:
            lead_row = conn.execute(
                "SELECT id FROM leads WHERE lower(trim(email)) = ? LIMIT 1",
                (norm_email,),
            ).fetchone()
            if lead_row:
                original_lead_id = lead_row["id"]

        # 2. Insert or update suppression record
        existing = conn.execute(
            "SELECT id FROM suppression WHERE lower(trim(email)) = ?",
            (norm_email,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE suppression SET
                   reason = ?,
                   source = ?,
                   original_lead_id = coalesce(?, original_lead_id),
                   created_at = datetime('now')
                   WHERE id = ?""",
                (reason, source, original_lead_id, existing["id"]),
            )
            suppression_id = existing["id"]
            action = "suppression_updated"
        else:
            cur = conn.execute(
                """INSERT INTO suppression (email, reason, source, original_lead_id)
                   VALUES (?, ?, ?, ?)""",
                (norm_email, reason, source, original_lead_id),
            )
            suppression_id = cur.lastrowid
            action = "suppression_created"

        # 3. Update lead status if lead exists
        if original_lead_id is not None:
            conn.execute(
                "UPDATE leads SET status = 'suppressed', updated_at = datetime('now') WHERE id = ?",
                (original_lead_id,),
            )
        else:
            conn.execute(
                "UPDATE leads SET status = 'suppressed', updated_at = datetime('now') WHERE lower(trim(email)) = ?",
                (norm_email,),
            )

        # 4. Critical: Cancel any queued, draft, or approved messages for this recipient
        cancel_reason = f"Recipient suppressed ({reason}) via {source}"
        cancelled_rows = conn.execute(
            """UPDATE messages
               SET status = 'cancelled',
                   error_message = ?
               WHERE lower(trim(recipient)) = ?
                 AND status IN ('draft', 'queued', 'approved')""",
            (cancel_reason, norm_email),
        ).rowcount

        # 5. Record in audit log
        audit_log(
            conn,
            "suppression",
            suppression_id,
            action,
            json.dumps({
                "email": norm_email,
                "reason": reason,
                "source": source,
                "lead_id": original_lead_id,
                "cancelled_messages": cancelled_rows,
            }),
        )

    logger.info(
        "Recorded suppression for %s (reason: %s). Cancelled %d pending messages.",
        norm_email,
        reason,
        cancelled_rows,
    )

    return {
        "email": norm_email,
        "reason": reason,
        "source": source,
        "suppression_id": suppression_id,
        "lead_id": original_lead_id,
        "cancelled_messages": cancelled_rows,
    }


def remove_suppression(conn: sqlite3.Connection, email: str) -> bool:
    """Remove an email from the suppression list (admin un-suppress)."""
    norm_email = normalize_email(email)
    if not norm_email:
        return False

    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM suppression WHERE lower(trim(email)) = ?",
            (norm_email,),
        ).fetchone()
        if not existing:
            return False

        conn.execute("DELETE FROM suppression WHERE id = ?", (existing["id"],))

        audit_log(
            conn,
            "suppression",
            existing["id"],
            "suppression_removed",
            json.dumps({"email": norm_email}),
        )

    return True


def list_suppressions(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Retrieve recent suppression records."""
    return conn.execute(
        """SELECT id, email, reason, source, original_lead_id, created_at
           FROM suppression
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def get_suppression_stats(conn: sqlite3.Connection) -> dict:
    """Return counts of suppressions broken down by reason."""
    total = conn.execute("SELECT count(*) FROM suppression").fetchone()[0]
    by_reason = {}
    for row in conn.execute("SELECT reason, count(*) as cnt FROM suppression GROUP BY reason"):
        by_reason[row["reason"]] = row["cnt"]

    return {
        "total_suppressed": total,
        "by_reason": by_reason,
    }
