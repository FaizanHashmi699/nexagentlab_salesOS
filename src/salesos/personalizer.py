"""Personalization Agent & Quality Gate for NexAgentLab SalesOS.

Generates concise, evidence-grounded cold outreach emails for qualified leads.
Uses either an active hosted AI provider (Gemini, Groq, OpenRouter) or
high-quality deterministic templates. Enforces a strict Quality Gate that
rejects unsupported claims, fake guarantees, and missing opt-outs before
persisting drafts into the ``messages`` table.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Literal, Optional

import sqlite3
from pydantic import BaseModel, Field

from .ai_provider import generate_email_draft_ai, get_provider_status
from .db import audit_log, transaction
from .service_matcher import match_services_for_lead

logger = logging.getLogger(__name__)

# Forbidden marketing buzzwords, fake scarcity, and false guarantees
_FORBIDDEN_PHRASES = [
    r"(?i)guarantee(d)?\s+(#1|first\s+page|100%|results)",
    r"(?i)100%\s+money\s+back",
    r"(?i)only\s+\d+\s+spots\s+left",
    r"(?i)act\s+now\s+before\s+it'?s\s+too\s+late",
    r"(?i)secret\s+formula",
    r"(?i)we\s+helped\s+a\s+similar\s+client\s+make\s+\$[0-9,]+",
    r"(?i)risk-free\s+trial",
]


# ── Pydantic models ──────────────────────────────────────────────────


class EmailDraft(BaseModel):
    """Generated email outreach draft."""

    lead_id: int
    business_name: str
    recipient: str
    contact_name: Optional[str] = None
    subject: str
    body: str
    selected_service: str
    service_title: str
    qualification_score: float = 0.0
    evidence_references: list[str] = Field(default_factory=list)
    provider: str = "template"
    model: Optional[str] = None
    quality_status: Literal["approved", "rejected", "manual_review"] = "approved"
    validation_errors: list[str] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class PersonalizationSummary(BaseModel):
    """Batch run summary for email personalization."""

    total: int = 0
    approved: int = 0
    rejected: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0


# ── Deterministic Template Generator ─────────────────────────────────


def generate_template_email(
    business_name: str,
    contact_name: str | None,
    website: str | None,
    service_key: str,
    evidence_refs: list[str],
) -> tuple[str, str]:
    """Generate an authentic, grounded email using deterministic templates."""
    greeting = f"Hi {contact_name}," if contact_name and contact_name.strip() else f"Hi {business_name} Team,"
    site_label = website.replace("https://", "").replace("http://", "").rstrip("/") if website else "your website"

    # Specific evidence-grounded hooks
    if service_key == "booking_system":
        subject = f"Online booking for {business_name}"
        observation = (
            f"While reviewing {site_label}, I noticed you don't currently have an online booking or "
            f"scheduling option for customers to set appointments directly."
        )
        benefit = (
            "We frequently help service businesses implement self-serve scheduling tools that let "
            "prospective clients book directly into your calendar without the typical back-and-forth emails."
        )
    elif service_key == "chatbot":
        subject = f"Quick question about {business_name}'s website inquiries"
        observation = (
            f"While looking through {site_label}, I noticed there is no live chat or automated assistant "
            f"to answer customer questions or capture inquiries outside standard operating hours."
        )
        benefit = (
            "An integrated virtual assistant can engage website visitors 24/7, answer common questions "
            "about your services, and capture qualified leads while your team is busy."
        )
    elif service_key == "whatsapp_integration":
        subject = f"WhatsApp chat option for {business_name}"
        observation = (
            f"While reviewing {site_label}, I noticed your mobile visitors don't have a direct WhatsApp "
            f"click-to-chat option to reach your team immediately."
        )
        benefit = (
            "Adding a direct WhatsApp channel often makes it much easier for mobile clients to initiate "
            "conversations and ask quick questions before booking."
        )
    elif service_key == "lead_capture":
        subject = f"Inbound lead capture for {business_name}"
        observation = (
            f"While exploring {site_label}, I noticed an opportunity to streamline your contact and "
            f"inquiry capture process so visitors can reach you in just a couple of clicks."
        )
        benefit = (
            "Setting up high-converting capture forms with automated lead alerts ensures that every interested "
            "prospect receives a prompt response before they explore other options."
        )
    elif service_key == "technical_seo":
        subject = f"Search discoverability for {business_name}"
        observation = (
            f"While analyzing {site_label}, I noticed your homepage appears to be missing a search meta description "
            f"tag, which is foundational for how your business appears in Google search results."
        )
        benefit = (
            "Resolving these technical search signals helps ensure prospective clients see a clear, engaging summary "
            "when searching for your services online."
        )
    else:
        subject = f"Digital improvements for {business_name}"
        observation = (
            f"I spent some time reviewing {site_label} and noticed a few practical opportunities to modernize "
            f"your digital workflow and user experience."
        )
        benefit = (
            "At NexAgentLab, we help businesses implement automated workflows and modern web capabilities "
            "that make it effortless for new clients to connect with you."
        )

    body = (
        f"{greeting}\n\n"
        f"{observation}\n\n"
        f"{benefit}\n\n"
        f"Would you be open to a brief 5-minute conversation next week to see if this could be helpful for {business_name}?\n\n"
        f"Best regards,\n"
        f"Saif\n"
        f"NexAgentLab | nexagentlab.tech\n"
        f"nexagentlab.tech@gmail.com\n\n"
        f"P.S. If you'd prefer not to receive future emails from us, simply reply with 'unsubscribe' and we will remove you immediately."
    )

    return subject, body


# ── Quality Gate Validation ──────────────────────────────────────────


def validate_email_draft(
    draft: EmailDraft,
    is_suppressed: bool = False,
) -> tuple[bool, list[str]]:
    """Strict Quality Gate that validates an email draft before approval.

    Returns ``(is_valid, rejection_reasons)``.
    """
    reasons: list[str] = []

    # 1. Suppression check
    if is_suppressed:
        reasons.append("Recipient email is registered on the suppression or opt-out list.")

    # 2. Recipient email validation
    email = (draft.recipient or "").strip().lower()
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        reasons.append(f"Invalid or missing recipient email address: '{draft.recipient}'.")

    # 3. Subject checks
    subj = (draft.subject or "").strip()
    if not subj:
        reasons.append("Subject line is empty.")
    elif len(subj) > 100:
        reasons.append(f"Subject line is too long ({len(subj)} chars, max 100).")
    elif len(subj) < 5:
        reasons.append("Subject line is too short.")

    # 4. Body checks
    body = (draft.body or "").strip()
    words = body.split()
    word_count = len(words)
    if word_count < 35:
        reasons.append(f"Email body is too short ({word_count} words, minimum 35).")
    elif word_count > 350:
        reasons.append(f"Email body is too long ({word_count} words, maximum 350).")

    # 5. Opt-out mechanism presence
    body_lower = body.lower()
    if "unsubscribe" not in body_lower and "opt-out" not in body_lower and "opt out" not in body_lower:
        reasons.append("Email lacks an explicit opt-out mechanism (must contain 'unsubscribe' or 'opt-out').")

    # 6. Sender identity
    if "nexagentlab" not in body_lower:
        reasons.append("Email lacks honest sender identity (must identify NexAgentLab).")

    # 7. Prohibition of fake guarantees / spam triggers
    for pattern in _FORBIDDEN_PHRASES:
        if re.search(pattern, body):
            reasons.append(f"Body contains prohibited marketing claim/guarantee matching '{pattern}'.")

    # 8. Business reference
    biz_clean = draft.business_name.lower().strip()
    if biz_clean and len(biz_clean) > 3:
        # Check if business name appears in subject or body
        if biz_clean not in body_lower and biz_clean not in subj.lower():
            reasons.append(f"Draft does not reference the prospective business name ('{draft.business_name}').")

    is_valid = len(reasons) == 0
    return is_valid, reasons


# ── Single Lead Personalization ──────────────────────────────────────


def personalize_lead(
    lead_row: sqlite3.Row,
    conn: sqlite3.Connection,
    use_ai: bool = True,
    force: bool = False,
) -> EmailDraft:
    """Generate and validate a personalized email draft for a lead."""
    lead_id = lead_row["id"]
    biz_name = lead_row["business_name"] or "Unknown Business"
    recipient = (lead_row["email"] or "").strip()
    contact_name = lead_row["contact_name"]
    website = lead_row["website"]
    score = lead_row["score"] or 0.0

    # 1. Duplicate draft check
    existing_msg = conn.execute(
        "SELECT * FROM messages WHERE lead_id = ? AND message_type = 'initial'",
        (lead_id,),
    ).fetchone()
    if existing_msg and not force:
        # Return existing draft
        return EmailDraft(
            lead_id=lead_id,
            business_name=biz_name,
            recipient=recipient,
            contact_name=contact_name,
            subject=existing_msg["subject"] or "",
            body=existing_msg["body"] or "",
            selected_service=existing_msg["selected_service"] or "general",
            service_title=existing_msg["selected_service"] or "Digital Improvement",
            qualification_score=existing_msg["qualification_score"] or score,
            provider=existing_msg["provider"] or "existing",
            quality_status=existing_msg["quality_status"] or "approved",
            validation_errors=json.loads(existing_msg["validation_errors"]) if existing_msg["validation_errors"] else [],
        )

    # 2. Check suppression
    is_suppressed = bool(
        conn.execute("SELECT 1 FROM suppression WHERE email = ?", (recipient.lower(),)).fetchone()
    )

    # 3. Retrieve research and opportunities
    research_row = conn.execute(
        "SELECT * FROM research WHERE lead_id = ? AND agent_type = 'website_researcher' ORDER BY created_at DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    evidence_dict = json.loads(research_row["findings"]) if research_row and research_row["findings"] else {}

    opp_rows = conn.execute(
        "SELECT * FROM opportunities WHERE lead_id = ? ORDER BY priority ASC, confidence DESC",
        (lead_id,),
    ).fetchall()
    opps = [dict(o) for o in opp_rows]

    # 4. Match services
    service_match = match_services_for_lead(lead_id, biz_name, opps, evidence_dict)

    # 5. Generate content (AI or Template)
    subject = ""
    body = ""
    provider_used = "template"
    model_used = None

    if use_ai:
        ai_status = get_provider_status()
        if ai_status.available:
            lead_context = {
                "business_name": biz_name,
                "contact_name": contact_name,
                "website": website,
                "location": lead_row["location"],
            }
            ai_res = generate_email_draft_ai(
                lead_context,
                service_match.model_dump(),
            )
            if ai_res.success and ai_res.structured:
                subject = ai_res.structured.get("subject", "")
                body = ai_res.structured.get("body", "")
                provider_used = ai_res.provider
                model_used = ai_res.model

    if not subject or not body:
        # Template fallback
        subject, body = generate_template_email(
            biz_name,
            contact_name,
            website,
            service_match.primary_service,
            service_match.evidence_references,
        )
        provider_used = "template"
        model_used = None

    # 6. Quality Gate
    draft = EmailDraft(
        lead_id=lead_id,
        business_name=biz_name,
        recipient=recipient,
        contact_name=contact_name,
        subject=subject,
        body=body,
        selected_service=service_match.primary_service,
        service_title=service_match.service_title,
        qualification_score=score,
        evidence_references=service_match.evidence_references,
        provider=provider_used,
        model=model_used,
    )

    is_valid, validation_errors = validate_email_draft(draft, is_suppressed=is_suppressed)
    draft.quality_status = "approved" if is_valid else "rejected"
    draft.validation_errors = validation_errors

    # 7. Persist to DB
    with transaction(conn):
        if existing_msg and force:
            conn.execute(
                """UPDATE messages SET
                   recipient = ?,
                   subject = ?,
                   body = ?,
                   selected_service = ?,
                   qualification_score = ?,
                   evidence_references = ?,
                   provider = ?,
                   model = ?,
                   quality_status = ?,
                   validation_errors = ?,
                   status = 'draft'
                   WHERE id = ?""",
                (
                    draft.recipient,
                    draft.subject,
                    draft.body,
                    draft.selected_service,
                    draft.qualification_score,
                    json.dumps(draft.evidence_references),
                    draft.provider,
                    draft.model,
                    draft.quality_status,
                    json.dumps(draft.validation_errors),
                    existing_msg["id"],
                ),
            )
            msg_id = existing_msg["id"]
        else:
            cursor = conn.execute(
                """INSERT INTO messages
                   (lead_id, recipient, subject, body, message_type, status,
                    quality_status, selected_service, qualification_score,
                    evidence_references, provider, model, validation_errors)
                   VALUES (?, ?, ?, ?, 'initial', 'draft', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    draft.lead_id,
                    draft.recipient,
                    draft.subject,
                    draft.body,
                    draft.quality_status,
                    draft.selected_service,
                    draft.qualification_score,
                    json.dumps(draft.evidence_references),
                    draft.provider,
                    draft.model,
                    json.dumps(draft.validation_errors),
                ),
            )
            msg_id = cursor.lastrowid

        # Update lead status if approved
        if draft.quality_status == "approved":
            conn.execute(
                "UPDATE leads SET status = 'ready_for_outreach', updated_at = datetime('now') WHERE id = ?",
                (lead_id,),
            )

        audit_log(
            conn,
            "message",
            msg_id,
            "draft_created" if draft.quality_status == "approved" else "draft_rejected",
            json.dumps({
                "lead_id": lead_id,
                "recipient": recipient,
                "quality_status": draft.quality_status,
                "provider": draft.provider,
                "errors": draft.validation_errors,
            }),
        )

    return draft


# ── Batch Personalization ────────────────────────────────────────────


def run_personalization_batch(
    conn: sqlite3.Connection,
    limit: int = 20,
    use_ai: bool = True,
    force: bool = False,
    lead_ids: list[int] | None = None,
) -> PersonalizationSummary:
    """Personalize email drafts for eligible qualified leads."""
    start_time = time.monotonic()
    summary = PersonalizationSummary()

    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        rows = conn.execute(
            f"SELECT * FROM leads WHERE id IN ({placeholders}) LIMIT ?",
            (*lead_ids, limit),
        ).fetchall()
    elif force:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status IN ('qualified', 'ready_for_outreach', 'researched') ORDER BY score DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        # Default: qualify leads with status='qualified' that do NOT already have a message
        rows = conn.execute(
            """SELECT l.* FROM leads l
               LEFT JOIN messages m ON m.lead_id = l.id AND m.message_type = 'initial'
               WHERE l.status IN ('qualified', 'researched') AND m.id IS NULL
               ORDER BY l.score DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    summary.total = len(rows)

    for row in rows:
        try:
            draft = personalize_lead(row, conn, use_ai=use_ai, force=force)
            if draft.quality_status == "approved":
                summary.approved += 1
            else:
                summary.rejected += 1
        except Exception:
            logger.exception("Failed to personalize draft for lead %d", row["id"])
            summary.skipped += 1

    summary.duration_seconds = round(time.monotonic() - start_time, 3)
    return summary


# ── Draft queries & exports ──────────────────────────────────────────


def get_drafts(
    conn: sqlite3.Connection,
    status_filter: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieve message drafts with associated lead details."""
    query = (
        "SELECT m.*, l.business_name, l.contact_name, l.location, l.website "
        "FROM messages m "
        "JOIN leads l ON l.id = m.lead_id "
        "WHERE 1=1"
    )
    params: list = []
    if status_filter and status_filter != "all":
        query += " AND m.quality_status = ?"
        params.append(status_filter)

    query += " ORDER BY m.id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        if item.get("evidence_references"):
            try:
                item["evidence_references"] = json.loads(item["evidence_references"])
            except Exception:
                pass
        if item.get("validation_errors"):
            try:
                item["validation_errors"] = json.loads(item["validation_errors"])
            except Exception:
                pass
        results.append(item)
    return results


def export_drafts(
    conn: sqlite3.Connection,
    output_path: str,
    fmt: str = "csv",
    status_filter: str | None = None,
) -> int:
    """Export drafts to a CSV or JSON file. Returns count of exported rows."""
    drafts = get_drafts(conn, status_filter=status_filter, limit=1000)
    if not drafts:
        return 0

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    if fmt == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(drafts, f, indent=2)
    else:
        fieldnames = [
            "id", "lead_id", "business_name", "recipient", "contact_name",
            "subject", "body", "selected_service", "qualification_score",
            "provider", "quality_status", "created_at",
        ]
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for d in drafts:
                writer.writerow(d)

    return len(drafts)
