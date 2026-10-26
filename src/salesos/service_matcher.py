"""Service Matching Agent for NexAgentLab SalesOS.

Evaluates research evidence and identified opportunity records to select
the most relevant NexAgentLab service package for a qualified lead.
Provides a clear rationale, verified evidence references, and realistic
value propositions without fabricating case studies or pricing guarantees.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, Field


# ── Agency Service Definitions ───────────────────────────────────────

SERVICES_CATALOG: dict[str, dict] = {
    "booking_system": {
        "title": "Online Booking & Automated Scheduling",
        "description": "Integration of self-serve appointment scheduling to eliminate manual scheduling friction.",
        "value_proposition": "Enable customers to book appointments directly from your website 24/7 without manual back-and-forth emails.",
        "triggers": ["booking_system"],
    },
    "chatbot": {
        "title": "AI Chat Assistant for 24/7 Visitor Engagement",
        "description": "Smart website chat agent that answers common customer questions and captures qualified leads.",
        "value_proposition": "Engage visitors immediately, answer questions about your services, and capture inquiries even when your office is closed.",
        "triggers": ["chatbot"],
    },
    "whatsapp_integration": {
        "title": "WhatsApp Business Chat Integration",
        "description": "Direct WhatsApp click-to-chat integration for instant mobile customer communication.",
        "value_proposition": "Give mobile visitors an instant, friction-free way to reach your team directly on WhatsApp.",
        "triggers": ["whatsapp"],
    },
    "lead_capture": {
        "title": "Lead Capture & CRM Automation",
        "description": "High-converting inquiry forms and automated routing into your customer management pipeline.",
        "value_proposition": "Capture more inbound inquiries with optimized forms and automated notifications so no lead slips through.",
        "triggers": ["lead_capture"],
    },
    "technical_seo": {
        "title": "Technical SEO & Search Performance Optimization",
        "description": "Resolution of structural SEO issues including meta descriptions, headings, and discoverability.",
        "value_proposition": "Improve your organic visibility on Google by fixing foundational meta tag and structural technical issues.",
        "triggers": ["technical_seo"],
    },
    "website_design": {
        "title": "Modern Website Redesign & Mobile Optimization",
        "description": "Clean, mobile-first website redesign focused on conversion and modern user experience.",
        "value_proposition": "Transform your website into a fast, mobile-friendly digital storefront that turns more visitors into clients.",
        "triggers": ["website_design"],
    },
    "local_seo": {
        "title": "Local SEO & Google Business Profile Optimization",
        "description": "Local search optimization to help area customers find your physical business easily.",
        "value_proposition": "Rank higher in local map searches so nearby customers find you before your local competitors.",
        "triggers": ["local_seo"],
    },
    "automation": {
        "title": "Inbound Sales Follow-Up Automation",
        "description": "Automated email/SMS follow-up sequences for new inquiries.",
        "value_proposition": "Respond to new customer inquiries within seconds and automatically nurture interested leads.",
        "triggers": ["automation"],
    },
    "paid_ads": {
        "title": "Targeted Google & Meta Advertising",
        "description": "High-intent paid ad campaigns driving ready-to-buy local traffic.",
        "value_proposition": "Drive immediate, qualified search traffic from prospective buyers actively looking for your services.",
        "triggers": ["paid_ads"],
    },
    "maintenance": {
        "title": "Website Speed, Security & Ongoing Care",
        "description": "Regular maintenance, security monitoring, and performance updates.",
        "value_proposition": "Keep your website secure, fast, and constantly running smoothly with dedicated ongoing care.",
        "triggers": ["maintenance"],
    },
}


# ── Pydantic models ──────────────────────────────────────────────────


class ServiceMatchResult(BaseModel):
    """The result of matching a lead to NexAgentLab service packages."""

    lead_id: int
    business_name: str
    primary_service: str
    service_title: str
    secondary_services: list[str] = Field(default_factory=list)
    rationale: str
    evidence_references: list[str] = Field(default_factory=list)
    value_proposition: str


# ── Matching logic ───────────────────────────────────────────────────


def match_services_for_lead(
    lead_id: int,
    business_name: str,
    opportunity_rows: list[dict],
    evidence_dict: dict | None = None,
) -> ServiceMatchResult:
    """Analyze opportunities and evidence to select the top NexAgentLab services."""
    evidence_dict = evidence_dict or {}

    # Gather category scores based on opportunity confidence and priority
    category_scores: dict[str, float] = {}
    category_evidence: dict[str, list[str]] = {}

    for opp in opportunity_rows:
        cat = opp.get("category", "")
        conf = float(opp.get("confidence", 0.5))
        prio = int(opp.get("priority", 3))
        ev_list = opp.get("evidence", [])
        if isinstance(ev_list, str):
            try:
                ev_list = json.loads(ev_list)
            except Exception:
                ev_list = [ev_list]

        # Weight higher priority (lower number) and higher confidence
        weight = conf * (6 - prio)
        category_scores[cat] = category_scores.get(cat, 0.0) + weight

        if cat not in category_evidence:
            category_evidence[cat] = []
        category_evidence[cat].extend(ev_list)

    # Check direct evidence signals
    if evidence_dict.get("has_booking") is False:
        category_scores["booking_system"] = category_scores.get("booking_system", 0.0) + 2.0
    if evidence_dict.get("has_chat_widget") is False:
        category_scores["chatbot"] = category_scores.get("chatbot", 0.0) + 1.5
    if evidence_dict.get("has_whatsapp") is False:
        category_scores["whatsapp_integration"] = category_scores.get("whatsapp_integration", 0.0) + 1.0
    if evidence_dict.get("has_lead_form") is False:
        category_scores["lead_capture"] = category_scores.get("lead_capture", 0.0) + 2.0
    if "meta_description" in evidence_dict and not evidence_dict.get("meta_description"):
        category_scores["technical_seo"] = category_scores.get("technical_seo", 0.0) + 2.5

    # Prioritize booking and lead capture and chatbots for local service businesses
    if not category_scores:
        # Default fallback service for businesses needing digital modernization
        primary_key = "website_design"
    else:
        # Pick highest scoring category that exists in our catalog
        sorted_cats = sorted(
            category_scores.keys(),
            key=lambda k: category_scores[k],
            reverse=True,
        )
        primary_key = sorted_cats[0]
        if primary_key not in SERVICES_CATALOG:
            # Map or fallback
            primary_key = "website_design"

    service_def = SERVICES_CATALOG.get(primary_key, SERVICES_CATALOG["website_design"])

    # Secondary services (next 2)
    secondary: list[str] = []
    if category_scores:
        for cat in sorted(category_scores.keys(), key=lambda k: category_scores[k], reverse=True)[1:]:
            if cat in SERVICES_CATALOG and cat != primary_key and len(secondary) < 2:
                secondary.append(SERVICES_CATALOG[cat]["title"])

    # Collect unique evidence references
    evidence_refs = list(dict.fromkeys(category_evidence.get(primary_key, [])))
    if not evidence_refs and evidence_dict.get("title"):
        evidence_refs.append(f"Analyzed homepage title: '{evidence_dict['title']}'")

    rationale = (
        f"Selected {service_def['title']} because research identified {primary_key.replace('_', ' ')} "
        f"as the highest-impact digital enhancement for {business_name}."
    )

    return ServiceMatchResult(
        lead_id=lead_id,
        business_name=business_name,
        primary_service=primary_key,
        service_title=service_def["title"],
        secondary_services=secondary,
        rationale=rationale,
        evidence_references=evidence_refs[:4],
        value_proposition=service_def["value_proposition"],
    )
