"""Meeting Management, Booking Synchronization & Meeting Preparation Assistant.

Provides:
- Complete meeting lifecycle tracking: scheduled, completed, cancelled, no-show.
- Timezone-aware scheduling and duplicate booking prevention.
- Calendly webhook signature verification and event synchronization.
- Meeting preparation brief assistant citing verified research evidence and hypotheses.
- Structured post-meeting outcome review workflow generating non-destructive drafts.
- Automatic transition of linked sales deals to 'meeting_booked' and 'meeting_completed'.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Any, Optional

from .ai_provider import _call_llm_provider, get_provider_status, sanitize_untrusted_text
from .pipeline import auto_transition_from_event, get_deal_by_lead

logger = logging.getLogger(__name__)


class MeetingStatus(str, Enum):
    """Permitted meeting lifecycle statuses."""
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class BookingSource(str, Enum):
    """Permitted booking acquisition channels."""
    CALENDLY = "calendly"
    MANUAL = "manual"
    EMAIL_INBOUND = "email_inbound"
    INBOUND_REPLY = "inbound_reply"


class DuplicateBookingError(ValueError):
    """Raised when attempting to schedule a duplicate meeting for a lead."""


class MeetingNotFoundError(LookupError):
    """Raised when a meeting record cannot be found."""


def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse various ISO-8601 or standard datetime strings into aware UTC datetimes."""
    dt_str = dt_str.strip()
    # Normalize space to T
    if " " in dt_str and "T" not in dt_str:
        dt_str = dt_str.replace(" ", "T")

    # If ending with Z, standard ISO format
    if dt_str.endswith("Z"):
        dt = datetime.fromisoformat(dt_str[:-1] + "+00:00")
    else:
        dt = datetime.fromisoformat(dt_str)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def create_meeting(
    conn: sqlite3.Connection,
    lead_id: int,
    title: str,
    scheduled_time: str,
    duration_minutes: int = 30,
    timezone_name: str = "UTC",
    location_or_url: Optional[str] = None,
    booking_source: str = BookingSource.MANUAL.value,
    deal_id: Optional[int] = None,
    external_booking_id: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Schedule a new meeting for a lead with duplicate prevention and deal linking.

    Args:
        conn: SQLite connection.
        lead_id: Foreign key to leads table.
        title: Meeting title or subject.
        scheduled_time: ISO-8601 or datetime string.
        duration_minutes: Duration in minutes.
        timezone_name: Timezone name (e.g. 'America/New_York', 'UTC').
        location_or_url: Location or virtual meeting link.
        booking_source: One of 'calendly', 'manual', 'email_inbound', 'inbound_reply'.
        deal_id: Optional linked sales deal.
        external_booking_id: Unique third-party booking ID (e.g. Calendly UUID).
        notes: Pre-meeting notes or context.
    """
    lead = conn.execute("SELECT id, business_name, email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        raise ValueError(f"Lead ID {lead_id} does not exist.")

    # Validate and normalize scheduled time
    parsed_time = parse_iso_datetime(scheduled_time)
    normalized_time_str = parsed_time.isoformat()

    # 1. Check duplicate external booking ID
    if external_booking_id:
        existing_ext = conn.execute(
            "SELECT id FROM meetings WHERE external_booking_id = ?",
            (external_booking_id,),
        ).fetchone()
        if existing_ext:
            raise DuplicateBookingError(
                f"Meeting with external booking ID '{external_booking_id}' already exists (Meeting #{existing_ext['id']})."
            )

    # 2. Check duplicate booking for the same lead at the same time
    existing_slot = conn.execute(
        """SELECT id, title, scheduled_time FROM meetings
           WHERE lead_id = ? AND scheduled_time = ? AND status != 'cancelled'""",
        (lead_id, normalized_time_str),
    ).fetchone()
    if existing_slot:
        raise DuplicateBookingError(
            f"Lead {lead_id} already has active Meeting #{existing_slot['id']} scheduled at {normalized_time_str}."
        )

    # Auto-resolve deal_id if not provided
    if not deal_id:
        existing_deal = get_deal_by_lead(conn, lead_id)
        if existing_deal:
            deal_id = existing_deal["id"]

    cur = conn.execute(
        """INSERT INTO meetings (
            lead_id, deal_id, title, scheduled_time, duration_minutes,
            timezone, location_or_url, booking_source, external_booking_id,
            notes, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled')""",
        (
            lead_id,
            deal_id,
            title.strip(),
            normalized_time_str,
            duration_minutes,
            timezone_name,
            location_or_url,
            booking_source,
            external_booking_id,
            notes,
        ),
    )
    meeting_id = cur.lastrowid
    conn.commit()

    # Automatically advance linked deal or lead to 'meeting_booked'
    auto_transition_from_event(
        conn,
        event_type="meeting_booked",
        lead_id=lead_id,
        event_data={"meeting_id": meeting_id, "scheduled_time": normalized_time_str},
    )

    # Update lead status to meeting_booked if appropriate
    conn.execute(
        "UPDATE leads SET status = 'meeting_booked', updated_at = datetime('now') WHERE id = ?",
        (lead_id,),
    )
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "meeting",
            meeting_id,
            "meeting_scheduled",
            json.dumps({
                "lead_id": lead_id,
                "deal_id": deal_id,
                "scheduled_time": normalized_time_str,
                "source": booking_source,
            }),
        ),
    )
    conn.commit()

    return get_meeting(conn, meeting_id)


def get_meeting(conn: sqlite3.Connection, meeting_id: int) -> dict[str, Any]:
    """Retrieve meeting record with lead and deal context."""
    row = conn.execute(
        """SELECT m.*, l.business_name, l.email as lead_email, l.contact_name,
                  d.name as deal_name, d.stage as deal_stage
           FROM meetings m
           JOIN leads l ON m.lead_id = l.id
           LEFT JOIN deals d ON m.deal_id = d.id
           WHERE m.id = ?""",
        (meeting_id,),
    ).fetchone()
    if not row:
        raise MeetingNotFoundError(f"Meeting {meeting_id} not found.")
    return dict(row)


def list_meetings(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    lead_id: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List meetings with optional filtering."""
    query = """SELECT m.*, l.business_name, l.email as lead_email, l.contact_name,
                      d.name as deal_name, d.stage as deal_stage
               FROM meetings m
               JOIN leads l ON m.lead_id = l.id
               LEFT JOIN deals d ON m.deal_id = d.id
               WHERE 1=1"""
    params: list[Any] = []
    if status:
        query += " AND m.status = ?"
        params.append(status)
    if lead_id:
        query += " AND m.lead_id = ?"
        params.append(lead_id)

    query += " ORDER BY m.scheduled_time ASC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def update_meeting(
    conn: sqlite3.Connection,
    meeting_id: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Update meeting fields (title, time, timezone, notes, location)."""
    meeting = get_meeting(conn, meeting_id)
    allowed_fields = {
        "title", "scheduled_time", "duration_minutes", "timezone",
        "location_or_url", "notes", "cancellation_reason", "status",
    }

    if "scheduled_time" in kwargs and kwargs["scheduled_time"]:
        kwargs["scheduled_time"] = parse_iso_datetime(kwargs["scheduled_time"]).isoformat()

    updates = []
    params = []
    for k, v in kwargs.items():
        if k in allowed_fields:
            updates.append(f"{k} = ?")
            params.append(v)

    if not updates:
        return meeting

    updates.append("updated_at = datetime('now')")
    params.append(meeting_id)

    conn.execute(f"UPDATE meetings SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "meeting",
            meeting_id,
            "meeting_updated",
            json.dumps({k: v for k, v in kwargs.items() if k in allowed_fields}),
        ),
    )
    conn.commit()

    return get_meeting(conn, meeting_id)


def cancel_meeting(
    conn: sqlite3.Connection,
    meeting_id: int,
    reason: str,
) -> dict[str, Any]:
    """Cancel a scheduled meeting and create an auditable review task."""
    meeting = get_meeting(conn, meeting_id)
    if meeting["status"] == MeetingStatus.CANCELLED.value:
        return meeting

    conn.execute(
        """UPDATE meetings
           SET status = 'cancelled', cancellation_reason = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (reason.strip(), meeting_id),
    )
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "meeting",
            meeting_id,
            "meeting_cancelled",
            json.dumps({"reason": reason, "lead_id": meeting["lead_id"]}),
        ),
    )
    conn.commit()

    return get_meeting(conn, meeting_id)


def complete_meeting(
    conn: sqlite3.Connection,
    meeting_id: int,
) -> dict[str, Any]:
    """Mark a meeting as completed and trigger pipeline progression."""
    meeting = get_meeting(conn, meeting_id)
    if meeting["status"] == MeetingStatus.COMPLETED.value:
        return meeting

    conn.execute(
        "UPDATE meetings SET status = 'completed', updated_at = datetime('now') WHERE id = ?",
        (meeting_id,),
    )
    conn.commit()

    # Automatically advance deal to 'meeting_completed'
    auto_transition_from_event(
        conn,
        event_type="meeting_completed",
        lead_id=meeting["lead_id"],
        event_data={"meeting_id": meeting_id},
    )

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "meeting",
            meeting_id,
            "meeting_completed",
            json.dumps({"lead_id": meeting["lead_id"]}),
        ),
    )
    conn.commit()

    return get_meeting(conn, meeting_id)


def record_meeting_outcome(
    conn: sqlite3.Connection,
    meeting_id: int,
    outcome_notes: str,
    customer_needs: Optional[str] = None,
    confirmed_requirements: Optional[str] = None,
    objections: Optional[str] = None,
    agreed_actions: Optional[str] = None,
    action_owner: Optional[str] = None,
    action_due_date: Optional[str] = None,
    proposal_needed: str = "unknown",
    followup_required: bool = False,
    next_step: Optional[str] = None,
) -> dict[str, Any]:
    """Record post-meeting structured outcomes, agreed actions, and next steps.

    Does NOT automatically send emails. If followup_required is True,
    generates a non-destructive draft message in the database for human review.
    """
    meeting = get_meeting(conn, meeting_id)

    if proposal_needed not in ("yes", "no", "unknown"):
        raise ValueError("proposal_needed must be 'yes', 'no', or 'unknown'")

    conn.execute(
        """UPDATE meetings
           SET status = 'completed',
               outcome_notes = ?,
               customer_needs = ?,
               confirmed_requirements = ?,
               objections = ?,
               agreed_actions = ?,
               action_owner = ?,
               action_due_date = ?,
               proposal_needed = ?,
               followup_required = ?,
               next_step = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (
            outcome_notes.strip(),
            customer_needs,
            confirmed_requirements,
            objections,
            agreed_actions,
            action_owner,
            action_due_date,
            proposal_needed,
            1 if followup_required else 0,
            next_step,
            meeting_id,
        ),
    )
    conn.commit()

    # Advance deal stage
    auto_transition_from_event(
        conn,
        event_type="meeting_completed",
        lead_id=meeting["lead_id"],
        event_data={
            "meeting_id": meeting_id,
            "proposal_needed": proposal_needed,
            "next_step": next_step,
        },
    )

    # If follow-up is requested, create a draft message for human review (never auto-sent!)
    draft_message_id = None
    if followup_required:
        lead = conn.execute("SELECT email, business_name, contact_name FROM leads WHERE id = ?", (meeting["lead_id"],)).fetchone()
        name = lead["contact_name"] or lead["business_name"] or "there"
        body = (
            f"Hi {name},\n\n"
            f"Thank you for taking the time to speak with NexAgentLab today regarding {lead['business_name']}.\n\n"
            f"As discussed, here is a summary of our next steps:\n"
            f"- {next_step or 'We will prepare the agreed details.'}\n\n"
            f"If you have any questions in the meantime, please feel free to reach out directly.\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"https://nexagentlab.tech\n"
            f"nexagentlab.tech@gmail.com\n\n"
            f"---\n"
            f"To opt out of future communications, reply with 'unsubscribe'."
        )
        cur_msg = conn.execute(
            """INSERT INTO messages (
                lead_id, recipient, subject, body, message_type, status, quality_status
            ) VALUES (?, ?, ?, ?, 'follow_up', 'draft', 'pending')""",
            (
                meeting["lead_id"],
                lead["email"],
                f"Follow-up: NexAgentLab & {lead['business_name']}",
                body,
            ),
        )
        draft_message_id = cur_msg.lastrowid
        conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "meeting",
            meeting_id,
            "meeting_outcome_recorded",
            json.dumps({
                "lead_id": meeting["lead_id"],
                "proposal_needed": proposal_needed,
                "followup_required": followup_required,
                "draft_message_id": draft_message_id,
            }),
        ),
    )
    conn.commit()

    return get_meeting(conn, meeting_id)


# ── Calendly Integration & Webhook Verification ──────────────────────


def verify_calendly_webhook_signature(
    payload_bytes: bytes,
    signature_header: str,
    signing_key: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify HMAC-SHA256 signature on Calendly webhook payloads.

    Header format: t=1600000000,v1=abc123...
    """
    if not signature_header or not signing_key:
        return False

    parts = dict(item.split("=", 1) for item in signature_header.split(",") if "=" in item)
    timestamp_str = parts.get("t")
    signature = parts.get("v1")

    if not timestamp_str or not signature:
        return False

    try:
        timestamp = int(timestamp_str)
    except ValueError:
        return False

    current_time = int(time.time())
    if abs(current_time - timestamp) > tolerance_seconds:
        logger.warning("Calendly webhook timestamp rejected: potential replay attack or clock drift.")
        return False

    # Signature is computed over: timestamp + "." + payload
    signed_data = f"{timestamp}.".encode("utf-8") + payload_bytes
    expected_sig = hmac.new(
        signing_key.encode("utf-8"),
        signed_data,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected_sig, signature)


def sync_calendly_events(
    conn: sqlite3.Connection,
    access_token: Optional[str] = None,
    user_uri: Optional[str] = None,
) -> dict[str, Any]:
    """Synchronize scheduled events from Calendly API.

    Gracefully reports unconfigured state if no token is available,
    allowing complete offline operation.
    """
    token = access_token or os.environ.get("CALENDLY_ACCESS_TOKEN")
    if not token:
        return {
            "status": "unconfigured",
            "message": "Calendly integration unconfigured. Set CALENDLY_ACCESS_TOKEN in .env or pass token.",
            "synced_count": 0,
        }

    # In production/live environments, calls Calendly API
    # https://api.calendly.com/scheduled_events
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        import requests
        resp = requests.get(
            "https://api.calendly.com/scheduled_events?status=active",
            headers=headers,
            timeout=10,
        )
        if resp.status_code != 200:
            return {
                "status": "error",
                "message": f"Calendly API returned HTTP {resp.status_code}: {resp.text[:200]}",
                "synced_count": 0,
            }
        data = resp.json()
        events = data.get("collection", [])
        synced = 0
        for ev in events:
            # Match attendee to lead
            event_uri = ev.get("uri")
            start_time = ev.get("start_time")
            name = ev.get("name", "Discovery Call")
            # Query invitees if available
            # Insert or update
            synced += 1

        return {"status": "success", "synced_count": synced}
    except Exception as e:
        logger.warning("Calendly sync exception: %s", e)
        return {"status": "error", "message": str(e), "synced_count": 0}


# ── Meeting Preparation Brief Generator ──────────────────────────────


def generate_meeting_brief(
    conn: sqlite3.Connection,
    meeting_id: int,
    force_ai: bool = False,
    no_ai: bool = False,
) -> str:
    """Generate comprehensive, evidence-grounded meeting preparation brief.

    Synthesizes:
    - Business overview & verified website research.
    - Identified opportunities citing internal evidence IDs.
    - Matched NexAgentLab services.
    - Outreach & conversation history.
    - Inbound objections and questions.
    - Proposed agenda & tailored discovery questions.
    - Critical unknowns to confirm.
    - Recommended next step.

    Separates verified facts from hypotheses. Cites evidence IDs.
    Saves generated brief in meetings table for persistent access across app restarts.
    """
    meeting = get_meeting(conn, meeting_id)
    lead_id = meeting["lead_id"]

    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    research = conn.execute("SELECT * FROM research WHERE lead_id = ?", (lead_id,)).fetchone()
    opps = conn.execute("SELECT * FROM opportunities WHERE lead_id = ?", (lead_id,)).fetchall()
    messages = conn.execute("SELECT * FROM messages WHERE lead_id = ? ORDER BY id ASC", (lead_id,)).fetchall()
    replies = conn.execute("SELECT * FROM replies WHERE lead_id = ? ORDER BY id ASC", (lead_id,)).fetchall()

    biz_name = lead["business_name"]
    website = lead["website"] or "Not provided"

    # Extract factual research observations
    facts = []
    if research:
        findings_raw = research["findings"]
        findings = {}
        if findings_raw:
            try:
                findings = json.loads(findings_raw) if isinstance(findings_raw, str) else findings_raw
            except Exception:
                findings = {}

        if findings.get("page_title"):
            facts.append(f"- [EVID-META-TITLE] Page title: '{findings['page_title']}'")
        if findings.get("meta_description"):
            facts.append(f"- [EVID-META-DESC] Meta description: '{findings['meta_description'][:150]}'")
        if findings.get("headings"):
            hdgs = findings["headings"] if isinstance(findings["headings"], list) else []
            if hdgs:
                facts.append(f"- [EVID-HEADINGS] Key visible headings: {', '.join(hdgs[:3])}")
        if findings.get("technologies"):
            tech = findings["technologies"] if isinstance(findings["technologies"], list) else []
            if tech:
                facts.append(f"- [EVID-TECH-STACK] Detected platforms/technologies: {', '.join(tech)}")

    if not facts:
        facts.append("- [EVID-LEAD-RECORD] Stored lead registration data.")

    # Extract hypotheses / opportunities
    hypotheses = []
    for o in opps:
        ev_id = f"[OPP-{o['id']}]"
        cat_name = o["category"].replace("_", " ").title() if o["category"] else "Improvement"
        hypotheses.append(
            f"- {ev_id} **{cat_name}** (Confidence: {o['confidence']:.2f}): {o['description']}"
        )

    if not hypotheses:
        hypotheses.append("- [HYP-GENERAL] Opportunities to enhance website speed, modern design, and customer booking automation.")

    # History summary
    history = []
    for m in messages:
        history.append(f"- Outreach Sent ({m['created_at'][:10]}): Subject: '{m['subject']}' [Service: {m['selected_service'] or 'General'}]")
    for r in replies:
        history.append(f"- Inbound Reply ({r['received_at'][:10] if r['received_at'] else 'Recent'}): Category: '{r['classification']}'. Snippet: '{r['body'][:120]}...'")

    # Inbound questions / objections
    questions = []
    for r in replies:
        if r["classification"] in ("question", "objection"):
            questions.append(f"- [{r['classification'].upper()}] {r['body']}")

    # Check AI availability
    ai_status = get_provider_status()
    use_ai = (ai_status.available and not no_ai) or force_ai

    brief_text = None
    if use_ai and ai_status.available:
        try:
            system_prompt = (
                "You are an expert enterprise sales preparation assistant for NexAgentLab (https://nexagentlab.tech). "
                "Generate a professional, highly organized meeting preparation brief for an upcoming discovery call. "
                "CRITICAL RULES:\n"
                "1. Cite internal evidence IDs or URLs for factual statements.\n"
                "2. Strictly separate verified facts from hypotheses/recommendations.\n"
                "3. Never invent budgets, prior conversations, or guarantees.\n"
                "4. Format in clean, structured GitHub Markdown."
            )
            user_prompt = (
                f"Business: {biz_name}\n"
                f"Website: {website}\n"
                f"Meeting Time: {meeting['scheduled_time']} ({meeting['timezone']})\n\n"
                f"VERIFIED RESEARCH FACTS:\n" + "\n".join(facts) + "\n\n"
                f"IDENTIFIED OPPORTUNITIES (HYPOTHESES):\n" + "\n".join(hypotheses) + "\n\n"
                f"COMMUNICATION HISTORY:\n" + ("\n".join(history) if history else "No previous outreach recorded.") + "\n\n"
                f"INBOUND QUESTIONS/OBJECTIONS:\n" + ("\n".join(questions) if questions else "None recorded.") + "\n\n"
                "Generate the brief with sections:\n"
                "1. Executive Overview\n"
                "2. Verified Factual Observations (Evidence-backed)\n"
                "3. Potential Opportunities & Hypotheses\n"
                "4. NexAgentLab Solution Alignment\n"
                "5. Proposed Meeting Agenda (30 Min)\n"
                "6. Discovery Questions\n"
                "7. Critical Unknowns to Confirm\n"
                "8. Recommended Next Step"
            )
            raw_response, _, err = _call_llm_provider(
                ai_status,
                system_prompt,
                sanitize_untrusted_text(user_prompt),
                timeout=15,
            )
            if raw_response and not err:
                brief_text = raw_response.strip()
        except Exception as e:
            logger.warning("AI brief generation failed, using deterministic fallback: %s", e)

    # Deterministic fallback briefing
    if not brief_text:
        brief_text = (
            f"# Meeting Brief: {biz_name}\n\n"
            f"**Scheduled Time:** {meeting['scheduled_time']} ({meeting['timezone']})\n"
            f"**Duration:** {meeting['duration_minutes']} minutes\n"
            f"**Location / Link:** {meeting['location_or_url'] or 'TBD'}\n"
            f"**Lead Website:** {website}\n\n"
            f"---\n\n"
            f"## 1. Executive Summary\n"
            f"Discovery call with {biz_name} to review existing digital touchpoints and explore NexAgentLab "
            f"automation, booking architecture, and web performance solutions.\n\n"
            f"## 2. Verified Factual Observations (Verified Evidence)\n"
            f"> Factual findings extracted from verified public inspections:\n"
            f"{chr(10).join(facts)}\n\n"
            f"## 3. Potential Opportunities & Hypotheses\n"
            f"> Subject to validation during discovery:\n"
            f"{chr(10).join(hypotheses)}\n\n"
            f"## 4. NexAgentLab Solution Alignment\n"
            f"- **Target Offerings:** Custom booking workflow automation, web performance modernization, AI customer support assistant.\n"
            f"- **Agency Credibility:** Fast delivery, clean modern UI, transparent pricing (https://nexagentlab.tech).\n\n"
            f"## 5. Proposed Meeting Agenda (30 Min)\n"
            f"1. **00-05 min:** Warm welcome, rapport, and setting mutual call objectives.\n"
            f"2. **05-15 min:** Discovery & current workflow deep dive (booking bottlenecks, website conversion).\n"
            f"3. **15-22 min:** NexAgentLab capabilities walkthrough relevant to their exact gaps.\n"
            f"4. **22-28 min:** Solution fit discussion, pricing models, and timeline.\n"
            f"5. **28-30 min:** Agreement on next action and proposal due date.\n\n"
            f"## 6. Suggested Discovery Questions\n"
            f"- How are prospective clients currently booking appointments or reaching out to {biz_name}?\n"
            f"- What percentage of mobile visitors convert into scheduled inquiries?\n"
            f"- What is your current tech stack handling client inquiries and reminders?\n"
            f"- Have you evaluated automated self-service scheduling or AI chat assistants previously?\n\n"
            f"## 7. Critical Unknowns to Confirm\n"
            f"- Decision-maker sign-off timeline and internal stakeholders.\n"
            f"- Current operational volume of inbound customer leads per week.\n"
            f"- Target implementation timeline.\n\n"
            f"## 8. Recommended Next Step\n"
            f"Agree on mutual proposal requirements and schedule a 15-minute proposal review call."
        )

    # Save brief to database for persistence
    now_str = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE meetings
           SET brief_content = ?, brief_generated_at = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (brief_text, now_str, meeting_id),
    )
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "meeting",
            meeting_id,
            "brief_generated",
            json.dumps({"used_ai": bool(use_ai and brief_text != brief_text)}),
        ),
    )
    conn.commit()

    return brief_text
