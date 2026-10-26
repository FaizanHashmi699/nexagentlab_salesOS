"""AI provider abstraction for lead research analysis and summarization.

Supports Google Gemini (via ``google-genai`` SDK) when an API key is
configured (``GEMINI_API_KEY`` or ``GOOGLE_API_KEY``).  Falls back
gracefully to non-AI operation when unconfigured or when quota is
exhausted.  Never crashes if the provider is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Default free-tier model
DEFAULT_MODEL = "gemini-2.0-flash"

# Rate limit tracking (in-memory, sliding 60-second window)
_REQUEST_TIMESTAMPS: list[float] = []
MAX_RPM = 15


# ── Pydantic models ──────────────────────────────────────────────────


class AIProviderStatus(BaseModel):
    """Runtime status of the AI provider integration."""

    provider: str  # "gemini" or "none"
    available: bool
    model: str | None = None
    reason: str | None = None


class AIAnalysisRequest(BaseModel):
    """Structured request for AI-assisted analysis."""

    task: str  # e.g., "summarize_research", "analyze_opportunities"
    context: dict
    lead_id: int


class AIAnalysisResult(BaseModel):
    """Result of an AI analysis invocation."""

    success: bool
    provider: str
    model: str | None = None
    content: str | None = None
    structured: dict | None = None
    error: str | None = None
    tokens_used: int | None = None
    duration_seconds: float = 0.0


# ── Rate limiting helper ─────────────────────────────────────────────


def _check_rate_limit() -> bool:
    """Return True if a new request is allowed under MAX_RPM, else False."""
    global _REQUEST_TIMESTAMPS
    now = time.monotonic()
    # Expire timestamps older than 60s
    _REQUEST_TIMESTAMPS = [t for t in _REQUEST_TIMESTAMPS if now - t < 60.0]
    if len(_REQUEST_TIMESTAMPS) >= MAX_RPM:
        return False
    _REQUEST_TIMESTAMPS.append(now)
    return True


# ── Provider discovery ───────────────────────────────────────────────


def get_api_key() -> str | None:
    """Retrieve Gemini API key from environment."""
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def get_provider_status() -> AIProviderStatus:
    """Inspect environment and return current AI provider availability."""
    api_key = get_api_key()
    if not api_key:
        return AIProviderStatus(
            provider="none",
            available=False,
            model=None,
            reason="No GEMINI_API_KEY or GOOGLE_API_KEY set",
        )

    try:
        import google.genai  # noqa: F401
    except ImportError:
        return AIProviderStatus(
            provider="none",
            available=False,
            model=None,
            reason="google-genai package not installed",
        )

    return AIProviderStatus(
        provider="gemini",
        available=True,
        model=DEFAULT_MODEL,
        reason=None,
    )


# ── Output validation ────────────────────────────────────────────────


def validate_ai_output(raw_response: str) -> dict | None:
    """Parse and validate JSON structured output from an LLM response.

    Strips markdown code fences (e.g. ```json ... ```) if present.
    Returns None if parsing fails.
    """
    if not raw_response or not raw_response.strip():
        return None

    cleaned = raw_response.strip()

    # Strip markdown code blocks
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        # Fallback regex search for JSON object inside text
        obj_match = re.search(r"\{[\s\S]*\}", cleaned)
        if obj_match:
            try:
                data = json.loads(obj_match.group(0))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return None


# ── Context sanitization ─────────────────────────────────────────────


def _sanitize_context(
    business_name: str,
    evidence_dict: dict,
    opportunities: list[dict],
) -> dict:
    """Strip personal contact details before sending context to LLM."""
    clean_evidence = dict(evidence_dict)
    clean_evidence.pop("contact_info", None)

    clean_opps = []
    for opp in opportunities:
        clean_opps.append({
            "category": opp.get("category"),
            "description": opp.get("description"),
            "confidence": opp.get("confidence"),
            "priority": opp.get("priority"),
            "evidence": opp.get("evidence", []),
        })

    return {
        "business_name": business_name,
        "title": clean_evidence.get("title"),
        "meta_description": clean_evidence.get("meta_description"),
        "headings": clean_evidence.get("headings", [])[:10],
        "technologies": clean_evidence.get("technologies_detected", []),
        "has_booking": clean_evidence.get("has_booking", False),
        "has_whatsapp": clean_evidence.get("has_whatsapp", False),
        "has_chat_widget": clean_evidence.get("has_chat_widget", False),
        "has_lead_form": clean_evidence.get("has_lead_form", False),
        "opportunities": clean_opps,
    }


# ── Mock analyze for testing ─────────────────────────────────────────


def _mock_analyze(
    evidence_dict: dict,
    opportunities: list[dict],
    business_name: str,
    lead_id: int,
) -> AIAnalysisResult:
    """Deterministic mock analysis for unit tests and offline testing."""
    top_opps = [opp.get("category", "") for opp in opportunities[:3]]
    structured = {
        "summary": f"{business_name} offers services with active online presence.",
        "top_opportunities": top_opps,
        "outreach_angle": f"Helping {business_name} capture and convert more inbound leads through automation.",
    }
    return AIAnalysisResult(
        success=True,
        provider="mock",
        model="mock-model",
        content=json.dumps(structured),
        structured=structured,
        tokens_used=120,
        duration_seconds=0.01,
    )


# ── Main analysis routine ────────────────────────────────────────────


def analyze_research(
    evidence_dict: dict,
    opportunities: list[dict],
    business_name: str,
    lead_id: int,
    mock: bool = False,
) -> AIAnalysisResult:
    """Analyze website evidence and opportunities to generate structured insights.

    If AI is unconfigured or unavailable, returns a graceful pending result.
    """
    start_time = time.monotonic()

    if mock:
        return _mock_analyze(evidence_dict, opportunities, business_name, lead_id)

    status = get_provider_status()
    if not status.available:
        return AIAnalysisResult(
            success=False,
            provider="none",
            error=status.reason or "AI provider unconfigured",
            duration_seconds=round(time.monotonic() - start_time, 3),
        )

    if not _check_rate_limit():
        return AIAnalysisResult(
            success=False,
            provider="gemini",
            error="Rate limit exceeded (15 RPM cap) - marked as pending",
            duration_seconds=round(time.monotonic() - start_time, 3),
        )

    sanitized = _sanitize_context(business_name, evidence_dict, opportunities)

    prompt = (
        "You are an expert B2B sales automation consultant for NexAgentLab.\n"
        "Analyze the following verified factual website data for a prospective client.\n"
        "Do NOT invent false claims or assume features missing from the homepage don't exist elsewhere.\n"
        "Respond ONLY with a valid JSON object matching this schema:\n"
        "{\n"
        '  "summary": "2-3 sentence overview of business and its current web capabilities",\n'
        '  "top_opportunities": ["list", "of", "top", "3", "categories"],\n'
        '  "outreach_angle": "one concise personalized value proposition hook"\n'
        "}\n\n"
        f"Data:\n{json.dumps(sanitized, indent=2)}"
    )

    try:
        from google import genai

        api_key = get_api_key()
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=status.model or DEFAULT_MODEL,
            contents=prompt,
        )

        content_text = response.text if hasattr(response, "text") else str(response)
        duration = round(time.monotonic() - start_time, 3)
        structured = validate_ai_output(content_text)

        tokens_used = None
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            tokens_used = getattr(response.usage_metadata, "total_token_count", None)

        if structured is None:
            return AIAnalysisResult(
                success=False,
                provider="gemini",
                model=status.model,
                content=content_text,
                error="Failed to validate structured JSON output from model",
                tokens_used=tokens_used,
                duration_seconds=duration,
            )

        return AIAnalysisResult(
            success=True,
            provider="gemini",
            model=status.model,
            content=content_text,
            structured=structured,
            tokens_used=tokens_used,
            duration_seconds=duration,
        )
    except Exception as exc:
        logger.warning("Gemini AI analysis failed for lead %d: %s", lead_id, exc)
        return AIAnalysisResult(
            success=False,
            provider="gemini",
            model=status.model,
            error=f"API error: {exc}",
            duration_seconds=round(time.monotonic() - start_time, 3),
        )
