"""Lead Conversation & CRM Timeline Engine for NexAgentLab SalesOS.

Aggregates complete chronological interaction history for each lead:
lead creation, research, qualification, outreach drafts, approvals, dispatches,
inbound replies, classifications, follow-up schedules, suppression events,
and auditable human actions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


def get_lead_timeline(conn: sqlite3.Connection, lead_id: int) -> dict:
    """Retrieve structured chronological timeline of all events for a lead."""
    # 1. Lead details
    lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        raise ValueError(f"Lead ID {lead_id} not found.")

    events: list[dict[str, Any]] = []

    # Lead imported event
    events.append({
        "timestamp": lead["created_at"],
        "type": "lead_imported",
        "title": "Lead Imported",
        "description": f"Lead created from source '{lead['source'] or 'direct'}' with email {lead['email']}.",
        "badge": "info",
    })

    # 2. Research events
    research_rows = conn.execute(
        "SELECT * FROM research WHERE lead_id = ? ORDER BY created_at ASC",
        (lead_id,),
    ).fetchall()
    for r in research_rows:
        events.append({
            "timestamp": r["created_at"],
            "type": "research_completed",
            "title": "Website Researched",
            "description": f"Agent {r['agent_type']} inspected website (Confidence: {r['confidence'] or 0:.2f}).",
            "badge": "info",
        })

    # 3. Qualification events from audit log
    audit_rows = conn.execute(
        """SELECT * FROM audit_log
           WHERE (entity_type = 'lead' AND entity_id = ?)
              OR (entity_type = 'qualification' AND entity_id = ?)
           ORDER BY created_at ASC""",
        (lead_id, lead_id),
    ).fetchall()
    for a in audit_rows:
        if a["action"] == "lead_qualified":
            details = json.loads(a["details"]) if a["details"] else {}
            events.append({
                "timestamp": a["created_at"],
                "type": "qualification",
                "title": f"Lead Qualified (Score: {details.get('score', 0):.1f})",
                "description": f"Priority: {details.get('priority', '').upper()} — {details.get('recommended_action', '')}",
                "badge": "success" if details.get("eligible") else "warning",
            })

    # 4. Messages (Outreach and Follow-ups)
    msg_rows = conn.execute(
        "SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at ASC",
        (lead_id,),
    ).fetchall()
    for m in msg_rows:
        mid = m["id"]
        mtype = m["message_type"]
        status = m["status"]

        # Draft created
        events.append({
            "timestamp": m["created_at"],
            "type": f"{mtype}_draft_created",
            "title": f"{mtype.replace('_', ' ').title()} Draft Created (Msg #{mid})",
            "description": f"Subject: '{m['subject']}' | Service: {m['selected_service'] or 'General'}",
            "badge": "draft",
        })

        # Approved event
        if m["approved_at"]:
            events.append({
                "timestamp": m["approved_at"],
                "type": "message_approved",
                "title": f"Message #{mid} Approved",
                "description": "User approved message for outbound sending.",
                "badge": "success",
            })

        # Sent event
        if m["sent_at"] and status == "sent":
            events.append({
                "timestamp": m["sent_at"],
                "type": "message_sent",
                "title": f"Message #{mid} Sent ({mtype.replace('_', ' ').title()})",
                "description": f"Dispatched to {m['recipient']} (Gmail ID: {m['gmail_message_id'] or 'dry-run'}).",
                "badge": "success",
            })
        elif status == "failed":
            events.append({
                "timestamp": m["sent_at"] or m["created_at"],
                "type": "message_failed",
                "title": f"Message #{mid} Failed to Send",
                "description": f"Error: {m['error_message']}",
                "badge": "error",
            })
        elif status == "cancelled":
            events.append({
                "timestamp": m["created_at"],
                "type": "message_cancelled",
                "title": f"Message #{mid} Cancelled",
                "description": f"Reason: {m['error_message'] or 'Cancelled before sending'}",
                "badge": "warning",
            })

    # 5. Inbound Replies
    reply_rows = conn.execute(
        "SELECT * FROM replies WHERE lead_id = ? ORDER BY created_at ASC",
        (lead_id,),
    ).fetchall()
    for rep in reply_rows:
        rid = rep["id"]
        cls = rep["classification"] or "unclassified"
        conf = rep["confidence"] or 0.0

        events.append({
            "timestamp": rep["received_at"] or rep["created_at"],
            "type": "reply_received",
            "title": f"Inbound Reply Received (Reply #{rid})",
            "description": f"From: {rep['from_email']} | Subject: '{rep['subject']}'",
            "badge": "reply",
        })

        if rep["is_classified"]:
            events.append({
                "timestamp": rep["processed_at"] or rep["created_at"],
                "type": "reply_classified",
                "title": f"Reply #{rid} Classified: [{cls.upper()}]",
                "description": f"Confidence: {conf:.2f} — {rep['explanation']}"
                               + (" (Review Required)" if rep["needs_review"] else ""),
                "badge": "success" if cls in ("positive_interest", "meeting_request") else "warning",
            })

    # 6. Follow-up schedules
    followup_rows = conn.execute(
        "SELECT * FROM followups WHERE lead_id = ? ORDER BY followup_number ASC",
        (lead_id,),
    ).fetchall()
    for f in followup_rows:
        step = f["followup_number"]
        fstatus = f["status"]
        events.append({
            "timestamp": f["created_at"],
            "type": "followup_planned",
            "title": f"Step {step} Follow-Up Planned (Due: {f['due_date']})",
            "description": f"Status: [{fstatus.upper()}] — {f['scheduled_reason']}",
            "badge": "info",
        })
        if fstatus == "cancelled":
            events.append({
                "timestamp": f["updated_at"],
                "type": "followup_cancelled",
                "title": f"Step {step} Follow-Up Cancelled",
                "description": f"Reason: {f['cancel_reason']}",
                "badge": "warning",
            })

    # 7. Suppression records
    supp_rows = conn.execute(
        "SELECT * FROM suppression WHERE lower(trim(email)) = lower(trim(?))",
        (lead["email"],),
    ).fetchall()
    for s in supp_rows:
        events.append({
            "timestamp": s["created_at"],
            "type": "suppression_recorded",
            "title": "Email Added to Suppression List",
            "description": f"Reason: {s['reason'].upper()} | Source: {s['source']}",
            "badge": "error",
        })

    # Sort events chronologically
    def parse_time(e: dict) -> str:
        t = e.get("timestamp") or "1970-01-01"
        return str(t)

    events.sort(key=parse_time)

    return {
        "lead_id": lead_id,
        "business_name": lead["business_name"],
        "contact_name": lead["contact_name"],
        "email": lead["email"],
        "website": lead["website"],
        "status": lead["status"],
        "score": lead["score"],
        "events": events,
    }
