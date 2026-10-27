"""Controlled Outreach Orchestrator & State Machine for NexAgentLab SalesOS.

Enforces strict pre-send compliance, message approval workflows, draft synchronization
to Gmail, duplicate-send prevention, rate limiting, daily caps, kill-switch monitoring,
and audit logging.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

import requests

from .db import audit_log, transaction
from .gmail_client import (
    AmbiguousSendError,
    GmailError,
    OAuthMissingError,
    SenderMismatchError,
    create_or_update_draft,
    get_configured_sender,
    get_gmail_status,
    is_dry_run,
    is_kill_switch_active,
    send_message_api,
    verify_sender_identity,
)
from .suppression import is_email_suppressed, normalize_email

logger = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT = 20
DEFAULT_SEND_DELAY_SECONDS = 2.0


# ── Outreach Exceptions ──────────────────────────────────────────────


class OutreachError(Exception):
    """Base exception for outreach operations."""


class KillSwitchActiveError(OutreachError):
    """Raised when outreach is blocked by the emergency kill switch."""


class DailyCapExceededError(OutreachError):
    """Raised when the daily send quota is exceeded."""


class PreflightCheckFailedError(OutreachError):
    """Raised when message fails safety or approval checks before sending."""


class DuplicateRecipientError(OutreachError):
    """Raised when attempting to send outreach to a recipient who was already emailed."""


# ── Helper Functions ─────────────────────────────────────────────────


def get_daily_limit() -> int:
    """Get configured daily send limit from environment."""
    val = os.environ.get("GMAIL_DAILY_LIMIT") or os.environ.get("CAMPAIGN_MAX_DAILY_SENDS")
    if val:
        try:
            return max(1, int(val))
        except ValueError:
            pass
    return DEFAULT_DAILY_LIMIT


def get_daily_sends_today(conn: sqlite3.Connection) -> int:
    """Count messages sent today."""
    row = conn.execute(
        """SELECT count(*) FROM messages
           WHERE status = 'sent'
             AND date(sent_at) = date('now')"""
    ).fetchone()
    return row[0] if row else 0


def has_recipient_been_sent(conn: sqlite3.Connection, email: str) -> bool:
    """Check if any message has already been successfully sent to this recipient."""
    norm = normalize_email(email)
    row = conn.execute(
        """SELECT 1 FROM messages
           WHERE lower(trim(recipient)) = ?
             AND status = 'sent'
           LIMIT 1""",
        (norm,),
    ).fetchone()
    return bool(row)


def generate_idempotency_key(message_id: int, recipient: str, subject: str) -> str:
    """Generate a reproducible idempotency hash for an outreach dispatch."""
    raw = f"{message_id}:{recipient.strip().lower()}:{subject.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# ── Message State Transitions ────────────────────────────────────────


def approve_message(
    conn: sqlite3.Connection,
    message_id: int,
    force: bool = False,
) -> tuple[bool, str]:
    """Approve a draft message for outbound sending.

    Rules:
    - Message must exist and be in 'draft' or 'rejected' (if force) status.
    - Message Quality Gate must be 'approved' (unless force=True).
    - Recipient must not be suppressed.
    """
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        return False, f"Message ID {message_id} not found."

    current_status = row["status"]
    if current_status == "sent":
        return False, f"Message {message_id} has already been sent."
    if current_status == "approved":
        return True, f"Message {message_id} is already approved."

    recipient = row["recipient"] or ""
    suppressed, reason = is_email_suppressed(conn, recipient)
    if suppressed:
        return False, f"Cannot approve: recipient {recipient} is suppressed ({reason})."

    quality_status = row["quality_status"] or "pending"
    if quality_status != "approved" and not force:
        val_errors = row["validation_errors"] or "Quality Gate has not approved this draft"
        return False, f"Cannot approve: {val_errors}. Use --force to override manual review."

    with transaction(conn):
        conn.execute(
            """UPDATE messages SET
               status = 'approved',
               approved_at = datetime('now'),
               error_message = NULL
               WHERE id = ?""",
            (message_id,),
        )
        audit_log(
            conn,
            "message",
            message_id,
            "message_approved",
            json.dumps({"recipient": recipient, "forced": force}),
        )

    return True, f"Message {message_id} approved for outreach."


def queue_messages(
    conn: sqlite3.Connection,
    limit: int | None = None,
    service: str | None = None,
) -> int:
    """Transition approved messages into the outbound dispatch queue."""
    query = "SELECT id, recipient FROM messages WHERE status = 'approved'"
    params: list = []

    if service:
        query += " AND selected_service = ?"
        params.append(service)

    query += " ORDER BY qualification_score DESC, created_at ASC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()
    queued_count = 0

    with transaction(conn):
        for r in rows:
            mid = r["id"]
            recip = r["recipient"]

            # Immediate suppression check before queueing
            suppressed, reason = is_email_suppressed(conn, recip)
            if suppressed:
                conn.execute(
                    "UPDATE messages SET status = 'cancelled', error_message = ? WHERE id = ?",
                    (f"Recipient suppressed ({reason})", mid),
                )
                continue

            conn.execute("UPDATE messages SET status = 'queued' WHERE id = ?", (mid,))
            queued_count += 1

        if queued_count > 0:
            audit_log(
                conn,
                "outreach",
                None,
                "messages_queued",
                json.dumps({"queued_count": queued_count}),
            )

    return queued_count


def preview_message(conn: sqlite3.Connection, message_id: int) -> dict:
    """Inspect complete draft details and evaluate pre-flight safety checks."""
    row = conn.execute(
        """SELECT m.*, l.business_name, l.contact_name, l.website, l.location
           FROM messages m
           JOIN leads l ON m.lead_id = l.id
           WHERE m.id = ?""",
        (message_id,),
    ).fetchone()

    if not row:
        raise OutreachError(f"Message ID {message_id} not found.")

    recip = row["recipient"] or ""
    suppressed, supp_reason = is_email_suppressed(conn, recip)
    already_sent = has_recipient_been_sent(conn, recip)
    daily_sends = get_daily_sends_today(conn)
    daily_cap = get_daily_limit()
    kill_switch = is_kill_switch_active()
    dry_run = is_dry_run()

    # Safety readiness checklist
    checks = {
        "is_approved": row["status"] in ("approved", "queued"),
        "quality_gate_passed": row["quality_status"] == "approved",
        "recipient_not_suppressed": not suppressed,
        "no_duplicate_sent": not already_sent or row["status"] == "sent",
        "daily_quota_available": daily_sends < daily_cap,
        "kill_switch_inactive": not kill_switch,
        "dry_run_mode": dry_run,
    }
    all_ready = (
        checks["is_approved"]
        and checks["quality_gate_passed"]
        and checks["recipient_not_suppressed"]
        and checks["no_duplicate_sent"]
        and checks["daily_quota_available"]
        and checks["kill_switch_inactive"]
    )

    ev_refs = []
    if row["evidence_references"]:
        try:
            ev_refs = json.loads(row["evidence_references"])
        except Exception:
            ev_refs = [row["evidence_references"]]

    return {
        "message_id": row["id"],
        "lead_id": row["lead_id"],
        "business_name": row["business_name"],
        "contact_name": row["contact_name"],
        "website": row["website"],
        "location": row["location"],
        "recipient": recip,
        "subject": row["subject"],
        "body": row["body"],
        "selected_service": row["selected_service"],
        "qualification_score": row["qualification_score"],
        "status": row["status"],
        "quality_status": row["quality_status"],
        "evidence_references": ev_refs,
        "gmail_draft_id": row["gmail_draft_id"],
        "gmail_message_id": row["gmail_message_id"],
        "created_at": row["created_at"],
        "approved_at": row["approved_at"],
        "sent_at": row["sent_at"],
        "checks": checks,
        "ready_to_send": all_ready,
        "suppression_reason": supp_reason,
        "daily_sends_today": daily_sends,
        "daily_cap": daily_cap,
    }


# ── Draft Synchronization to Gmail ───────────────────────────────────


def sync_drafts_to_gmail(
    conn: sqlite3.Connection,
    message_ids: list[int] | None = None,
    dry_run: bool | None = None,
) -> dict:
    """Sync approved/queued email drafts into the Gmail Drafts folder.

    Allows visual inspection directly within the Gmail web interface without
    transmitting live emails.
    """
    if dry_run is None:
        dry_run = is_dry_run()

    if message_ids:
        placeholders = ",".join("?" for _ in message_ids)
        rows = conn.execute(
            f"SELECT * FROM messages WHERE id IN ({placeholders})",
            message_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM messages WHERE status IN ('approved', 'queued')"
        ).fetchall()

    synced = 0
    skipped = 0
    errors = []

    for r in rows:
        mid = r["id"]
        recip = r["recipient"]
        subj = r["subject"]
        body = r["body"]
        draft_id = r["gmail_draft_id"]

        # Suppression check
        suppressed, reason = is_email_suppressed(conn, recip)
        if suppressed:
            with transaction(conn):
                conn.execute(
                    "UPDATE messages SET status = 'cancelled', error_message = ? WHERE id = ?",
                    (f"Recipient suppressed ({reason})", mid),
                )
            skipped += 1
            continue

        if dry_run:
            # Generate deterministic dry-run draft ID
            mock_draft_id = draft_id or f"dry_draft_{mid}_{int(time.time())}"
            with transaction(conn):
                conn.execute(
                    "UPDATE messages SET gmail_draft_id = ? WHERE id = ?",
                    (mock_draft_id, mid),
                )
            synced += 1
            continue

        # Live sync to Gmail API
        try:
            res = create_or_update_draft(
                to_email=recip,
                subject=subj,
                body=body,
                draft_id=draft_id,
            )
            new_draft_id = res.get("draft_id")
            with transaction(conn):
                conn.execute(
                    "UPDATE messages SET gmail_draft_id = ? WHERE id = ?",
                    (new_draft_id, mid),
                )
                audit_log(
                    conn,
                    "message",
                    mid,
                    "gmail_draft_synced",
                    json.dumps({"draft_id": new_draft_id, "recipient": recip}),
                )
            synced += 1
        except Exception as e:
            logger.error("Failed to sync draft for message %d: %s", mid, e)
            errors.append(f"Message {mid}: {e}")

    return {
        "total_evaluated": len(rows),
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
    }


# ── Outreach Dispatch ────────────────────────────────────────────────


def send_outreach_message(
    conn: sqlite3.Connection,
    message_id: int,
    dry_run: bool | None = None,
    force: bool = False,
) -> dict:
    """Safely dispatch an approved outreach message via Gmail.

    Enforces all pre-flight compliance, suppression, daily cap, and duplicate checks.
    """
    if dry_run is None:
        dry_run = is_dry_run()

    # 1. Check emergency kill switch
    if is_kill_switch_active():
        raise KillSwitchActiveError(
            "Campaign kill switch is active (CAMPAIGN_KILL_SWITCH=true). All outbound sending is halted."
        )

    # 2. Load message
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if not row:
        raise OutreachError(f"Message ID {message_id} not found.")

    recip = row["recipient"] or ""
    current_status = row["status"]
    draft_id = row["gmail_draft_id"]

    # 3. State validations
    if current_status == "sent":
        raise DuplicateRecipientError(
            f"Message {message_id} has already been sent at {row['sent_at']}."
        )
    if current_status == "cancelled":
        raise PreflightCheckFailedError(
            f"Message {message_id} is cancelled: {row['error_message']}."
        )
    if current_status not in ("approved", "queued") and not force:
        raise PreflightCheckFailedError(
            f"Message {message_id} is in '{current_status}' status. "
            f"Outreach messages must be approved before sending. Run 'salesos outreach approve {message_id}'."
        )

    # 4. Quality Gate validation
    if row["quality_status"] != "approved" and not force:
        raise PreflightCheckFailedError(
            f"Message {message_id} failed Quality Gate ({row['quality_status']}): {row['validation_errors']}"
        )

    # 5. Immediate suppression check
    suppressed, supp_reason = is_email_suppressed(conn, recip)
    if suppressed:
        with transaction(conn):
            conn.execute(
                "UPDATE messages SET status = 'cancelled', error_message = ? WHERE id = ?",
                (f"Recipient suppressed ({supp_reason})", message_id),
            )
        raise PreflightCheckFailedError(
            f"Cannot send to {recip}: email is on suppression list ({supp_reason})."
        )

    # 6. Duplicate-recipient check across all prior messages
    if has_recipient_been_sent(conn, recip) and not force:
        with transaction(conn):
            conn.execute(
                "UPDATE messages SET status = 'cancelled', error_message = 'Duplicate recipient already contacted' WHERE id = ?",
                (message_id,),
            )
        raise DuplicateRecipientError(
            f"Outreach was already sent to recipient '{recip}'. Duplicate sending is blocked."
        )

    # 7. Daily send quota check
    daily_sends = get_daily_sends_today(conn)
    daily_cap = get_daily_limit()
    if daily_sends >= daily_cap and not force:
        raise DailyCapExceededError(
            f"Daily send limit of {daily_cap} reached for today ({daily_sends} already sent). "
            f"Wait until tomorrow or adjust GMAIL_DAILY_LIMIT."
        )

    # 8. Idempotency key
    idempotency_key = generate_idempotency_key(message_id, recip, row["subject"])

    # ── Dispatch Execution ───────────────────────────────────────────
    if dry_run:
        simulated_msg_id = f"dry_send_{message_id}_{int(time.time())}"
        with transaction(conn):
            conn.execute(
                """UPDATE messages SET
                   status = 'sent',
                   sent_at = datetime('now'),
                   gmail_message_id = ?,
                   idempotency_key = ?,
                   error_message = NULL
                   WHERE id = ?""",
                (simulated_msg_id, idempotency_key, message_id),
            )
            conn.execute(
                "UPDATE leads SET status = 'contacted', updated_at = datetime('now') WHERE id = ?",
                (row["lead_id"],),
            )
            audit_log(
                conn,
                "outreach",
                message_id,
                "dry_run_send",
                json.dumps({
                    "recipient": recip,
                    "simulated_message_id": simulated_msg_id,
                    "daily_count": daily_sends + 1,
                }),
            )

        logger.info("[DRY RUN] Simulated outreach dispatch to %s (ID %d)", recip, message_id)
        return {
            "message_id": message_id,
            "recipient": recip,
            "status": "sent",
            "dry_run": True,
            "gmail_message_id": simulated_msg_id,
            "daily_sends_today": daily_sends + 1,
        }

    # Live Gmail Dispatch
    verify_sender_identity()

    try:
        res = send_message_api(
            to_email=recip,
            subject=row["subject"],
            body=row["body"],
            draft_id=draft_id,
        )
        gmail_mid = res.get("message_id")
        gmail_tid = res.get("thread_id")

        with transaction(conn):
            conn.execute(
                """UPDATE messages SET
                   status = 'sent',
                   sent_at = datetime('now'),
                   gmail_message_id = ?,
                   gmail_thread_id = ?,
                   idempotency_key = ?,
                   error_message = NULL
                   WHERE id = ?""",
                (gmail_mid, gmail_tid, idempotency_key, message_id),
            )
            conn.execute(
                "UPDATE leads SET status = 'contacted', updated_at = datetime('now') WHERE id = ?",
                (row["lead_id"],),
            )
            audit_log(
                conn,
                "outreach",
                message_id,
                "live_message_sent",
                json.dumps({
                    "recipient": recip,
                    "gmail_message_id": gmail_mid,
                    "thread_id": gmail_tid,
                    "daily_count": daily_sends + 1,
                }),
            )

        logger.info("Dispatched live outreach email to %s (Gmail ID %s)", recip, gmail_mid)
        return {
            "message_id": message_id,
            "recipient": recip,
            "status": "sent",
            "dry_run": False,
            "gmail_message_id": gmail_mid,
            "thread_id": gmail_tid,
            "daily_sends_today": daily_sends + 1,
        }

    except (AmbiguousSendError, requests.Timeout, requests.ConnectionError) as exc:
        ambiguous_err = exc if isinstance(exc, AmbiguousSendError) else AmbiguousSendError(f"Ambiguous send outcome: {exc}")
        with transaction(conn):
            conn.execute(
                """UPDATE messages SET
                   status = 'failed',
                   error_message = ?
                   WHERE id = ?""",
                (str(ambiguous_err), message_id),
            )
            audit_log(
                conn,
                "outreach",
                message_id,
                "ambiguous_send_outcome",
                json.dumps({"error": str(ambiguous_err), "recipient": recip}),
            )
        raise ambiguous_err

    except Exception as exc:
        with transaction(conn):
            conn.execute(
                """UPDATE messages SET
                   status = 'failed',
                   error_message = ?
                   WHERE id = ?""",
                (str(exc), message_id),
            )
            audit_log(
                conn,
                "outreach",
                message_id,
                "send_failed",
                json.dumps({"error": str(exc), "recipient": recip}),
            )
        raise OutreachError(f"Failed to send email to {recip}: {exc}") from exc


def send_batch(
    conn: sqlite3.Connection,
    limit: int | None = None,
    dry_run: bool | None = None,
    delay_seconds: float = DEFAULT_SEND_DELAY_SECONDS,
) -> dict:
    """Send queued outreach messages in controlled batch with rate-limiting."""
    if dry_run is None:
        dry_run = is_dry_run()

    query = "SELECT id FROM messages WHERE status = 'queued' ORDER BY qualification_score DESC"
    if limit:
        query += f" LIMIT {int(limit)}"

    rows = conn.execute(query).fetchall()
    sent_count = 0
    skipped_count = 0
    errors = []

    for idx, r in enumerate(rows):
        mid = r["id"]
        try:
            send_outreach_message(conn, mid, dry_run=dry_run)
            sent_count += 1
            # Delay between messages to avoid provider throttling
            if delay_seconds > 0 and idx < len(rows) - 1:
                time.sleep(delay_seconds)
        except (DailyCapExceededError, KillSwitchActiveError) as e:
            logger.warning("Batch sending stopped early: %s", e)
            errors.append(str(e))
            break
        except Exception as e:
            logger.error("Failed sending message %d in batch: %s", mid, e)
            errors.append(f"Message {mid}: {e}")
            skipped_count += 1

    return {
        "total_queued": len(rows),
        "sent": sent_count,
        "skipped": skipped_count,
        "errors": errors,
        "dry_run": dry_run,
    }


def get_outreach_status(conn: sqlite3.Connection) -> dict:
    """Gather aggregate outreach statistics and system safety state."""
    counts_by_status = {}
    for r in conn.execute("SELECT status, count(*) as cnt FROM messages GROUP BY status"):
        counts_by_status[r["status"]] = r["cnt"]

    daily_sends = get_daily_sends_today(conn)
    daily_cap = get_daily_limit()
    suppression_count = conn.execute("SELECT count(*) FROM suppression").fetchone()[0]

    return {
        "messages_by_status": counts_by_status,
        "daily_sends_today": daily_sends,
        "daily_cap": daily_cap,
        "quota_remaining": max(0, daily_cap - daily_sends),
        "dry_run": is_dry_run(),
        "kill_switch_active": is_kill_switch_active(),
        "total_suppressed": suppression_count,
        "configured_sender": get_configured_sender(),
    }
