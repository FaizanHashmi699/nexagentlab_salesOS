"""Pydantic models for lead data validation and column mapping.

Provides strict validation for lead records, auto-detection of column
names from common aliases, and mapping utilities for heterogeneous
input files.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel, field_validator


# ── Lead pipeline statuses ──────────────────────────────────────────

class LeadStatus(str, Enum):
    """All valid lead pipeline statuses."""
    IMPORTED = "imported"
    RESEARCHING = "researching"
    QUALIFIED = "qualified"
    READY_FOR_OUTREACH = "ready_for_outreach"
    SENT = "sent"
    REPLIED = "replied"
    INTERESTED = "interested"
    MEETING_PROPOSED = "meeting_proposed"
    MEETING_BOOKED = "meeting_booked"
    PROPOSAL = "proposal"
    WON = "won"
    LOST = "lost"
    SUPPRESSED = "suppressed"
    INVALID = "invalid"
    UNQUALIFIED = "unqualified"
    BOUNCED = "bounced"
    PAUSED = "paused"


class ImportStatus(str, Enum):
    """Import job statuses."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"


# ── Column alias map ────────────────────────────────────────────────

COLUMN_ALIASES: dict[str, list[str]] = {
    "business_name": [
        "business_name", "business", "company", "company_name",
        "name", "org", "organization",
    ],
    "contact_name": [
        "contact_name", "contact", "full_name", "person",
        "representative", "contact_person",
    ],
    "email": [
        "email", "email_address", "e-mail", "e_mail", "mail",
    ],
    "website": [
        "website", "url", "web", "site", "domain", "website_url",
    ],
    "phone": [
        "phone", "telephone", "tel", "phone_number", "mobile",
    ],
    "location": [
        "location", "city", "address", "region", "area", "city_state",
    ],
    "source": [
        "source", "lead_source", "origin", "channel",
    ],
    "external_id": [
        "external_id", "crm_id", "lead_id", "ref",
    ],
    "tags": [
        "tags", "labels", "categories",
    ],
    "notes": [
        "notes", "comments", "remarks", "description",
    ],
}

REQUIRED_FIELDS = {"business_name", "email"}

_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
)


# ── Lead record model ───────────────────────────────────────────────

class LeadRecord(BaseModel):
    """A validated lead record ready for database insertion."""

    business_name: str
    email: str
    website: Optional[str] = None
    contact_name: Optional[str] = None
    location: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None
    external_id: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Email is required")
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError(f"Invalid email format: {v}")
        return v

    @field_validator("business_name")
    @classmethod
    def validate_business_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Business name is required")
        return v.strip()

    @field_validator("website")
    @classmethod
    def normalize_website(cls, v: Optional[str]) -> Optional[str]:
        if v and v.strip():
            v = v.strip().lower()
            if not v.startswith(("http://", "https://")):
                v = "https://" + v
            return v
        return None


# ── Column mapping utilities ────────────────────────────────────────

def auto_map_columns(headers: list[str]) -> dict[str, str]:
    """Auto-detect column mapping from file headers to standard fields.

    Returns ``{standard_field: original_header}`` for each detected match.
    """
    mapping: dict[str, str] = {}
    normalized: dict[str, str] = {
        h.strip().lower().replace(" ", "_").replace("-", "_"): h
        for h in headers
    }

    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break

    return mapping


def apply_column_mapping(row: dict, mapping: dict[str, str]) -> dict:
    """Transform a raw row dict to use standard field names via *mapping*.

    Keys in *row* that don't appear in *mapping* values are dropped.
    """
    reverse = {v: k for k, v in mapping.items()}
    return {
        reverse[original_key]: value
        for original_key, value in row.items()
        if original_key in reverse
    }
