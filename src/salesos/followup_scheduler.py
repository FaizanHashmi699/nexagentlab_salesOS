"""Follow-Up Scheduling & Business Day Engine for NexAgentLab SalesOS.

Implements persistent, multi-step follow-up automation for non-responsive leads.
Accounts for business days (skips weekends), checks replies and suppression
before any action, prevents duplicate scheduling, and generates reviewable drafts
by default without automated live sending.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from .db import audit_log, transaction
from .personalizer import generate_template_email, validate_email_draft
from .suppression import is_email_suppressed, normalize_email

logger = logging.getLogger(__name__)

DEFAULT_STEP1_DAYS = 3
DEFAULT_STEP2_DAYS = 7
DEFAULT_MAX_STEPS = 2


# ── Business Day Calculation ─────────────────────────────────────────


def add_business_days(start_date: datetime, n_days: int) -> datetime:
    """Calculate future datetime by adding n business days (Monday-Friday)."""
    current = start_date
    added = 0
    while added < n_days:
        current += timedelta(days=1)
        # Saturday is 5, Sunday is 6
        if current.weekday() < 5:
            added += 1
    return current


def get_step_delay(step_number: int) -> int:
    """Get configurable business day delay for a given follow-up step."""
    if step_number == 1:
        val = os.environ.get("FOLLOWUP_STEP1_DAYS")
        return int(val) if val and val.isdigit() else DEFAULT_STEP1_DAYS
    elif step_number == 2:
        val = os.environ.get("FOLLOWUP_STEP2_DAYS")
        return int(val) if val and val.isdigit() else DEFAULT_STEP2_DAYS
    return DEFAULT_STEP2_DAYS


def get_max_followup_steps() -> int:
    """Get maximum allowed follow-up steps per lead."""
    val = os.environ.get("FOLLOWUP_MAX_STEPS")
    return int(val) if val and val.isdigit() else DEFAULT_MAX_STEPS


# ── Follow-Up Planning ───────────────────────────────────────────────


def plan_followups(
    conn: sqlite3.Connection,
    max_leads: int = 50,
) -> dict:
    """Scan contacted leads and plan next follow-up steps for non-responders."""
    max_steps = get_max_followup_steps()

    # Find candidate leads who have had messages sent
    rows = conn.execute(
        """SELECT m.id as message_id, m.lead_id, m.recipient, m.sent_at, m.subject,
                  m.selected_service, l.business_name, l.contact_name, l.status as lead_status
           FROM messages m
           JOIN leads l ON m.lead_id = l.id
           WHERE m.status = 'sent'
             AND m.sent_at IS NOT NULL
             AND l.status NOT IN ('suppressed', 'replied', 'interested', 'not_interested')
           ORDER BY m.sent_at ASC
           LIMIT ?""",
        (max_leads,),
    ).fetchall()

    planned_count = 0
    skipped_count = 0
    cancelled_count = 0

    for r in rows:
        lead_id = r["lead_id"]
        recipient = r["recipient"]

        # 1. Stop Condition: Suppression / Opt-out check
        suppressed, reason = is_email_suppressed(conn, recipient)
        if suppressed:
            cancel_lead_followups(conn, lead_id, f"Lead suppressed ({reason})")
            skipped_count += 1
            continue

        # 2. Stop Condition: Did lead reply?
        reply_row = conn.execute(
            "SELECT 1 FROM replies WHERE lead_id = ? LIMIT 1",
            (lead_id,),
        ).fetchone()
        if reply_row:
            cancel_lead_followups(conn, lead_id, "Lead has already replied")
            skipped_count += 1
            continue

        # 3. Determine next step number
        last_followup = conn.execute(
            """SELECT followup_number, status, due_date
               FROM followups
               WHERE lead_id = ?
               ORDER BY followup_number DESC LIMIT 1""",
            (lead_id,),
        ).fetchone()

        if not last_followup:
            next_step = 1
            base_time_str = r["sent_at"]
        else:
            last_num = last_followup["followup_number"]
            last_status = last_followup["status"]

            if last_num >= max_steps:
                # Sequence exhausted
                continue

            if last_status != "sent":
                # Previous step has not been sent yet
                continue

            next_step = last_num + 1
            base_time_str = last_followup["due_date"]

        # 4. Calculate due date
        delay_days = get_step_delay(next_step)
        try:
            base_dt = datetime.fromisoformat(base_time_str.replace("Z", "+00:00"))
        except Exception:
            base_dt = datetime.now(timezone.utc)

        due_dt = add_business_days(base_dt, delay_days)
        due_date_iso = due_dt.strftime("%Y-%m-%d %H:%M:%S")

        reason_text = (
            f"Step {next_step} follow-up scheduled {delay_days} business days "
            f"after {'initial outreach' if next_step == 1 else f'step {next_step - 1}'}."
        )

        with transaction(conn):
            # Check duplicate schedule prevention
            existing_step = conn.execute(
                "SELECT id FROM followups WHERE lead_id = ? AND followup_number = ?",
                (lead_id, next_step),
            ).fetchone()

            if not existing_step:
                cur = conn.execute(
                    """INSERT INTO followups (
                       lead_id, original_message_id, followup_number,
                       due_date, status, scheduled_reason
                    ) VALUES (?, ?, ?, ?, 'scheduled', ?)""",
                    (lead_id, r["message_id"], next_step, due_date_iso, reason_text),
                )
                followup_id = cur.lastrowid

                # Generate the draft message for review immediately
                generate_followup_draft(conn, followup_id)

                audit_log(
                    conn,
                    "followup",
                    followup_id,
                    "followup_scheduled",
                    json.dumps({
                        "lead_id": lead_id,
                        "step": next_step,
                        "due_date": due_date_iso,
                    }),
                )
                planned_count += 1

    return {
        "leads_evaluated": len(rows),
        "planned_count": planned_count,
        "skipped_count": skipped_count,
    }


# ── Draft Generation for Follow-Ups ──────────────────────────────────


def generate_followup_draft(conn: sqlite3.Connection, followup_id: int) -> Optional[int]:
    """Create a grounded follow-up email draft in messages for human review."""
    row = conn.execute(
        """SELECT f.*, m.recipient, m.subject as orig_subject, m.selected_service,
                  l.business_name, l.contact_name, l.website
           FROM followups f
           JOIN messages m ON f.original_message_id = m.id
           JOIN leads l ON f.lead_id = l.id
           WHERE f.id = ?""",
        (followup_id,),
    ).fetchone()

    if not row:
        return None

    # If draft already created, return its ID
    if row["generated_message_id"]:
        return row["generated_message_id"]

    biz_name = row["business_name"] or "your business"
    contact_name = row["contact_name"]
    step = row["followup_number"]
    orig_subject = row["orig_subject"] or f"Modernization for {biz_name}"

    # Subject line convention
    subject = f"Re: {orig_subject}" if not orig_subject.startswith("Re:") else orig_subject

    # Grounded, respectful follow-up body
    greeting = f"Hi {contact_name}," if contact_name and contact_name.strip() else f"Hi {biz_name} Team,"

    if step == 1:
        body = (
            f"{greeting}\n\n"
            f"I wanted to follow up briefly on my earlier note regarding digital modernization for {biz_name}.\n\n"
            f"We recently worked with similar service businesses to streamline their online booking and "
            f"customer capture workflows, eliminating the routine back-and-forth communication.\n\n"
            f"Would you be open to a 10-minute introductory conversation this week?\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"nexagentlab.tech@gmail.com\n\n"
            f"To opt out of future emails, reply 'unsubscribe' or 'opt-out'."
        )
    else:
        body = (
            f"{greeting}\n\n"
            f"Following up one last time regarding {biz_name}'s website inquiries and automated workflows.\n\n"
            f"If this isn't a current priority for your team, no problem at all — I won't follow up again.\n\n"
            f"Should you ever want to explore practical AI assistants or automated scheduling in the future, "
            f"feel free to reach out anytime.\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"nexagentlab.tech@gmail.com\n\n"
            f"To opt out of future emails, reply 'unsubscribe' or 'opt-out'."
        )

    # Persist draft into messages
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO messages (
               lead_id, recipient, subject, body, message_type,
               status, quality_status, selected_service, provider
            ) VALUES (?, ?, ?, ?, 'follow_up', 'draft', 'approved', ?, 'template')""",
            (row["lead_id"], row["recipient"], subject, body, row["selected_service"]),
        )
        msg_id = cur.lastrowid

        conn.execute(
            """UPDATE followups SET
               generated_message_id = ?,
               status = 'planned',
               updated_at = datetime('now')
               WHERE id = ?""",
            (msg_id, followup_id),
        )

    return msg_id


# ── Follow-Up Queue & Review Operations ──────────────────────────────


def get_due_followups(
    conn: sqlite3.Connection,
    as_of_date: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Retrieve follow-ups whose due date has arrived and re-verify stop conditions."""
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """SELECT f.*, m.recipient, m.status as msg_status, l.business_name, l.contact_name
           FROM followups f
           JOIN messages m ON f.generated_message_id = m.id
           JOIN leads l ON f.lead_id = l.id
           WHERE f.status IN ('scheduled', 'planned')
             AND f.due_date <= ?
           ORDER BY f.due_date ASC""",
        (as_of_date,),
    ).fetchall()

    valid_due = []
    for r in rows:
        lead_id = r["lead_id"]
        recip = r["recipient"]

        # Pre-check stop condition 1: Suppression
        suppressed, reason = is_email_suppressed(conn, recip)
        if suppressed:
            cancel_lead_followups(conn, lead_id, f"Suppressed ({reason})")
            continue

        # Pre-check stop condition 2: Reply arrived
        reply = conn.execute("SELECT 1 FROM replies WHERE lead_id = ? LIMIT 1", (lead_id,)).fetchone()
        if reply:
            cancel_lead_followups(conn, lead_id, "Lead replied")
            continue

        valid_due.append(r)

    return valid_due


def approve_followup(conn: sqlite3.Connection, followup_id: int) -> tuple[bool, str]:
    """Approve a scheduled follow-up draft for outreach sending."""
    row = conn.execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
    if not row:
        return False, f"Follow-up ID {followup_id} not found."

    if row["status"] == "cancelled":
        return False, f"Cannot approve cancelled follow-up: {row['cancel_reason']}"
    if row["status"] == "sent":
        return False, f"Follow-up {followup_id} has already been sent."

    msg_id = row["generated_message_id"]
    if not msg_id:
        msg_id = generate_followup_draft(conn, followup_id)

    with transaction(conn):
        conn.execute("UPDATE messages SET status = 'approved', approved_at = datetime('now') WHERE id = ?", (msg_id,))
        conn.execute("UPDATE followups SET status = 'approved', updated_at = datetime('now') WHERE id = ?", (followup_id,))
        audit_log(conn, "followup", followup_id, "followup_approved", json.dumps({"message_id": msg_id}))

    return True, f"Follow-up {followup_id} (Message {msg_id}) approved for dispatch."


def cancel_followup(
    conn: sqlite3.Connection,
    followup_id: int,
    reason: str = "manual_cancel",
) -> bool:
    """Cancel a scheduled or planned follow-up task."""
    row = conn.execute("SELECT * FROM followups WHERE id = ?", (followup_id,)).fetchone()
    if not row:
        return False

    with transaction(conn):
        conn.execute(
            """UPDATE followups SET
               status = 'cancelled',
               cancel_reason = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (reason, followup_id),
        )

        # Cancel linked message draft if not already sent
        if row["generated_message_id"]:
            conn.execute(
                """UPDATE messages SET
                   status = 'cancelled',
                   error_message = ?
                   WHERE id = ? AND status IN ('draft', 'approved', 'queued')""",
                (f"Follow-up cancelled: {reason}", row["generated_message_id"]),
            )

        audit_log(conn, "followup", followup_id, "followup_cancelled", json.dumps({"reason": reason}))

    return True


def cancel_lead_followups(
    conn: sqlite3.Connection,
    lead_id: int,
    reason: str,
) -> int:
    """Cancel all pending follow-ups for a specific lead."""
    rows = conn.execute(
        """SELECT id, generated_message_id FROM followups
           WHERE lead_id = ? AND status IN ('scheduled', 'planned', 'approved')""",
        (lead_id,),
    ).fetchall()

    if not rows:
        return 0

    with transaction(conn):
        for r in rows:
            conn.execute(
                """UPDATE followups SET
                   status = 'cancelled',
                   cancel_reason = ?,
                   updated_at = datetime('now')
                   WHERE id = ?""",
                (reason, r["id"]),
            )
            if r["generated_message_id"]:
                conn.execute(
                    """UPDATE messages SET
                       status = 'cancelled',
                       error_message = ?
                       WHERE id = ? AND status IN ('draft', 'approved', 'queued')""",
                    (f"Follow-up stopped: {reason}", r["generated_message_id"]),
                )

        audit_log(
            conn,
            "lead",
            lead_id,
            "all_followups_cancelled",
            json.dumps({"reason": reason, "cancelled_count": len(rows)}),
        )

    return len(rows)


def get_followups_status(conn: sqlite3.Connection) -> dict:
    """Aggregate statistics for follow-up campaign engine."""
    by_status = {}
    for r in conn.execute("SELECT status, count(*) as cnt FROM followups GROUP BY status"):
        by_status[r["status"]] = r["cnt"]

    due_count = len(get_due_followups(conn))

    return {
        "followups_by_status": by_status,
        "due_for_review": due_count,
        "step1_delay_days": get_step_delay(1),
        "step2_delay_days": get_step_delay(2),
        "max_steps": get_max_followup_steps(),
    }
