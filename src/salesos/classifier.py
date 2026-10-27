"""Reply Classification Agent & Suggested Response Drafter for NexAgentLab SalesOS.

Analyzes incoming emails using explainable deterministic heuristics combined with
optional multi-provider LLM classification. Identifies intent, confidence, and
immediate compliance triggers (opt-out suppression, bounce handling, follow-up cancellation).
Generates safe, grounded suggested reply drafts for human review.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from .ai_provider import _call_llm_provider, get_provider_status, sanitize_untrusted_text
from .db import audit_log, transaction
from .suppression import record_suppression

logger = logging.getLogger(__name__)

CLASSIFICATION_CATEGORIES = (
    "positive_interest",
    "meeting_request",
    "question",
    "objection",
    "not_interested",
    "unsubscribe",
    "out_of_office",
    "bounce_or_delivery_failure",
    "ambiguous",
    "unrelated",
)


@dataclass
class ClassificationResult:
    """Structured evaluation of an inbound reply."""

    category: str
    confidence: float
    explanation: str
    needs_review: bool
    evidence_references: list[str] = field(default_factory=list)
    classifier_version: str = "rules_v1"
    suggested_reply: Optional[str] = None
    meeting_details: Optional[dict] = None


# ── Deterministic Rule Heuristics ─────────────────────────────────────


_UNSUBSCRIBE_PATTERNS = [
    r"\bunsubscribe\b",
    r"\bopt[\s-]?out\b",
    r"\bremove\s+me\b",
    r"\btake\s+me\s+off\b",
    r"\bstop\s+emailing\b",
    r"\bdo\s+not\s+(email|contact)\b",
    r"\bnever\s+contact\b",
    r"\bplease\s+delete\b",
]

_BOUNCE_PATTERNS = [
    r"\bmailer-daemon\b",
    r"\bdelivery\s+status\s+notification\b",
    r"\bundeliverable\b",
    r"\b550\s+5\.\d\.\d\b",
    r"\buser\s+unknown\b",
    r"\bmailbox\s+unavailable\b",
    r"\baddress\s+not\s+found\b",
    r"\bpermanent\s+failure\b",
]

_OOO_PATTERNS = [
    r"\bout\s+of\s+(the\s+)?office\b",
    r"\bautomatic\s+reply\b",
    r"\bauto-reply\b",
    r"\bon\s+(vacation|leave|pto)\b",
    r"\baway\s+from\s+(my\s+)?(desk|office|email)\b",
    r"\bi\s+will\s+return\s+on\b",
    r"\blimited\s+access\s+to\s+email\b",
]

_MEETING_PATTERNS = [
    r"\b(call|talk|chat|meet)\s+(today|tomorrow|this\s+week|next\s+week|monday|tuesday|wednesday|thursday|friday)\b",
    r"\blet'?s\s+(hop\s+on\s+a\s+call|talk|chat|meet|schedule|connect)\b",
    r"\bfree\s+(at|on|tomorrow|next\s+week)\b",
    r"\bsend\s+(me\s+)?(a\s+)?(calendar|invite|link|calendly)\b",
    r"\bwhat('?s|\s+is)\s+your\s+availability\b",
    r"\bavailable\s+at\b",
    r"\bhow\s+about\s+\d{1,2}(:\d{2})?\s*(am|pm)?\b",
]

_POSITIVE_PATTERNS = [
    r"\bsounds?\s+(great|good|interesting|promising)\b",
    r"\bi('?m|\s+am)\s+interested\b",
    r"\bwe('?re|\s+are)\s+interested\b",
    r"\btell\s+me\s+more\b",
    r"\bsend\s+(more\s+)?(details|info|information)\b",
    r"\bwhat\s+are\s+your\s+(rates|prices|packages)\b",
    r"\bwould\s+love\s+to\s+(hear|learn)\s+more\b",
    r"\bdefinitely\s+interested\b",
]

_NOT_INTERESTED_PATTERNS = [
    r"\bnot\s+interested\b",
    r"\bno\s+thank\s*you\b",
    r"\bno\s+thanks\b",
    r"\bpass\s+on\s+this\b",
    r"\bwe('?re|\s+are)\s+(all\s+)?set\b",
    r"\bwe('?re|\s+are)\s+good\b",
    r"\bnot\s+looking\s+for\b",
    r"\bplease\s+don'?t\s+reach\s+out\b",
]

_OBJECTION_PATTERNS = [
    r"\btoo\s+expensive\b",
    r"\bno\s+budget\b",
    r"\balready\s+have\s+(an?\s+)?(web|seo|developer|agency|team|partner)\b",
    r"\bwe\s+handle\s+this\s+in[\s-]house\b",
    r"\bnot\s+a\s+priority\s+(right\s+now|at\s+this\s+time)\b",
    r"\btoo\s+busy\s+(right\s+now|at\s+the\s+moment)\b",
    r"\bmaybe\s+(next\s+quarter|later\s+this\s+year)\b",
]

_QUESTION_PATTERNS = [
    r"\bhow\s+(much|does\s+it\s+work|do\s+you)\b",
    r"\bwhat\s+(is|are|services|technologies)\b",
    r"\bdo\s+you\s+(support|offer|handle|integrate|work\s+with)\b",
    r"\bwhere\s+are\s+you\s+located\b",
    r"\?",
]


def classify_text_rules(subject: str, body: str, from_email: str) -> Optional[ClassificationResult]:
    """Fast deterministic rule classifier for high-certainty reply types."""
    combined = f"{subject}\n{body}".lower()
    from_lower = from_email.lower()

    # 1. Bounce / delivery failure check (highest precedence)
    if "mailer-daemon" in from_lower or "postmaster" in from_lower:
        return ClassificationResult(
            category="bounce_or_delivery_failure",
            confidence=0.98,
            explanation="Automated Mailer-Daemon address detected.",
            needs_review=False,
            evidence_references=["Sender is Mailer-Daemon / Postmaster"],
            classifier_version="rules_v1",
        )

    for pat in _BOUNCE_PATTERNS:
        match = re.search(pat, combined)
        if match:
            return ClassificationResult(
                category="bounce_or_delivery_failure",
                confidence=0.95,
                explanation=f"Delivery failure keyword identified: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
            )

    # 2. Unsubscribe / opt-out check (strict compliance)
    for pat in _UNSUBSCRIBE_PATTERNS:
        match = re.search(pat, combined)
        if match:
            return ClassificationResult(
                category="unsubscribe",
                confidence=0.98,
                explanation=f"Explicit unsubscribe or opt-out phrase identified: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
            )

    # 3. Out-of-office automated replies
    for pat in _OOO_PATTERNS:
        match = re.search(pat, combined)
        if match:
            return ClassificationResult(
                category="out_of_office",
                confidence=0.95,
                explanation=f"Out-of-office autoresponder detected: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
            )

    # 4. Explicit Not Interested
    for pat in _NOT_INTERESTED_PATTERNS:
        match = re.search(pat, combined)
        if match:
            return ClassificationResult(
                category="not_interested",
                confidence=0.92,
                explanation=f"Polite or explicit decline statement detected: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
            )

    # 5. Meeting request / scheduling intent
    for pat in _MEETING_PATTERNS:
        match = re.search(pat, combined)
        if match:
            # Extract basic time indications
            meeting_details = {}
            time_match = re.search(r"\b(\d{1,2}(:\d{2})?\s*(am|pm)?)\b", combined)
            if time_match:
                meeting_details["proposed_time"] = time_match.group(0)

            return ClassificationResult(
                category="meeting_request",
                confidence=0.90,
                explanation=f"Meeting or call scheduling phrase detected: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
                meeting_details=meeting_details if meeting_details else None,
            )

    # 6. Positive interest
    for pat in _POSITIVE_PATTERNS:
        match = re.search(pat, combined)
        if match:
            return ClassificationResult(
                category="positive_interest",
                confidence=0.88,
                explanation=f"Positive reception or inquiry for info detected: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
            )

    # 7. Objection
    for pat in _OBJECTION_PATTERNS:
        match = re.search(pat, combined)
        if match:
            return ClassificationResult(
                category="objection",
                confidence=0.85,
                explanation=f"Business timing, budget, or vendor objection detected: '{match.group(0)}'",
                needs_review=False,
                evidence_references=[match.group(0)],
                classifier_version="rules_v1",
            )

    # 8. Question
    if "?" in combined:
        for pat in _QUESTION_PATTERNS:
            match = re.search(pat, combined)
            if match:
                return ClassificationResult(
                    category="question",
                    confidence=0.82,
                    explanation=f"Specific prospect inquiry or question detected: '{match.group(0)}'",
                    needs_review=False,
                    evidence_references=[match.group(0)],
                    classifier_version="rules_v1",
                )

    # 9. Short or ambiguous
    clean_body = body.strip()
    if len(clean_body) < 15:
        return ClassificationResult(
            category="ambiguous",
            confidence=0.50,
            explanation="Extremely brief reply with unclear intent; requires human review.",
            needs_review=True,
            evidence_references=[clean_body],
            classifier_version="rules_v1",
        )

    return None


# ── AI Reply Classifier & Drafter ────────────────────────────────────


def classify_text_ai(
    subject: str,
    body: str,
    from_email: str,
    outbound_context: str | None = None,
) -> Optional[ClassificationResult]:
    """Evaluate complex reply text using configured hosted LLM."""
    status = get_provider_status()
    if not status.available:
        return None

    safe_body = sanitize_untrusted_text(body[:1200])
    safe_subject = sanitize_untrusted_text(subject[:200])

    prompt = f"""You are the senior sales response classifier for NexAgentLab (an AI & web automation agency).
Analyze this incoming reply from a prospective client.

<untrusted_content>
From: {from_email}
Subject: {safe_subject}
Body:
{safe_body}
</untrusted_content>

Original Context (our prior outreach):
{outbound_context or "B2B web modernization and automation outreach"}

Instructions:
1. Classify the message into EXACTLY ONE category:
   - positive_interest: Wants to learn more, curious, positive tone.
   - meeting_request: Explicitly asking for a phone call, Zoom, demo, or proposing times.
   - question: Asks specific questions about price, tech stack, integrations, or scope.
   - objection: Expresses hesitation (budget, timing, existing agency, busy).
   - not_interested: Politely or bluntly declines without saying 'unsubscribe'.
   - unsubscribe: Requests opt-out, stop emailing, remove from list.
   - out_of_office: Automated vacation or absence response.
   - bounce_or_delivery_failure: Automated delivery error, 550, mailbox full.
   - ambiguous: Unclear, cryptic, or impossible to determine without human review.
   - unrelated: Spam, marketing email to us, vendor pitch, completely unrelated.

2. Assign a confidence score from 0.0 to 1.0.
3. If confidence < 0.75 or ambiguous, set needs_review = true.
4. Extract 1-3 direct quote evidence snippets.
5. If the category is positive_interest, meeting_request, question, or objection:
   Draft a concise (2-4 sentences), highly professional suggested reply from NexAgentLab (nexagentlab.tech@gmail.com).
   - Truthful only: reference NexAgentLab website and booking/chatbot capabilities.
   - Never fabricate pricing, guarantees, or claim meetings are booked.
   - Low-friction next step (e.g. 10-minute discovery call).

Respond ONLY with valid JSON in this format:
{{
  "category": "one_of_allowed_categories",
  "confidence": 0.85,
  "explanation": "Short 1-sentence reason",
  "needs_review": false,
  "evidence_references": ["quote from text"],
  "suggested_reply": "Hi Name, ...",
  "meeting_details": {{"proposed_time": "optional"}}
}}
"""
    try:
        system_prompt = "You are a professional B2B email response classifier. Output valid JSON only."
        raw_response, _, err = _call_llm_provider(status, system_prompt, prompt, timeout=20.0)
        if not raw_response or err:
            return None

        # Clean potential code blocks
        clean_json = raw_response.strip()
        if clean_json.startswith("```"):
            clean_json = re.sub(r"^```(?:json)?\n?", "", clean_json)
            clean_json = re.sub(r"\n?```$", "", clean_json)

        data = json.loads(clean_json)
        cat = data.get("category", "ambiguous")
        if cat not in CLASSIFICATION_CATEGORIES:
            cat = "ambiguous"

        return ClassificationResult(
            category=cat,
            confidence=float(data.get("confidence", 0.7)),
            explanation=data.get("explanation", "AI semantic classification"),
            needs_review=bool(data.get("needs_review", cat == "ambiguous")),
            evidence_references=data.get("evidence_references", []),
            classifier_version=f"{status.provider}_{status.model}",
            suggested_reply=data.get("suggested_reply"),
            meeting_details=data.get("meeting_details"),
        )
    except Exception as e:
        logger.warning("AI classification failed: %s. Falling back to rules.", e)
        return None


# ── Deterministic Suggested Reply Generator ──────────────────────────


def generate_suggested_reply(
    category: str,
    from_name: str | None,
    business_name: str | None,
    service_name: str | None = None,
) -> Optional[str]:
    """Produce a concise, professional suggested reply draft when AI is offline."""
    greeting = f"Hi {from_name}," if from_name and from_name.strip() else "Hi there,"
    biz_label = f" for {business_name}" if business_name else ""

    if category == "meeting_request":
        return (
            f"{greeting}\n\n"
            f"Thanks for getting back to me! I would be glad to connect and discuss how we can assist{biz_label}.\n\n"
            f"Would you be open to a quick 10-15 minute introductory call? "
            f"Let me know what time window works best for you this week, or feel free to pick a slot that fits your schedule.\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"nexagentlab.tech@gmail.com"
        )
    elif category == "positive_interest":
        return (
            f"{greeting}\n\n"
            f"Thank you for your interest! We help businesses implement practical digital modernization{biz_label}, "
            f"focusing on seamless lead capture, automated customer inquiries, and booking workflows.\n\n"
            f"Would you have 10 minutes later this week for a brief conversation to explore if this fits your current priorities?\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"nexagentlab.tech@gmail.com"
        )
    elif category == "question":
        return (
            f"{greeting}\n\n"
            f"Thanks for reaching out with your question. We specialize in custom web solutions and automation tools tailored to your existing workflow.\n\n"
            f"I would be happy to share more specifics or hop on a brief call to go over the details directly. What works best for you?\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"nexagentlab.tech@gmail.com"
        )
    elif category == "objection":
        return (
            f"{greeting}\n\n"
            f"I completely understand, and I appreciate you letting me know your current priorities. Timing is everything with these projects.\n\n"
            f"I will pause our outreach for now. If your team ever revisits digital workflows or website upgrades in the future, please feel free to reach back out.\n\n"
            f"Best regards,\n"
            f"NexAgentLab Team\n"
            f"nexagentlab.tech@gmail.com"
        )

    return None


# ── Classification Orchestration & Safety Actions ────────────────────


def classify_reply(
    conn: sqlite3.Connection,
    reply_id: int,
    use_ai: bool = True,
) -> ClassificationResult:
    """Classify a single stored reply and execute mandatory safety triggers."""
    row = conn.execute(
        """SELECT r.*, l.business_name, l.contact_name, m.subject as outbound_subject, m.selected_service
           FROM replies r
           LEFT JOIN leads l ON r.lead_id = l.id
           LEFT JOIN messages m ON r.message_id = m.id
           WHERE r.id = ?""",
        (reply_id,),
    ).fetchone()

    if not row:
        raise ValueError(f"Reply ID {reply_id} not found.")

    subject = row["subject"] or ""
    body = row["body"] or ""
    from_email = row["from_email"] or ""
    outbound_ctx = f"Subject: {row['outbound_subject']} | Service: {row['selected_service']}"

    # 1. Deterministic rules evaluation
    result = classify_text_rules(subject, body, from_email)

    # 2. Hybrid AI if rules were inconclusive or ambiguous
    if (result is None or result.category == "ambiguous") and use_ai:
        ai_res = classify_text_ai(subject, body, from_email, outbound_context=outbound_ctx)
        if ai_res:
            result = ai_res

    # 3. Fallback to ambiguous if completely unclassified
    if result is None:
        result = ClassificationResult(
            category="ambiguous",
            confidence=0.40,
            explanation="No deterministic patterns or AI predictions matched; requires manual review.",
            needs_review=True,
            classifier_version="rules_fallback",
        )

    # 4. Generate fallback suggested reply draft if applicable and none generated
    if not result.suggested_reply and result.category in ("positive_interest", "meeting_request", "question", "objection"):
        contact_name = row["contact_name"]
        biz_name = row["business_name"]
        result.suggested_reply = generate_suggested_reply(
            result.category,
            from_name=contact_name,
            business_name=biz_name,
            service_name=row["selected_service"],
        )

    lead_id = row["lead_id"]

    # ── Execute Immediate Compliance & Safety Actions ─────────────────
    with transaction(conn):
        # Action A: Unsubscribe triggers immediate central suppression
        if result.category == "unsubscribe":
            record_suppression(
                conn,
                email=from_email,
                reason="unsubscribe",
                source="inbound_reply",
                original_lead_id=lead_id,
            )
            # Cancel all scheduled followups
            conn.execute(
                """UPDATE followups SET status = 'cancelled', cancel_reason = 'Prospect unsubscribed'
                   WHERE lead_id = ? AND status IN ('scheduled', 'planned')""",
                (lead_id,),
            )
            if lead_id:
                conn.execute(
                    "UPDATE leads SET status = 'suppressed', updated_at = datetime('now') WHERE id = ?",
                    (lead_id,),
                )

        # Action B: Delivery failure / Bounce
        elif result.category == "bounce_or_delivery_failure":
            record_suppression(
                conn,
                email=from_email,
                reason="bounce",
                source="delivery_failure",
                original_lead_id=lead_id,
            )
            if row["message_id"]:
                conn.execute(
                    "UPDATE messages SET status = 'bounced', error_message = ? WHERE id = ?",
                    (result.explanation, row["message_id"]),
                )
            conn.execute(
                """UPDATE followups SET status = 'cancelled', cancel_reason = 'Email delivery bounced'
                   WHERE lead_id = ? AND status IN ('scheduled', 'planned')""",
                (lead_id,),
            )

        # Action C: Positive response or meeting request -> Cancel cold follow-ups
        elif result.category in ("positive_interest", "meeting_request"):
            conn.execute(
                """UPDATE followups SET status = 'cancelled', cancel_reason = 'Lead replied with interest'
                   WHERE lead_id = ? AND status IN ('scheduled', 'planned')""",
                (lead_id,),
            )
            if lead_id:
                conn.execute(
                    "UPDATE leads SET status = 'interested', updated_at = datetime('now') WHERE id = ?",
                    (lead_id,),
                )

        # Action D: Decline or Objection -> Cancel pending cold follow-ups
        elif result.category in ("not_interested", "objection"):
            conn.execute(
                """UPDATE followups SET status = 'cancelled', cancel_reason = 'Lead responded to outreach'
                   WHERE lead_id = ? AND status IN ('scheduled', 'planned')""",
                (lead_id,),
            )
            if lead_id and result.category == "not_interested":
                conn.execute(
                    "UPDATE leads SET status = 'not_interested', updated_at = datetime('now') WHERE id = ?",
                    (lead_id,),
                )

        # Action E: Question -> Cancel cold follow-ups (lead engaged)
        elif result.category == "question":
            conn.execute(
                """UPDATE followups SET status = 'cancelled', cancel_reason = 'Lead asked a question'
                   WHERE lead_id = ? AND status IN ('scheduled', 'planned')""",
                (lead_id,),
            )

        # Update reply record with classification results
        conn.execute(
            """UPDATE replies SET
               classification = ?,
               confidence = ?,
               explanation = ?,
               needs_review = ?,
               classifier_version = ?,
               evidence_references = ?,
               suggested_reply = ?,
               suggested_reply_status = 'pending',
               meeting_details = ?,
               is_classified = 1,
               processed_at = datetime('now')
               WHERE id = ?""",
            (
                result.category,
                result.confidence,
                result.explanation,
                1 if result.needs_review else 0,
                result.classifier_version,
                json.dumps(result.evidence_references),
                result.suggested_reply,
                json.dumps(result.meeting_details) if result.meeting_details else None,
                reply_id,
            ),
        )

        audit_log(
            conn,
            "reply",
            reply_id,
            "reply_classified",
            json.dumps({
                "category": result.category,
                "confidence": result.confidence,
                "needs_review": result.needs_review,
                "lead_id": lead_id,
            }),
        )

    return result


def classify_pending_replies(
    conn: sqlite3.Connection,
    limit: int = 50,
    use_ai: bool = True,
) -> dict:
    """Classify all unclassified inbound replies in the database."""
    rows = conn.execute(
        """SELECT id FROM replies
           WHERE is_classified = 0 OR classification IS NULL
           ORDER BY id ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    processed = 0
    by_category: dict[str, int] = {}
    needs_review_count = 0

    for r in rows:
        rid = r["id"]
        try:
            res = classify_reply(conn, rid, use_ai=use_ai)
            processed += 1
            by_category[res.category] = by_category.get(res.category, 0) + 1
            if res.needs_review:
                needs_review_count += 1
        except Exception as e:
            logger.error("Failed to classify reply %d: %s", rid, e)

    return {
        "total_evaluated": len(rows),
        "processed": processed,
        "by_category": by_category,
        "needs_review_count": needs_review_count,
    }
