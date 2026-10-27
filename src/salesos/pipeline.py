"""Sales Pipeline, Deal Intelligence, and Evidence-Based Stage Transitions.

Provides:
- Independent sales deals table decoupling CRM sales stages from technical research.
- Centrally defined pipeline stages and valid state transitions.
- Evidence-based automated transitions with verification and audit logging.
- Protection against moving suppressed leads into active outreach.
- Requirement of explicit confirmation for won/lost outcomes.
- Reporting, data quality metrics, and CSV/JSON export utilities.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class DealStage(str, Enum):
    """Permitted sales pipeline stages."""
    LEAD_CAPTURED = "lead_captured"
    OUTREACH_READY = "outreach_ready"
    CONTACTED = "contacted"
    ENGAGED = "engaged"
    MEETING_BOOKED = "meeting_booked"
    MEETING_COMPLETED = "meeting_completed"
    PROPOSAL_SENT = "proposal_sent"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"


ALL_STAGES = [s.value for s in DealStage]

# Default permitted forward stage transitions
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    DealStage.LEAD_CAPTURED.value: {DealStage.OUTREACH_READY.value, DealStage.LOST.value},
    DealStage.OUTREACH_READY.value: {DealStage.CONTACTED.value, DealStage.LOST.value},
    DealStage.CONTACTED.value: {DealStage.ENGAGED.value, DealStage.LOST.value},
    DealStage.ENGAGED.value: {DealStage.MEETING_BOOKED.value, DealStage.LOST.value},
    DealStage.MEETING_BOOKED.value: {DealStage.MEETING_COMPLETED.value, DealStage.LOST.value},
    DealStage.MEETING_COMPLETED.value: {DealStage.PROPOSAL_SENT.value, DealStage.ENGAGED.value, DealStage.LOST.value},
    DealStage.PROPOSAL_SENT.value: {DealStage.NEGOTIATION.value, DealStage.WON.value, DealStage.LOST.value},
    DealStage.NEGOTIATION.value: {DealStage.WON.value, DealStage.LOST.value},
    DealStage.WON.value: set(),
    DealStage.LOST.value: set(),
}


class InvalidTransitionError(ValueError):
    """Raised when attempting an illegal or unsupported stage transition."""


class DealNotFoundError(LookupError):
    """Raised when a referenced deal cannot be located."""


class SuppressedLeadError(PermissionError):
    """Raised when attempting to move a suppressed lead back into active outreach."""


def _check_suppression(conn: sqlite3.Connection, lead_id: int) -> bool:
    """Check if lead email is suppressed."""
    row = conn.execute(
        """SELECT s.id FROM suppression s
           JOIN leads l ON s.email = l.email
           WHERE l.id = ?""",
        (lead_id,),
    ).fetchone()
    return row is not None


def create_deal(
    conn: sqlite3.Connection,
    lead_id: int,
    name: str,
    service_interest: Optional[str] = None,
    stage: str = DealStage.LEAD_CAPTURED.value,
    estimated_value: Optional[float] = None,
    currency: str = "USD",
    confidence: Optional[float] = None,
    confidence_explanation: Optional[str] = None,
    owner: Optional[str] = None,
    expected_close_date: Optional[str] = None,
    source_campaign_id: Optional[int] = None,
    next_action: Optional[str] = None,
    next_action_due: Optional[str] = None,
    initial_evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a new sales deal attached to a lead.

    Never invents budget, probability, or close dates: missing values remain NULL.
    """
    if stage not in ALL_STAGES:
        raise ValueError(f"Invalid deal stage: {stage}. Must be one of {ALL_STAGES}")

    lead = conn.execute("SELECT id, business_name, email FROM leads WHERE id = ?", (lead_id,)).fetchone()
    if not lead:
        raise ValueError(f"Lead ID {lead_id} does not exist.")

    if stage in (DealStage.OUTREACH_READY.value, DealStage.CONTACTED.value, DealStage.ENGAGED.value):
        if _check_suppression(conn, lead_id):
            raise SuppressedLeadError(f"Lead {lead_id} ({lead['email']}) is suppressed and cannot enter active outreach.")

    evidence_list = []
    if initial_evidence:
        evidence_list.append({
            "stage": stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": "deal_created",
            **initial_evidence,
        })

    cur = conn.execute(
        """INSERT INTO deals (
            lead_id, name, service_interest, stage, estimated_value, currency,
            confidence, confidence_explanation, owner, expected_close_date,
            source_campaign_id, last_activity_at, next_action, next_action_due,
            stage_evidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)""",
        (
            lead_id,
            name.strip(),
            service_interest,
            stage,
            estimated_value,
            currency or "USD",
            confidence,
            confidence_explanation,
            owner,
            expected_close_date,
            source_campaign_id,
            next_action,
            next_action_due,
            json.dumps(evidence_list),
        ),
    )
    deal_id = cur.lastrowid
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "deal",
            deal_id,
            "deal_created",
            json.dumps({"lead_id": lead_id, "name": name, "stage": stage, "estimated_value": estimated_value}),
        ),
    )
    conn.commit()

    return get_deal(conn, deal_id)


def get_deal(conn: sqlite3.Connection, deal_id: int) -> dict[str, Any]:
    """Retrieve deal by ID with associated lead business name."""
    row = conn.execute(
        """SELECT d.*, l.business_name, l.email as lead_email, l.status as lead_status
           FROM deals d
           JOIN leads l ON d.lead_id = l.id
           WHERE d.id = ?""",
        (deal_id,),
    ).fetchone()
    if not row:
        raise DealNotFoundError(f"Deal {deal_id} not found.")

    data = dict(row)
    try:
        data["stage_evidence"] = json.loads(data["stage_evidence"]) if data["stage_evidence"] else []
    except Exception:
        data["stage_evidence"] = []
    return data


def get_deal_by_lead(conn: sqlite3.Connection, lead_id: int) -> Optional[dict[str, Any]]:
    """Retrieve primary or most recent deal for a lead."""
    row = conn.execute(
        """SELECT d.*, l.business_name, l.email as lead_email, l.status as lead_status
           FROM deals d
           JOIN leads l ON d.lead_id = l.id
           WHERE d.lead_id = ?
           ORDER BY d.id DESC LIMIT 1""",
        (lead_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["stage_evidence"] = json.loads(data["stage_evidence"]) if data["stage_evidence"] else []
    except Exception:
        data["stage_evidence"] = []
    return data


def list_deals(
    conn: sqlite3.Connection,
    stage: Optional[str] = None,
    owner: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List deals matching filters."""
    query = """SELECT d.*, l.business_name, l.email as lead_email, l.status as lead_status
               FROM deals d
               JOIN leads l ON d.lead_id = l.id
               WHERE 1=1"""
    params: list[Any] = []
    if stage:
        query += " AND d.stage = ?"
        params.append(stage)
    if owner:
        query += " AND d.owner = ?"
        params.append(owner)

    query += " ORDER BY d.updated_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    deals = []
    for r in rows:
        d = dict(r)
        try:
            d["stage_evidence"] = json.loads(d["stage_evidence"]) if d["stage_evidence"] else []
        except Exception:
            d["stage_evidence"] = []
        deals.append(d)
    return deals


def move_deal_stage(
    conn: sqlite3.Connection,
    deal_id: int,
    target_stage: str,
    evidence: Optional[dict[str, Any]] = None,
    force: bool = False,
    reason: Optional[str] = None,
    actor: Optional[str] = None,
) -> dict[str, Any]:
    """Transition a deal to a new stage with validation and audit trail.

    Args:
        conn: Database connection.
        deal_id: Deal ID to move.
        target_stage: Destination stage.
        evidence: Verified triggering event or business facts.
        force: Allow override of standard progression DAG.
        reason: Mandatory override justification if force=True.
        actor: User or automated subsystem initiating the change.
    """
    deal = get_deal(conn, deal_id)
    current_stage = deal["stage"]

    if target_stage not in ALL_STAGES:
        raise ValueError(f"Invalid stage '{target_stage}'. Must be one of {ALL_STAGES}")

    if target_stage == current_stage:
        return deal  # No-op, already at target stage

    # Suppressed lead outreach protection
    if target_stage in (DealStage.OUTREACH_READY.value, DealStage.CONTACTED.value, DealStage.ENGAGED.value):
        if _check_suppression(conn, deal["lead_id"]):
            raise SuppressedLeadError(
                f"Cannot advance Deal {deal_id} to '{target_stage}': Lead is suppressed (opt-out/bounced)."
            )

    # Check allowed transitions unless force override
    allowed = ALLOWED_TRANSITIONS.get(current_stage, set())
    if not force and target_stage not in allowed:
        raise InvalidTransitionError(
            f"Invalid stage transition from '{current_stage}' to '{target_stage}'. "
            f"Allowed transitions: {sorted(allowed)}. Use --force with an auditable reason to override."
        )

    # Outcome confirmation requirements
    if target_stage == DealStage.WON.value:
        win_notes = (evidence or {}).get("win_notes") or deal.get("win_notes")
        if not win_notes and not force:
            raise InvalidTransitionError("Cannot mark deal as 'won' without explicit confirmation and win notes.")

    if target_stage == DealStage.LOST.value:
        loss_reason = (evidence or {}).get("loss_reason") or deal.get("loss_reason")
        if not loss_reason and not force:
            raise InvalidTransitionError("Cannot mark deal as 'lost' without specifying a loss reason.")

    if force and not reason:
        raise ValueError("Manual stage override (--force) requires an auditable reason.")

    # Record stage evidence entry
    ev_entry = {
        "from_stage": current_stage,
        "to_stage": target_stage,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": actor or ("manual_override" if force else "system"),
        "force": force,
        "override_reason": reason,
        **(evidence or {}),
    }

    current_ev_list = deal["stage_evidence"]
    current_ev_list.append(ev_entry)

    loss_reason_val = deal.get("loss_reason")
    win_notes_val = deal.get("win_notes")
    if target_stage == DealStage.LOST.value and evidence and "loss_reason" in evidence:
        loss_reason_val = evidence["loss_reason"]
    elif target_stage == DealStage.WON.value and evidence and "win_notes" in evidence:
        win_notes_val = evidence["win_notes"]

    conn.execute(
        """UPDATE deals
           SET stage = ?,
               stage_evidence = ?,
               loss_reason = ?,
               win_notes = ?,
               last_activity_at = datetime('now'),
               updated_at = datetime('now')
           WHERE id = ?""",
        (
            target_stage,
            json.dumps(current_ev_list),
            loss_reason_val,
            win_notes_val,
            deal_id,
        ),
    )
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "deal",
            deal_id,
            "stage_transition",
            json.dumps({
                "from_stage": current_stage,
                "to_stage": target_stage,
                "force": force,
                "reason": reason,
                "actor": actor,
            }),
        ),
    )
    conn.commit()

    return get_deal(conn, deal_id)


def update_deal(
    conn: sqlite3.Connection,
    deal_id: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """Update editable deal attributes."""
    deal = get_deal(conn, deal_id)
    allowed_fields = {
        "name", "service_interest", "estimated_value", "currency",
        "confidence", "confidence_explanation", "owner",
        "expected_close_date", "next_action", "next_action_due",
        "loss_reason", "win_notes",
    }

    updates = []
    params = []
    for k, v in kwargs.items():
        if k in allowed_fields:
            updates.append(f"{k} = ?")
            params.append(v)

    if not updates:
        return deal

    updates.append("updated_at = datetime('now')")
    params.append(deal_id)

    conn.execute(
        f"UPDATE deals SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    conn.commit()

    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (
            "deal",
            deal_id,
            "deal_updated",
            json.dumps({k: v for k, v in kwargs.items() if k in allowed_fields}),
        ),
    )
    conn.commit()

    return get_deal(conn, deal_id)


def auto_transition_from_event(
    conn: sqlite3.Connection,
    event_type: str,
    lead_id: int,
    event_data: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Safely and idempotently update deal stage based on real sales activity events.

    Supported events:
    - 'lead_qualified': advance to 'outreach_ready'
    - 'outreach_sent': advance to 'contacted'
    - 'inbound_engaged': advance to 'engaged'
    - 'meeting_booked': advance to 'meeting_booked'
    - 'meeting_completed': advance to 'meeting_completed'
    """
    deal = get_deal_by_lead(conn, lead_id)
    if not deal:
        # Create initial deal if none exists
        lead = conn.execute("SELECT business_name FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            return None
        deal = create_deal(
            conn,
            lead_id=lead_id,
            name=f"{lead['business_name']} - Deal",
            initial_evidence={"event": event_type, **(event_data or {})},
        )

    current_stage = deal["stage"]
    target_stage = None

    if event_type == "lead_qualified" and current_stage == DealStage.LEAD_CAPTURED.value:
        target_stage = DealStage.OUTREACH_READY.value
    elif event_type == "outreach_sent" and current_stage in (DealStage.LEAD_CAPTURED.value, DealStage.OUTREACH_READY.value):
        target_stage = DealStage.CONTACTED.value
    elif event_type == "inbound_engaged" and current_stage in (DealStage.LEAD_CAPTURED.value, DealStage.OUTREACH_READY.value, DealStage.CONTACTED.value):
        target_stage = DealStage.ENGAGED.value
    elif event_type == "meeting_booked" and current_stage in (DealStage.LEAD_CAPTURED.value, DealStage.OUTREACH_READY.value, DealStage.CONTACTED.value, DealStage.ENGAGED.value):
        target_stage = DealStage.MEETING_BOOKED.value
    elif event_type == "meeting_completed" and current_stage == DealStage.MEETING_BOOKED.value:
        target_stage = DealStage.MEETING_COMPLETED.value

    if target_stage and target_stage != current_stage:
        try:
            return move_deal_stage(
                conn,
                deal["id"],
                target_stage,
                evidence={"trigger_event": event_type, **(event_data or {})},
                force=True,
                reason=f"Automated event transition: {event_type}",
                actor=f"auto_{event_type}",
            )
        except (InvalidTransitionError, SuppressedLeadError):
            return deal

    return deal


def get_pipeline_report(conn: sqlite3.Connection, inactive_days: int = 14) -> dict[str, Any]:
    """Generate pipeline analytics, value summaries, warnings, and win rates."""
    # 1. Stage counts and estimated values
    rows = conn.execute(
        """SELECT stage,
                  count(*) as deal_count,
                  sum(CASE WHEN estimated_value IS NOT NULL THEN estimated_value ELSE 0 END) as total_val,
                  sum(CASE WHEN estimated_value IS NOT NULL THEN 1 ELSE 0 END) as valued_deals_count
           FROM deals
           GROUP BY stage"""
    ).fetchall()

    by_stage: dict[str, dict[str, Any]] = {s: {"count": 0, "total_value": 0.0, "valued_count": 0} for s in ALL_STAGES}
    for r in rows:
        by_stage[r["stage"]] = {
            "count": r["deal_count"],
            "total_value": float(r["total_val"] or 0.0),
            "valued_count": r["valued_deals_count"],
        }

    # 2. Meetings counts
    m_rows = conn.execute(
        "SELECT status, count(*) as cnt FROM meetings GROUP BY status"
    ).fetchall()
    meetings_summary = {r["status"]: r["cnt"] for r in m_rows}

    # 3. Pending action cues
    leads_awaiting_contact = conn.execute(
        """SELECT count(*) FROM leads l
           LEFT JOIN messages m ON l.id = m.lead_id AND m.status = 'sent'
           WHERE m.id IS NULL AND l.status IN ('qualified', 'ready_for_outreach')"""
    ).fetchone()[0]

    positive_replies_review = conn.execute(
        """SELECT count(*) FROM replies
           WHERE needs_review = 1 AND classification IN ('positive_interest', 'meeting_request', 'question')"""
    ).fetchone()[0]

    meetings_needing_prep = conn.execute(
        """SELECT count(*) FROM meetings
           WHERE status = 'scheduled' AND (brief_content IS NULL OR length(brief_content) = 0)"""
    ).fetchone()[0]

    overdue_actions = conn.execute(
        """SELECT count(*) FROM deals
           WHERE next_action_due IS NOT NULL
             AND next_action_due < date('now')
             AND stage NOT IN ('won', 'lost')"""
    ).fetchone()[0]

    stalled_deals = conn.execute(
        f"""SELECT count(*) FROM deals
            WHERE last_activity_at < datetime('now', '-{inactive_days} days')
              AND stage NOT IN ('won', 'lost')"""
    ).fetchone()[0]

    # 4. Win / Loss rates
    won_count = by_stage[DealStage.WON.value]["count"]
    lost_count = by_stage[DealStage.LOST.value]["count"]
    closed_total = won_count + lost_count
    win_rate = (won_count / closed_total * 100.0) if closed_total > 0 else 0.0

    # 5. Data quality warnings
    warnings: list[str] = []
    missing_owner = conn.execute(
        "SELECT count(*) FROM deals WHERE (owner IS NULL OR owner = '') AND stage NOT IN ('won', 'lost')"
    ).fetchone()[0]
    if missing_owner > 0:
        warnings.append(f"{missing_owner} active deal(s) have no assigned owner.")

    missing_next_action = conn.execute(
        "SELECT count(*) FROM deals WHERE (next_action IS NULL OR next_action = '') AND stage NOT IN ('won', 'lost')"
    ).fetchone()[0]
    if missing_next_action > 0:
        warnings.append(f"{missing_next_action} active deal(s) have no defined next action.")

    missing_evidence = conn.execute(
        "SELECT count(*) FROM deals WHERE stage != 'lead_captured' AND (stage_evidence IS NULL OR stage_evidence = '[]')"
    ).fetchone()[0]
    if missing_evidence > 0:
        warnings.append(f"{missing_evidence} advanced deal(s) lack recorded stage evidence.")

    return {
        "by_stage": by_stage,
        "total_active_deals": sum(v["count"] for k, v in by_stage.items() if k not in ("won", "lost")),
        "total_pipeline_value": sum(v["total_value"] for k, v in by_stage.items() if k not in ("won", "lost")),
        "meetings_summary": meetings_summary,
        "action_cues": {
            "leads_awaiting_contact": leads_awaiting_contact,
            "positive_replies_review": positive_replies_review,
            "meetings_needing_prep": meetings_needing_prep,
            "overdue_actions": overdue_actions,
            "stalled_deals": stalled_deals,
        },
        "performance": {
            "won_count": won_count,
            "lost_count": lost_count,
            "win_rate_percent": round(win_rate, 1),
        },
        "data_quality_warnings": warnings,
    }


def export_pipeline(
    conn: sqlite3.Connection,
    filepath: str,
    format: str = "csv",
) -> int:
    """Export all pipeline deals to CSV or JSON file."""
    deals = list_deals(conn, limit=10000)

    if format.lower() == "json":
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(deals, f, indent=2, default=str)
        return len(deals)

    # Default CSV
    fieldnames = [
        "id", "lead_id", "business_name", "lead_email", "name",
        "service_interest", "stage", "estimated_value", "currency",
        "confidence", "owner", "expected_close_date", "last_activity_at",
        "next_action", "next_action_due", "loss_reason", "win_notes",
        "created_at", "updated_at",
    ]

    with open(filepath, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for d in deals:
            writer.writerow(d)

    return len(deals)
