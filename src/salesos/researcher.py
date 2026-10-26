"""Website research engine for automated lead qualification.

Fetches lead websites, extracts structured evidence from HTML, identifies
sales opportunities using rule-based heuristics, and stores all findings
in the database.  Designed to be safe (SSRF protection, size limits,
untrusted-HTML handling) and idempotent (re-runs skip already-completed
research unless ``force=True``).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import sqlite3
import time
from datetime import datetime, timezone
from typing import Literal, Optional
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from .db import audit_log, transaction

logger = logging.getLogger(__name__)

AGENT_TYPE = "website_researcher"

USER_AGENT = "NexAgentLab-SalesOS-Research/0.1 (business-analysis)"

MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB

CONNECT_TIMEOUT = 15  # seconds
READ_TIMEOUT = 30  # seconds


# ── Pydantic schemas ────────────────────────────────────────────────


class WebsiteEvidence(BaseModel):
    """Structured evidence extracted from a website's HTML."""

    title: str | None = None
    meta_description: str | None = None
    headings: list[str] = Field(default_factory=list)
    links_found: int = 0
    contact_info: dict = Field(default_factory=dict)
    has_booking: bool = False
    has_whatsapp: bool = False
    has_chat_widget: bool = False
    has_lead_form: bool = False
    calls_to_action: list[str] = Field(default_factory=list)
    technologies_detected: list[str] = Field(default_factory=list)
    pages_analyzed: int = 1
    raw_observations: list[str] = Field(default_factory=list)


_OPPORTUNITY_CATEGORIES = {
    "website_design",
    "technical_seo",
    "local_seo",
    "booking_system",
    "lead_capture",
    "chatbot",
    "ai_assistant",
    "automation",
}


class OpportunityFinding(BaseModel):
    """A single identified sales opportunity for a lead."""

    category: str = Field(..., pattern="^(" + "|".join(_OPPORTUNITY_CATEGORIES) + ")$")
    description: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    confidence_label: Literal["confirmed", "likely", "hypothesis"]
    evidence: list[str] = Field(default_factory=list)
    priority: int = Field(..., ge=1, le=5)


class ResearchResult(BaseModel):
    """Complete output of a research run for one lead."""

    lead_id: int
    business_name: str
    website: str | None = None
    evidence: WebsiteEvidence | None = None
    opportunities: list[OpportunityFinding] = Field(default_factory=list)
    status: Literal["completed", "failed", "partial", "no_website"] = "completed"
    error_message: str | None = None
    researched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    duration_seconds: float = 0.0


class ResearchRunSummary(BaseModel):
    """Aggregate statistics for a batch research run."""

    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    no_website: int = 0
    duration_seconds: float = 0.0


# ── SSRF protection ────────────────────────────────────────────────


# Private / reserved CIDR blocks that must never be contacted.
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_safe_url(url: str) -> tuple[bool, str]:
    """Validate that *url* is safe to fetch (no SSRF into internal networks).

    Returns ``(is_safe, reason)`` where *reason* describes the rejection
    when ``is_safe`` is False.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Malformed URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"Disallowed scheme: {parsed.scheme!r}"

    hostname = parsed.hostname
    if not hostname:
        return False, "No hostname in URL"

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False, f"DNS resolution failed for {hostname!r}"

    for family, _type, _proto, _canonname, sockaddr in addr_infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"Invalid resolved IP: {ip_str}"
        for network in _BLOCKED_NETWORKS:
            if ip in network:
                return False, f"Resolved to blocked address {ip_str}"

    return True, "OK"


# ── Website fetcher ─────────────────────────────────────────────────


def fetch_page(
    url: str,
) -> tuple[str | None, str | None, int | None, str | None]:
    """Fetch a page over HTTP with safety guards.

    Returns ``(html_content, final_url, status_code, error_message)``.
    All four values are ``None``-able; on success the first three are
    populated and ``error_message`` is ``None``.
    """
    # Validate the initial URL before connecting.
    safe, reason = _is_safe_url(url)
    if not safe:
        return None, None, None, f"URL blocked: {reason}"

    # Build a session that does NOT auto-follow redirects so we can
    # validate each hop.
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.max_redirects = 10

    current_url = url
    response: requests.Response | None = None

    try:
        for _hop in range(session.max_redirects + 1):
            resp = session.get(
                current_url,
                allow_redirects=False,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                stream=True,
            )

            if resp.is_redirect or resp.is_permanent_redirect:
                next_url = resp.headers.get("Location", "")
                if not next_url:
                    resp.close()
                    return None, current_url, resp.status_code, "Redirect with no Location header"

                # Make relative redirects absolute.
                if next_url.startswith("/"):
                    parsed = urlparse(current_url)
                    next_url = f"{parsed.scheme}://{parsed.netloc}{next_url}"

                safe, reason = _is_safe_url(next_url)
                if not safe:
                    resp.close()
                    return None, current_url, resp.status_code, f"Redirect blocked: {reason}"

                resp.close()
                current_url = next_url
                continue

            # Not a redirect — consume the body with a size cap.
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=8192):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    resp.close()
                    return None, current_url, resp.status_code, (
                        f"Response exceeded {MAX_RESPONSE_BYTES} byte limit"
                    )
                chunks.append(chunk)
            resp.close()

            resp.raise_for_status()

            body = b"".join(chunks)
            encoding = resp.encoding or "utf-8"
            try:
                html = body.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                html = body.decode("utf-8", errors="replace")

            return html, current_url, resp.status_code, None

        # If we exhaust all hops without a non-redirect response:
        return None, current_url, None, "Too many redirects"

    except requests.exceptions.SSLError as exc:
        return None, current_url, None, f"SSL error: {exc}"
    except requests.exceptions.ConnectionError as exc:
        return None, current_url, None, f"Connection error: {exc}"
    except requests.exceptions.Timeout as exc:
        return None, current_url, None, f"Timeout: {exc}"
    except requests.exceptions.TooManyRedirects as exc:
        return None, current_url, None, f"Too many redirects: {exc}"
    except requests.exceptions.HTTPError as exc:
        return None, current_url, getattr(exc.response, "status_code", None), f"HTTP error: {exc}"
    except requests.exceptions.RequestException as exc:
        return None, current_url, None, f"Request error: {exc}"


# ── Evidence extractor ──────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s\-.]?)?\(?\d{2,4}\)?[\s\-.]?\d{3,4}[\s\-.]?\d{3,4}"
)

_BOOKING_KEYWORDS = {
    "book", "schedule", "appointment", "reserve",
    "calendly", "acuity",
}

_CHAT_WIDGET_INDICATORS = {
    "tawk.to", "tawk", "tidio", "intercom", "drift",
    "livechat", "zendesk", "crisp", "hubspot",
}

_TECH_PATTERNS: dict[str, list[str]] = {
    "WordPress": ["wp-content", "wp-includes", "wordpress"],
    "Shopify": ["cdn.shopify", "shopify"],
    "Wix": ["wix.com", "parastorage.com"],
    "Squarespace": ["squarespace.com", "sqsp."],
    "React": ["react.production.min", "react-dom"],
    "Vue.js": ["vue.min.js", "vue.runtime", "vuejs"],
    "Angular": ["angular.min.js", "angular.io"],
    "jQuery": ["jquery"],
    "Bootstrap": ["bootstrap"],
    "Tailwind CSS": ["tailwind"],
    "Google Analytics": ["google-analytics.com", "googletagmanager.com/gtag", "ga.js", "analytics.js"],
    "Google Tag Manager": ["googletagmanager.com/gtm"],
    "Facebook Pixel": ["connect.facebook.net/en_US/fbevents", "facebook.net/signals"],
    "Hotjar": ["hotjar.com"],
}

_CTA_KEYWORDS = {
    "get started", "sign up", "start free", "try free", "free trial",
    "contact us", "get a quote", "request a demo", "book now",
    "schedule", "learn more", "subscribe", "download", "buy now",
    "shop now", "order now", "call now", "get in touch",
}


def extract_evidence(html: str, url: str) -> WebsiteEvidence:
    """Parse *html* and extract structured evidence about the website.

    All observations are factual — the function never fabricates claims.
    """
    soup = BeautifulSoup(html, "html.parser")

    observations: list[str] = []

    # ── Title ───────────────────────────────────────────────────────
    title_tag = soup.title
    title = title_tag.get_text(strip=True) if title_tag else None
    if title:
        observations.append(f"Page title: {title}")
    else:
        observations.append("No page title found")

    # ── Meta description ────────────────────────────────────────────
    meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta_description: str | None = None
    if meta_desc_tag and meta_desc_tag.get("content"):
        meta_description = str(meta_desc_tag["content"]).strip()
        observations.append(f"Meta description: {meta_description[:120]}")
    else:
        observations.append("No meta description found")

    # ── Headings ────────────────────────────────────────────────────
    headings: list[str] = []
    h1_count = 0
    for tag_name in ("h1", "h2", "h3"):
        for tag in soup.find_all(tag_name):
            text = tag.get_text(strip=True)
            if text:
                headings.append(f"{tag_name}: {text}")
                if tag_name == "h1":
                    h1_count += 1

    if h1_count == 0:
        observations.append("No H1 heading found")
    elif h1_count == 1:
        observations.append("Found 1 H1 heading")
    else:
        observations.append(f"Found {h1_count} H1 headings")

    if headings:
        observations.append(f"Found {len(headings)} headings (H1-H3)")

    # ── Links ───────────────────────────────────────────────────────
    all_links = soup.find_all("a", href=True)
    links_found = len(all_links)
    if links_found:
        observations.append(f"Found {links_found} links")

    # ── Contact info ────────────────────────────────────────────────
    contact_info: dict[str, list[str]] = {}

    # Emails from mailto: links
    emails_found: set[str] = set()
    for a in all_links:
        href = str(a.get("href", ""))
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            if email:
                emails_found.add(email.lower())

    # Emails from page text
    page_text = soup.get_text(separator=" ")
    for match in _EMAIL_RE.finditer(page_text):
        emails_found.add(match.group().lower())

    if emails_found:
        contact_info["emails"] = sorted(emails_found)
        observations.append(f"Found {len(emails_found)} email address(es)")

    # Phone numbers
    phones_found: set[str] = set()
    for a in all_links:
        href = str(a.get("href", ""))
        if href.lower().startswith("tel:"):
            phone = href[4:].strip()
            if phone:
                phones_found.add(phone)

    for match in _PHONE_RE.finditer(page_text):
        candidate = match.group().strip()
        # Basic sanity: must have at least 7 digit characters.
        if sum(c.isdigit() for c in candidate) >= 7:
            phones_found.add(candidate)

    if phones_found:
        contact_info["phones"] = sorted(phones_found)
        observations.append(f"Found {len(phones_found)} phone number(s)")

    # ── Booking indicators ──────────────────────────────────────────
    has_booking = False
    html_lower = html.lower()
    for a in all_links:
        href_lower = str(a.get("href", "")).lower()
        text_lower = a.get_text(strip=True).lower()
        combined = href_lower + " " + text_lower
        if any(kw in combined for kw in _BOOKING_KEYWORDS):
            has_booking = True
            break

    if not has_booking:
        for btn in soup.find_all("button"):
            text_lower = btn.get_text(strip=True).lower()
            if any(kw in text_lower for kw in _BOOKING_KEYWORDS):
                has_booking = True
                break

    if has_booking:
        observations.append("Booking/scheduling capability detected")

    # ── WhatsApp ────────────────────────────────────────────────────
    has_whatsapp = False
    if "wa.me" in html_lower or "whatsapp" in html_lower:
        has_whatsapp = True
        observations.append("WhatsApp integration detected")

    # ── Chat widgets ────────────────────────────────────────────────
    has_chat_widget = False
    for indicator in _CHAT_WIDGET_INDICATORS:
        if indicator in html_lower:
            has_chat_widget = True
            observations.append(f"Chat widget detected: {indicator}")
            break

    # ── Lead capture forms ──────────────────────────────────────────
    has_lead_form = False
    for form in soup.find_all("form"):
        form_html = str(form).lower()
        # Look for email inputs, newsletter signup, or contact form patterns.
        has_email_input = bool(
            form.find("input", attrs={"type": "email"})
            or form.find("input", attrs={"name": re.compile(r"email", re.I)})
        )
        has_contact_fields = any(
            kw in form_html
            for kw in ("newsletter", "subscribe", "contact", "signup", "sign-up", "message")
        )
        if has_email_input or has_contact_fields:
            has_lead_form = True
            break

    if has_lead_form:
        observations.append("Lead capture form detected")

    # ── Calls to action ─────────────────────────────────────────────
    calls_to_action: list[str] = []
    seen_ctas: set[str] = set()

    for btn in soup.find_all("button"):
        text = btn.get_text(strip=True)
        if text and len(text) < 80:
            text_lower = text.lower()
            if any(kw in text_lower for kw in _CTA_KEYWORDS):
                if text_lower not in seen_ctas:
                    seen_ctas.add(text_lower)
                    calls_to_action.append(text)

    for a in all_links:
        text = a.get_text(strip=True)
        if text and len(text) < 80:
            text_lower = text.lower()
            if any(kw in text_lower for kw in _CTA_KEYWORDS):
                if text_lower not in seen_ctas:
                    seen_ctas.add(text_lower)
                    calls_to_action.append(text)

    if calls_to_action:
        observations.append(f"Found {len(calls_to_action)} call(s) to action")

    # ── Technology detection ────────────────────────────────────────
    technologies_detected: list[str] = []
    scripts = soup.find_all("script", src=True)
    link_tags = soup.find_all("link", href=True)

    # Build a combined string of all script srcs and link hrefs for scanning.
    resource_urls = " ".join(
        str(tag.get("src", "")) for tag in scripts
    ) + " " + " ".join(
        str(tag.get("href", "")) for tag in link_tags
    )
    resource_urls_lower = resource_urls.lower()

    for tech_name, patterns in _TECH_PATTERNS.items():
        for pattern in patterns:
            if pattern in resource_urls_lower or pattern in html_lower:
                technologies_detected.append(tech_name)
                observations.append(f"Technology detected: {tech_name}")
                break

    # ── Responsive viewport ─────────────────────────────────────────
    viewport_tag = soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)})
    if viewport_tag:
        observations.append("Responsive viewport meta tag found")

    return WebsiteEvidence(
        title=title,
        meta_description=meta_description,
        headings=headings,
        links_found=links_found,
        contact_info=contact_info,
        has_booking=has_booking,
        has_whatsapp=has_whatsapp,
        has_chat_widget=has_chat_widget,
        has_lead_form=has_lead_form,
        calls_to_action=calls_to_action,
        technologies_detected=technologies_detected,
        pages_analyzed=1,
        raw_observations=observations,
    )


# ── Opportunity identifier ──────────────────────────────────────────


def identify_opportunities(evidence: WebsiteEvidence) -> list[OpportunityFinding]:
    """Apply rule-based heuristics to identify sales opportunities.

    Returns a list of :class:`OpportunityFinding` sorted by priority
    (1 = highest).  All rules are deterministic — no AI required.
    """
    opportunities: list[OpportunityFinding] = []

    # ── No meta description → technical_seo ─────────────────────────
    if not evidence.meta_description:
        opportunities.append(OpportunityFinding(
            category="technical_seo",
            description="Website is missing a meta description tag, which hurts search engine click-through rates.",
            confidence=0.9,
            confidence_label="confirmed",
            evidence=["No meta description tag found on the homepage."],
            priority=2,
        ))

    # ── H1 heading issues → technical_seo ───────────────────────────
    h1_headings = [h for h in evidence.headings if h.startswith("h1:")]
    if len(h1_headings) == 0:
        opportunities.append(OpportunityFinding(
            category="technical_seo",
            description="Website has no H1 heading. Search engines rely on H1 tags to understand page content.",
            confidence=0.8,
            confidence_label="confirmed",
            evidence=["No H1 heading tag found on the homepage."],
            priority=2,
        ))
    elif len(h1_headings) > 1:
        opportunities.append(OpportunityFinding(
            category="technical_seo",
            description=f"Website has {len(h1_headings)} H1 headings. Best practice is exactly one H1 per page.",
            confidence=0.8,
            confidence_label="confirmed",
            evidence=[f"Found {len(h1_headings)} H1 headings on the homepage."],
            priority=3,
        ))

    # ── No lead capture form → lead_capture ─────────────────────────
    if not evidence.has_lead_form:
        opportunities.append(OpportunityFinding(
            category="lead_capture",
            description="No lead capture form detected on the homepage. Adding one could convert more visitors into leads.",
            confidence=0.6,
            confidence_label="hypothesis",
            evidence=["No contact form, newsletter signup, or email capture form found on the analyzed page."],
            priority=2,
        ))

    # ── No booking system → booking_system ──────────────────────────
    if not evidence.has_booking:
        opportunities.append(OpportunityFinding(
            category="booking_system",
            description="No online booking or scheduling system detected. An integrated booking tool could streamline appointments.",
            confidence=0.5,
            confidence_label="hypothesis",
            evidence=["No booking/scheduling links or widgets found on the analyzed page."],
            priority=3,
        ))

    # ── No chat widget → chatbot ────────────────────────────────────
    if not evidence.has_chat_widget:
        opportunities.append(OpportunityFinding(
            category="chatbot",
            description="No live chat or chatbot widget detected. A chat widget can increase engagement and capture leads 24/7.",
            confidence=0.5,
            confidence_label="hypothesis",
            evidence=["No known chat widget scripts (Tawk.to, Tidio, Intercom, Drift, etc.) detected."],
            priority=3,
        ))

    # ── No WhatsApp → chatbot (lower confidence) ────────────────────
    if not evidence.has_whatsapp:
        opportunities.append(OpportunityFinding(
            category="chatbot",
            description="No WhatsApp integration detected. WhatsApp chat can improve accessibility for mobile-first customers.",
            confidence=0.3,
            confidence_label="hypothesis",
            evidence=["No wa.me links or WhatsApp references found."],
            priority=4,
        ))

    # ── Design indicators → website_design ──────────────────────────
    design_issues: list[str] = []
    has_responsive = any("viewport" in obs.lower() for obs in evidence.raw_observations)
    if not has_responsive:
        design_issues.append("No responsive viewport meta tag found — site may not be mobile-friendly.")

    # Check for old jQuery versions (very rough heuristic).
    for obs in evidence.raw_observations:
        if "jquery" in obs.lower():
            # jQuery itself isn't bad, but combined with no viewport it suggests age.
            if not has_responsive:
                design_issues.append("jQuery detected without responsive viewport — may indicate an older design.")
            break

    if design_issues:
        opportunities.append(OpportunityFinding(
            category="website_design",
            description="Website shows signs of an outdated design that could impact user experience and conversions.",
            confidence=0.6,
            confidence_label="likely",
            evidence=design_issues,
            priority=3,
        ))

    # ── No analytics → technical_seo ────────────────────────────────
    has_ga = "Google Analytics" in evidence.technologies_detected
    has_gtm = "Google Tag Manager" in evidence.technologies_detected
    if not has_ga and not has_gtm:
        opportunities.append(OpportunityFinding(
            category="technical_seo",
            description="No Google Analytics or Google Tag Manager detected. Without analytics, it's impossible to measure marketing ROI.",
            confidence=0.7,
            confidence_label="likely",
            evidence=["No Google Analytics or Google Tag Manager scripts found."],
            priority=2,
        ))

    # Sort by priority (ascending = most important first).
    opportunities.sort(key=lambda o: (o.priority, -o.confidence))
    return opportunities


# ── Database helpers ────────────────────────────────────────────────


def _store_research(
    conn: sqlite3.Connection,
    result: ResearchResult,
    *,
    force: bool = False,
) -> None:
    """Persist a :class:`ResearchResult` to the ``research`` and
    ``opportunities`` tables.

    Idempotent: skips if a completed record already exists for the same
    ``lead_id`` + ``agent_type``, unless *force* is True.
    """
    existing = conn.execute(
        "SELECT id FROM research WHERE lead_id = ? AND agent_type = ? AND status = 'completed'",
        (result.lead_id, AGENT_TYPE),
    ).fetchone()

    findings_json = result.evidence.model_dump_json() if result.evidence else None

    if existing and not force:
        return

    if existing and force:
        # Update the existing record.
        conn.execute(
            """UPDATE research SET
               findings = ?, confidence = ?, status = ?,
               updated_at = datetime('now')
               WHERE id = ?""",
            (
                findings_json,
                _overall_confidence(result),
                result.status,
                existing["id"],
            ),
        )
        # Remove old opportunities so we can re-insert fresh ones.
        conn.execute(
            "DELETE FROM opportunities WHERE lead_id = ? AND status = 'identified'",
            (result.lead_id,),
        )
    else:
        conn.execute(
            """INSERT INTO research (lead_id, agent_type, findings, confidence, status)
               VALUES (?, ?, ?, ?, ?)""",
            (
                result.lead_id,
                AGENT_TYPE,
                findings_json,
                _overall_confidence(result),
                result.status,
            ),
        )

    # Insert opportunities.
    for opp in result.opportunities:
        conn.execute(
            """INSERT INTO opportunities
               (lead_id, category, description, confidence, evidence, priority, status)
               VALUES (?, ?, ?, ?, ?, ?, 'identified')""",
            (
                result.lead_id,
                opp.category,
                opp.description,
                opp.confidence,
                json.dumps(opp.evidence),
                opp.priority,
            ),
        )

    audit_log(
        conn,
        "research",
        result.lead_id,
        "research_completed" if result.status == "completed" else f"research_{result.status}",
        json.dumps({
            "status": result.status,
            "opportunities": len(result.opportunities),
            "duration_seconds": result.duration_seconds,
            "error": result.error_message,
        }),
    )


def _overall_confidence(result: ResearchResult) -> float:
    """Compute a simple average confidence from opportunities, or 0.0."""
    if not result.opportunities:
        return 0.0
    return round(
        sum(o.confidence for o in result.opportunities) / len(result.opportunities),
        3,
    )


# ── Research orchestration ──────────────────────────────────────────


def research_lead(
    lead_row: sqlite3.Row,
    conn: sqlite3.Connection,
    force: bool = False,
) -> ResearchResult:
    """Run the full research pipeline for a single lead.

    1. Check if the lead has a website.
    2. Fetch the homepage.
    3. Extract evidence from the HTML.
    4. Identify opportunities.
    5. Store results in the database.

    Returns a :class:`ResearchResult` regardless of outcome.
    """
    start = time.monotonic()

    lead_id: int = lead_row["id"]
    business_name: str = lead_row["business_name"]
    website: str | None = lead_row["website"]

    # ── No website ──────────────────────────────────────────────────
    if not website or not website.strip():
        result = ResearchResult(
            lead_id=lead_id,
            business_name=business_name,
            website=None,
            status="no_website",
            duration_seconds=round(time.monotonic() - start, 3),
        )
        with transaction(conn):
            _store_research(conn, result, force=force)
        return result

    website = website.strip()

    # Ensure scheme.
    if not website.startswith(("http://", "https://")):
        website = "https://" + website

    # ── Fetch ───────────────────────────────────────────────────────
    html, final_url, status_code, fetch_error = fetch_page(website)
    if html is None:
        result = ResearchResult(
            lead_id=lead_id,
            business_name=business_name,
            website=website,
            status="failed",
            error_message=fetch_error,
            duration_seconds=round(time.monotonic() - start, 3),
        )
        with transaction(conn):
            _store_research(conn, result, force=force)
        return result

    # ── Extract evidence ────────────────────────────────────────────
    try:
        evidence = extract_evidence(html, final_url or website)
    except Exception as exc:
        logger.exception("Evidence extraction failed for lead %d", lead_id)
        result = ResearchResult(
            lead_id=lead_id,
            business_name=business_name,
            website=website,
            status="partial",
            error_message=f"Evidence extraction error: {exc}",
            duration_seconds=round(time.monotonic() - start, 3),
        )
        with transaction(conn):
            _store_research(conn, result, force=force)
        return result

    # ── Identify opportunities ──────────────────────────────────────
    opportunities = identify_opportunities(evidence)

    result = ResearchResult(
        lead_id=lead_id,
        business_name=business_name,
        website=final_url or website,
        evidence=evidence,
        opportunities=opportunities,
        status="completed",
        duration_seconds=round(time.monotonic() - start, 3),
    )

    with transaction(conn):
        _store_research(conn, result, force=force)

    return result


def run_research_batch(
    conn: sqlite3.Connection,
    limit: int = 50,
    lead_ids: list[int] | None = None,
    force: bool = False,
) -> ResearchRunSummary:
    """Research a batch of leads sequentially.

    Parameters
    ----------
    conn:
        Open database connection.
    limit:
        Maximum number of leads to process in this batch.
    lead_ids:
        If provided, only research these specific leads.  Otherwise,
        picks leads with ``status='imported'``.
    force:
        Re-run research even if a completed record already exists.
    """
    batch_start = time.monotonic()
    summary = ResearchRunSummary()

    # ── Select eligible leads ───────────────────────────────────────
    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        rows = conn.execute(
            f"SELECT * FROM leads WHERE id IN ({placeholders})",
            lead_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status = 'imported' LIMIT ?",
            (limit,),
        ).fetchall()

    summary.total = len(rows)

    for row in rows:
        lead_id = row["id"]

        # Skip already-researched leads unless force is set.
        if not force:
            existing = conn.execute(
                "SELECT id FROM research WHERE lead_id = ? AND agent_type = ? AND status = 'completed'",
                (lead_id, AGENT_TYPE),
            ).fetchone()
            if existing:
                summary.skipped += 1
                continue

        # Mark as researching.
        with transaction(conn):
            conn.execute(
                "UPDATE leads SET status = 'researching', updated_at = datetime('now') WHERE id = ?",
                (lead_id,),
            )

        try:
            result = research_lead(row, conn, force=force)
        except Exception:
            logger.exception("Unhandled error researching lead %d", lead_id)
            # Roll lead status back.
            with transaction(conn):
                conn.execute(
                    "UPDATE leads SET status = 'imported', updated_at = datetime('now') WHERE id = ?",
                    (lead_id,),
                )
            summary.failed += 1
            continue

        # Update lead status based on result.
        if result.status == "completed":
            new_status = "researched"
            summary.completed += 1
        elif result.status == "no_website":
            new_status = "researched"
            summary.no_website += 1
        else:
            # failed or partial — roll back so it can be retried.
            new_status = "imported"
            summary.failed += 1

        with transaction(conn):
            conn.execute(
                "UPDATE leads SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (new_status, lead_id),
            )

    summary.duration_seconds = round(time.monotonic() - batch_start, 3)
    return summary


# ── Query helpers ───────────────────────────────────────────────────


def get_research_status(conn: sqlite3.Connection) -> dict:
    """Return counts of research records grouped by status.

    Example return::

        {"completed": 42, "failed": 3, "no_website": 5, "pending": 0}
    """
    rows = conn.execute(
        "SELECT status, COUNT(*) AS cnt FROM research WHERE agent_type = ? GROUP BY status",
        (AGENT_TYPE,),
    ).fetchall()
    return {row["status"]: row["cnt"] for row in rows}


def get_lead_research(conn: sqlite3.Connection, lead_id: int) -> dict | None:
    """Return research findings and opportunities for a single lead.

    Returns ``None`` if no research record exists.
    """
    research_row = conn.execute(
        "SELECT * FROM research WHERE lead_id = ? AND agent_type = ? ORDER BY created_at DESC LIMIT 1",
        (lead_id, AGENT_TYPE),
    ).fetchone()

    if not research_row:
        return None

    opportunity_rows = conn.execute(
        "SELECT * FROM opportunities WHERE lead_id = ? ORDER BY priority ASC, confidence DESC",
        (lead_id,),
    ).fetchall()

    findings = None
    if research_row["findings"]:
        try:
            findings = json.loads(research_row["findings"])
        except json.JSONDecodeError:
            findings = research_row["findings"]

    return {
        "lead_id": lead_id,
        "agent_type": research_row["agent_type"],
        "status": research_row["status"],
        "confidence": research_row["confidence"],
        "findings": findings,
        "created_at": research_row["created_at"],
        "updated_at": research_row["updated_at"],
        "opportunities": [
            {
                "id": opp["id"],
                "category": opp["category"],
                "description": opp["description"],
                "confidence": opp["confidence"],
                "evidence": json.loads(opp["evidence"]) if opp["evidence"] else [],
                "priority": opp["priority"],
                "status": opp["status"],
            }
            for opp in opportunity_rows
        ],
    }


def retry_failed_research(
    conn: sqlite3.Connection,
    limit: int = 20,
) -> ResearchRunSummary:
    """Re-run research on leads whose previous attempt failed.

    Selects leads that have a ``status='failed'`` record in the research
    table and re-processes them (with ``force=True``).
    """
    rows = conn.execute(
        """SELECT DISTINCT l.* FROM leads l
           JOIN research r ON r.lead_id = l.id
           WHERE r.agent_type = ? AND r.status = 'failed'
           LIMIT ?""",
        (AGENT_TYPE, limit),
    ).fetchall()

    lead_ids = [row["id"] for row in rows]
    if not lead_ids:
        return ResearchRunSummary()

    return run_research_batch(conn, limit=limit, lead_ids=lead_ids, force=True)
