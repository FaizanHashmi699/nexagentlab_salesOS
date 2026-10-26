"""Lead Qualification Agent for NexAgentLab SalesOS.

Evaluates researched leads against NexAgentLab's service offerings,
evidence strength, opportunity relevance, contact data validity, and
suppression records. Produces an explainable 0-100 score, priority category,
and recommended next action.

Deterministic and fully functional without requiring an external AI API key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

import sqlite3
from pydantic import BaseModel, Field

from .db import audit_log, transaction

logger = logging.getLogger(__name__)

SCORING_VERSION = "v1.0"


# ── Pydantic models ──────────────────────────────────────────────────


class QualificationResult(BaseModel):
    """Detailed qualification evaluation for a single lead."""

    lead_id: int
    business_name: str
    email: str
    score: float = Field(..., ge=0.0, le=100.0)
    priority: Literal["high", "medium", "low", "unqualified", "suppressed"]
    eligible: bool
    recommended_action: str
    explanation: list[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    scoring_version: str = SCORING_VERSION
    qualified_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class QualificationSummary(BaseModel):
    """Batch run summary for lead qualification."""

    total: int = 0
    qualified: int = 0
    unqualified: int = 0
    suppressed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0


# ── Scoring core ─────────────────────────────────────────────────────


def evaluate_lead_qualification(
    lead_row: sqlite3.Row,
    research_row: sqlite3.Row | None,
    opportunity_rows: list[sqlite3.Row],
    is_suppressed: bool,
) -> QualificationResult:
    """Calculate an explainable qualification score for a lead."""
    lead_id = lead_row["id"]
    biz_name = lead_row["business_name"] or "Unknown Business"
    email = (lead_row["email"] or "").strip().lower()

    explanation: list[str] = []

    # 1. Immediate exclusion: Suppression & Opt-Out
    if is_suppressed:
        return QualificationResult(
            lead_id=lead_id,
            business_name=biz_name,
            email=email,
            score=0.0,
            priority="suppressed",
            eligible=False,
            recommended_action="Do not contact — email is on the suppression or opt-out list",
            explanation=["Contact email is recorded in the suppression list."],
            confidence=1.0,
        )

    # 2. Check for invalid or missing email
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return QualificationResult(
            lead_id=lead_id,
            business_name=biz_name,
            email=email,
            score=0.0,
            priority="unqualified",
            eligible=False,
            recommended_action="Update or verify contact email address before proceeding",
            explanation=["Lead lacks a valid, reachable email address."],
            confidence=1.0,
        )

    # Start scoring
    score = 0.0
    evidence_points = 0.0
    contact_points = 0.0
    opportunity_points = 0.0
    fit_points = 0.0

    # ── Factor A: Contact Data Quality & Completeness (Max 20 pts) ─────
    contact_points += 5.0
    explanation.append("+5: Valid email address format verified.")

    if lead_row["contact_name"] and lead_row["contact_name"].strip():
        contact_points += 5.0
        explanation.append(f"+5: Named decision maker known ({lead_row['contact_name']}).")
    else:
        explanation.append("+0: Decision maker name unknown (team outreach required).")

    if lead_row["location"] and lead_row["location"].strip():
        contact_points += 5.0
        explanation.append(f"+5: Geographic location identified ({lead_row['location']}).")

    if lead_row["phone"] and lead_row["phone"].strip():
        contact_points += 5.0
        explanation.append("+5: Direct phone contact available.")

    # ── Factor B: Research & Website Quality (Max 25 pts) ──────────────
    website = (lead_row["website"] or "").strip()
    findings = None
    if research_row and research_row["findings"]:
        try:
            findings = json.loads(research_row["findings"])
        except Exception:
            findings = None

    if website:
        evidence_points += 10.0
        explanation.append(f"+10: Verified public website URL ({website}).")

        if findings:
            if findings.get("title"):
                evidence_points += 5.0
                explanation.append("+5: Valid HTML page title extracted.")
            if findings.get("meta_description"):
                evidence_points += 5.0
                explanation.append("+5: Meta description extracted.")
            if findings.get("technologies_detected") or findings.get("headings"):
                evidence_points += 5.0
                explanation.append("+5: Website technology stack or page structure identified.")
        else:
            explanation.append("+0: Website has not been crawled or research is incomplete.")
    else:
        explanation.append("-15: No public website available for business verification.")
        evidence_points -= 15.0

    # ── Factor C: Opportunity Strength (Max 35 pts) ────────────────────
    confirmed_opps = 0
    likely_opps = 0
    hypothesis_opps = 0

    categories_found = set()

    for opp in opportunity_rows:
        conf = opp["confidence"] or 0.0
        cat = opp["category"]
        categories_found.add(cat)
        priority = opp["priority"] or 3

        if conf >= 0.8:
            confirmed_opps += 1
            pts = 17.0
            explanation.append(f"+17: Confirmed opportunity in {cat} (confidence {conf:.2f}).")
        elif conf >= 0.6:
            likely_opps += 1
            pts = 10.0
            explanation.append(f"+10: Likely opportunity in {cat} (confidence {conf:.2f}).")
        else:
            hypothesis_opps += 1
            pts = 5.0
            explanation.append(f"+5: Hypothesis opportunity in {cat} (confidence {conf:.2f}).")

        if priority in (1, 2):
            pts += 3.0
        opportunity_points += pts

    opportunity_points = min(35.0, opportunity_points)
    if not opportunity_rows:
        explanation.append("+0: No specific digital improvement opportunities identified.")

    # ── Factor D: Service Fit with NexAgentLab Offerings (Max 20 pts) ──
    core_agency_services = {
        "website_design",
        "technical_seo",
        "local_seo",
        "booking_system",
        "chatbot",
        "lead_capture",
        "automation",
    }
    matching_services = categories_found.intersection(core_agency_services)
    if matching_services:
        fit_points = min(20.0, 10.0 + (len(matching_services) - 1) * 5.0)
        explanation.append(
            f"+{fit_points:.0f}: Opportunity areas match NexAgentLab core agency services ({', '.join(matching_services)})."
        )
    else:
        explanation.append("+0: No direct match with core NexAgentLab service packages.")

    # Total score calculation
    raw_score = contact_points + evidence_points + opportunity_points + fit_points
    score = max(0.0, min(100.0, round(raw_score, 1)))

    # Determine priority category & next action
    if score >= 70.0:
        priority = "high"
        eligible = True
        top_cats = ", ".join(list(categories_found)[:2]) if categories_found else "digital improvement"
        recommended_action = f"Generate personalized outreach proposing {top_cats} modernization"
        confidence = 0.85
    elif score >= 45.0:
        priority = "medium"
        eligible = True
        recommended_action = "Draft personalized value proposition email based on observed evidence"
        confidence = 0.70
    elif score >= 25.0:
        priority = "low"
        eligible = False
        recommended_action = "Perform manual review or enrich contact info before outreach"
        confidence = 0.55
    else:
        priority = "unqualified"
        eligible = False
        recommended_action = "Disqualify from active outreach — insufficient signal or fit"
        confidence = 0.80

    return QualificationResult(
        lead_id=lead_id,
        business_name=biz_name,
        email=email,
        score=score,
        priority=priority,
        eligible=eligible,
        recommended_action=recommended_action,
        explanation=explanation,
        confidence=confidence,
    )


# ── Orchestration & DB persistence ───────────────────────────────────


def qualify_lead(
    lead_row: sqlite3.Row,
    conn: sqlite3.Connection,
) -> QualificationResult:
    """Qualify a single lead and update its score and status in the DB."""
    lead_id = lead_row["id"]
    email = (lead_row["email"] or "").strip().lower()

    # Check suppression
    is_suppressed = bool(
        conn.execute(
            "SELECT 1 FROM suppression WHERE email = ?",
            (email,),
        ).fetchone()
    )

    # Check research
    research_row = conn.execute(
        "SELECT * FROM research WHERE lead_id = ? AND agent_type = 'website_researcher' ORDER BY created_at DESC LIMIT 1",
        (lead_id,),
    ).fetchone()

    # Check opportunities
    opportunity_rows = conn.execute(
        "SELECT * FROM opportunities WHERE lead_id = ? ORDER BY priority ASC, confidence DESC",
        (lead_id,),
    ).fetchall()

    result = evaluate_lead_qualification(
        lead_row,
        research_row,
        opportunity_rows,
        is_suppressed,
    )

    # Update leads table
    new_status = lead_row["status"]
    if result.priority == "suppressed":
        new_status = "suppressed"
    elif result.eligible and result.score >= 45.0:
        new_status = "qualified"
    elif not result.eligible and result.priority == "unqualified":
        new_status = "unqualified"

    with transaction(conn):
        conn.execute(
            """UPDATE leads SET
               score = ?,
               status = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (result.score, new_status, lead_id),
        )

        audit_log(
            conn,
            "qualification",
            lead_id,
            "lead_qualified",
            json.dumps({
                "score": result.score,
                "priority": result.priority,
                "eligible": result.eligible,
                "recommended_action": result.recommended_action,
                "scoring_version": result.scoring_version,
            }),
        )

    return result


def run_qualification_batch(
    conn: sqlite3.Connection,
    limit: int = 50,
    lead_ids: list[int] | None = None,
    force: bool = False,
) -> QualificationSummary:
    """Run qualification over a batch of researched leads."""
    import time
    start_time = time.monotonic()
    summary = QualificationSummary()

    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        rows = conn.execute(
            f"SELECT * FROM leads WHERE id IN ({placeholders}) LIMIT ?",
            (*lead_ids, limit),
        ).fetchall()
    elif force:
        rows = conn.execute(
            "SELECT * FROM leads ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        # Default: qualify leads that have been researched or imported and haven't been scored yet
        rows = conn.execute(
            "SELECT * FROM leads WHERE status IN ('researched', 'imported') AND (score IS NULL OR score = 0) ORDER BY id ASC LIMIT ?",
            (limit,),
        ).fetchall()

    summary.total = len(rows)

    for row in rows:
        try:
            res = qualify_lead(row, conn)
            if res.priority == "suppressed":
                summary.suppressed += 1
            elif res.eligible:
                summary.qualified += 1
            else:
                summary.unqualified += 1
        except Exception:
            logger.exception("Failed to qualify lead %d", row["id"])
            summary.skipped += 1

    summary.duration_seconds = round(time.monotonic() - start_time, 3)
    return summary


def get_lead_qualification(conn: sqlite3.Connection, lead_id: int) -> dict | None:
    """Retrieve the latest qualification data and explanation for a lead."""
    lead = conn.execute(
        "SELECT * FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()

    if not lead:
        return None

    audit_entry = conn.execute(
        "SELECT * FROM audit_log WHERE entity_type = 'qualification' AND entity_id = ? ORDER BY created_at DESC LIMIT 1",
        (lead_id,),
    ).fetchone()

    details = json.loads(audit_entry["details"]) if audit_entry and audit_entry["details"] else {}

    return {
        "lead_id": lead["id"],
        "business_name": lead["business_name"],
        "email": lead["email"],
        "status": lead["status"],
        "score": lead["score"],
        "priority": details.get("priority", "unqualified" if (lead["score"] or 0) < 45 else "medium"),
        "eligible": details.get("eligible", (lead["score"] or 0) >= 45),
        "recommended_action": details.get("recommended_action", "Draft personalized outreach"),
        "qualified_at": audit_entry["created_at"] if audit_entry else lead["updated_at"],
    }


def get_qualification_status(conn: sqlite3.Connection) -> dict:
    """Return aggregate qualification statistics across all leads."""
    total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
    qualified = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'qualified'").fetchone()[0]
    unqualified = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'unqualified'").fetchone()[0]
    suppressed = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'suppressed'").fetchone()[0]
    scored = conn.execute("SELECT COUNT(*) FROM leads WHERE score IS NOT NULL").fetchone()[0]

    return {
        "total_leads": total,
        "scored_leads": scored,
        "qualified_leads": qualified,
        "unqualified_leads": unqualified,
        "suppressed_leads": suppressed,
    }
