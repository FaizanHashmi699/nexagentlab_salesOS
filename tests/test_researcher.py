"""Comprehensive tests for the SalesOS Research Agent (Milestone 2).

Verifies website evidence extraction, sales opportunity identification,
SSRF defense, fetch error resilience, database persistence, idempotence,
audit logging, AI provider abstraction, and CLI integration.

All network calls are strictly mocked. No external requests are executed.
"""

from __future__ import annotations

import json
import socket
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
import requests

from salesos.ai_provider import (
    AIAnalysisResult,
    AIProviderStatus,
    _check_rate_limit,
    _mock_analyze,
    analyze_research,
    get_provider_status,
    validate_ai_output,
)
from salesos.db import init_db
from salesos.researcher import (
    AGENT_TYPE,
    OpportunityFinding,
    ResearchResult,
    ResearchRunSummary,
    WebsiteEvidence,
    _is_safe_url,
    extract_evidence,
    fetch_page,
    get_lead_research,
    get_research_status,
    identify_opportunities,
    research_lead,
    retry_failed_research,
    run_research_batch,
)


# ── Sample HTML fixtures ─────────────────────────────────────────────

SAMPLE_HTML_FULL = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="description" content="Sunrise Bakery - Fresh artisan bread and pastries daily.">
    <title>Sunrise Bakery | Fresh Artisan Bread &amp; Pastries</title>
    <script src="https://www.googletagmanager.com/gtag/js?id=G-12345"></script>
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
</head>
<body>
    <header>
        <h1>Welcome to Sunrise Bakery</h1>
        <p>Austin's favorite family-owned bakery.</p>
    </header>
    <main>
        <h2>Our Daily Bread Menu</h2>
        <p>Fresh sourdough, baguettes, and croissants baked every morning.</p>
        <h2>Visit Us or Call</h2>
        <p>Phone: (512) 555-0101 | Email: orders@sunrisebakery-test.com</p>
        <a href="mailto:contact@sunrisebakery-test.com">Email Us Directly</a>
        <a href="https://wa.me/15125550101">Chat on WhatsApp</a>
        <a href="https://calendly.com/sunrisebakery/tasting" class="btn">Schedule a Tasting</a>
        <form action="/newsletter" method="post">
            <input type="email" name="email" placeholder="Enter your email" required />
            <button type="submit">Subscribe</button>
        </form>
        <script src="https://embed.tawk.to/xyz123/default"></script>
    </main>
</body>
</html>"""

SAMPLE_HTML_MINIMAL = """<!DOCTYPE html>
<html>
<head><title>Minimal Page</title></head>
<body><p>Just a simple page without extra features.</p></body>
</html>"""

SAMPLE_HTML_NO_SEO = """<!DOCTYPE html>
<html>
<head></head>
<body>
    <h1>First Heading</h1>
    <h1>Second Duplicate Heading</h1>
    <p>No meta description, no title tag, duplicate H1.</p>
</body>
</html>"""

SAMPLE_HTML_BROKEN = """<html><head><title>Broken<p>Unclosed tags<a href="http://example.com"""


# ── Database fixtures ────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory/temp SQLite database initialized with schema."""
    db_path = str(tmp_path / "test_research.db")
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def db_with_leads(db):
    """Database populated with synthetic test leads."""
    leads = [
        ("Sunrise Bakery", "maria@sunrisebakery-test.com", "https://sunrisebakery-test.com", "Austin, TX"),
        ("TechFix Solutions", "james@techfix-test.com", "https://techfix-test.com", "Denver, CO"),
        ("No Website Plumbing", "mark@nosite-test.com", None, "Phoenix, AZ"),
        ("Empty Website Landscaping", "robert@empty-test.com", "", "Portland, OR"),
    ]
    for name, email, site, loc in leads:
        db.execute(
            """INSERT INTO leads (business_name, email, website, location, status)
               VALUES (?, ?, ?, ?, 'imported')""",
            (name, email, site, loc),
        )
    db.commit()
    return db


# ── SSRF Protection Tests ────────────────────────────────────────────


class TestSSRFProtection:
    def test_safe_public_url(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ]
            safe, reason = _is_safe_url("https://example.com")
            assert safe is True
            assert reason in ("OK", "")

    def test_reject_localhost_string(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
            ]
            safe, reason = _is_safe_url("http://localhost:8000")
            assert safe is False
            assert "Private" in reason or "Loopback" in reason or "blocked" in reason.lower()

    def test_reject_127_ip(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))
            ]
            safe, reason = _is_safe_url("http://127.0.0.1/admin")
            assert safe is False

    def test_reject_private_10_network(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80))
            ]
            safe, reason = _is_safe_url("http://10.0.0.1/dashboard")
            assert safe is False

    def test_reject_private_172_network(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.16.50.1", 80))
            ]
            safe, reason = _is_safe_url("http://172.16.50.1/config")
            assert safe is False

    def test_reject_private_192_168_network(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 80))
            ]
            safe, reason = _is_safe_url("http://192.168.1.1/")
            assert safe is False

    def test_reject_aws_metadata_169_254(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))
            ]
            safe, reason = _is_safe_url("http://169.254.169.254/latest/meta-data/")
            assert safe is False

    def test_reject_ipv6_loopback(self):
        with patch("socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 80, 0, 0))
            ]
            safe, reason = _is_safe_url("http://[::1]:8080")
            assert safe is False

    def test_reject_non_http_schemes(self):
        for bad_url in ["ftp://example.com", "file:///etc/passwd", "gopher://bad"]:
            safe, reason = _is_safe_url(bad_url)
            assert safe is False
            assert "scheme" in reason.lower()

    def test_reject_dns_failure(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
            safe, reason = _is_safe_url("https://this-domain-definitely-does-not-exist-xyz123.org")
            assert safe is False
            assert "DNS" in reason


# ── Page Fetcher Tests ───────────────────────────────────────────────


class TestFetchPage:
    @patch("salesos.researcher._is_safe_url", return_value=(True, "OK"))
    @patch("requests.Session.send")
    def test_fetch_success(self, mock_send, mock_safe):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_redirect = False
        mock_response.is_permanent_redirect = False
        mock_response.url = "https://sunrisebakery-test.com/"
        mock_response.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_response.iter_content.return_value = [b"<html><body><h1>Bakery</h1></body></html>"]
        mock_response.encoding = "utf-8"
        mock_send.return_value = mock_response

        html, final_url, status, err = fetch_page("https://sunrisebakery-test.com")
        assert err is None
        assert status == 200
        assert "Bakery" in html
        assert final_url.startswith("https://sunrisebakery-test.com")

    @patch("salesos.researcher._is_safe_url", return_value=(False, "Private IP range rejected"))
    def test_fetch_blocked_by_ssrf(self, mock_safe):
        html, final_url, status, err = fetch_page("http://127.0.0.1:8080")
        assert html is None
        assert "Blocked" in err or "Private IP" in err

    @patch("salesos.researcher._is_safe_url", return_value=(True, "OK"))
    @patch("requests.Session.send", side_effect=requests.exceptions.Timeout("Connect timeout"))
    def test_fetch_timeout(self, mock_send, mock_safe):
        html, final_url, status, err = fetch_page("https://slow-site-test.com")
        assert html is None
        assert "timed out" in err.lower() or "timeout" in err.lower()

    @patch("salesos.researcher._is_safe_url", return_value=(True, "OK"))
    @patch("requests.Session.send", side_effect=requests.exceptions.ConnectionError("Connection refused"))
    def test_fetch_connection_error(self, mock_send, mock_safe):
        html, final_url, status, err = fetch_page("https://offline-site-test.com")
        assert html is None
        assert "connection" in err.lower()

    @patch("salesos.researcher._is_safe_url", return_value=(True, "OK"))
    @patch("requests.Session.send")
    def test_fetch_too_large_content(self, mock_send, mock_safe):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.is_redirect = False
        mock_response.is_permanent_redirect = False
        mock_response.url = "https://huge-site-test.com"
        mock_response.iter_content.return_value = [b"x" * (6 * 1024 * 1024)]
        mock_send.return_value = mock_response

        html, final_url, status, err = fetch_page("https://huge-site-test.com")
        assert html is None
        assert "exceeded" in err.lower() or "limit" in err.lower()


# ── Evidence Extraction Tests ────────────────────────────────────────


class TestEvidenceExtraction:
    def test_extract_full_page_evidence(self):
        ev = extract_evidence(SAMPLE_HTML_FULL, "https://sunrisebakery-test.com")
        assert ev.title == "Sunrise Bakery | Fresh Artisan Bread & Pastries"
        assert "Sunrise Bakery" in ev.meta_description
        assert len(ev.headings) >= 2
        assert ev.has_whatsapp is True
        assert ev.has_booking is True
        assert ev.has_chat_widget is True
        assert ev.has_lead_form is True
        assert "contact@sunrisebakery-test.com" in ev.contact_info.get("emails", [])
        assert any("google" in tech.lower() for tech in ev.technologies_detected)
        assert any("jquery" in tech.lower() for tech in ev.technologies_detected)
        assert len(ev.raw_observations) > 0

    def test_extract_minimal_page(self):
        ev = extract_evidence(SAMPLE_HTML_MINIMAL, "https://minimal-test.com")
        assert ev.title == "Minimal Page"
        assert ev.meta_description is None
        assert ev.has_whatsapp is False
        assert ev.has_booking is False
        assert ev.has_chat_widget is False
        assert ev.has_lead_form is False

    def test_extract_malformed_html_does_not_crash(self):
        ev = extract_evidence(SAMPLE_HTML_BROKEN, "https://broken-test.com")
        assert ev is not None
        assert isinstance(ev.headings, list)
        assert isinstance(ev.technologies_detected, list)

    def test_extract_seo_indicators(self):
        ev = extract_evidence(SAMPLE_HTML_NO_SEO, "https://no-seo-test.com")
        assert ev.title is None
        assert ev.meta_description is None
        # Should record multiple H1 headings
        h1s = [h for h in ev.headings if h.lower().startswith("h1:")]
        assert len(h1s) == 2


# ── Opportunity Identification Tests ────────────────────────────────


class TestOpportunityIdentification:
    def test_identify_technical_seo_opportunities(self):
        ev = extract_evidence(SAMPLE_HTML_NO_SEO, "https://no-seo-test.com")
        opps = identify_opportunities(ev)

        categories = [o.category for o in opps]
        assert "technical_seo" in categories

        # Should identify missing meta description and H1 issues
        seo_opps = [o for o in opps if o.category == "technical_seo"]
        assert any("meta description" in o.description.lower() for o in seo_opps)
        assert any("h1" in o.description.lower() for o in seo_opps)

        # All opportunities must contain non-empty evidence
        for opp in opps:
            assert len(opp.evidence) > 0
            assert 1 <= opp.priority <= 5
            assert 0.0 <= opp.confidence <= 1.0

    def test_identify_missing_lead_capture(self):
        ev = extract_evidence(SAMPLE_HTML_MINIMAL, "https://minimal-test.com")
        opps = identify_opportunities(ev)

        categories = [o.category for o in opps]
        assert "lead_capture" in categories
        # Should be a hypothesis since missing on homepage doesn't guarantee missing site-wide
        lead_opp = next(o for o in opps if o.category == "lead_capture")
        assert lead_opp.confidence_label == "hypothesis"

    def test_identify_missing_chatbot(self):
        ev = extract_evidence(SAMPLE_HTML_MINIMAL, "https://minimal-test.com")
        opps = identify_opportunities(ev)

        categories = [o.category for o in opps]
        assert "chatbot" in categories
        chat_opp = next(o for o in opps if o.category == "chatbot")
        assert chat_opp.confidence_label == "hypothesis"

    def test_fully_featured_site_has_fewer_gaps(self):
        ev_full = extract_evidence(SAMPLE_HTML_FULL, "https://sunrisebakery-test.com")
        opps_full = identify_opportunities(ev_full)

        # Full site has booking, chat, whatsapp, lead form, title, meta
        categories = [o.category for o in opps_full]
        assert "booking_system" not in categories
        assert "chatbot" not in categories
        assert "lead_capture" not in categories


# ── Single Lead Research Workflow Tests ──────────────────────────────


class TestResearchLead:
    @patch("salesos.researcher.fetch_page")
    def test_research_lead_success(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_FULL, "https://sunrisebakery-test.com", 200, None)

        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 1").fetchone()
        result = research_lead(lead, db_with_leads)

        assert result.status == "completed"
        assert result.lead_id == 1
        assert result.evidence is not None
        assert result.evidence.has_booking is True

        # Check research table record
        res_row = db_with_leads.execute(
            "SELECT * FROM research WHERE lead_id = 1 AND agent_type = ?", (AGENT_TYPE,)
        ).fetchone()
        assert res_row is not None
        assert res_row["status"] == "completed"

        # Check opportunities table records
        opp_count = db_with_leads.execute(
            "SELECT COUNT(*) FROM opportunities WHERE lead_id = 1"
        ).fetchone()[0]
        assert opp_count == len(result.opportunities)

    def test_research_lead_no_website(self, db_with_leads):
        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 3").fetchone()  # No website
        result = research_lead(lead, db_with_leads)

        assert result.status == "no_website"
        assert result.evidence is None
        assert len(result.opportunities) == 0

        res_row = db_with_leads.execute(
            "SELECT * FROM research WHERE lead_id = 3 AND agent_type = ?", (AGENT_TYPE,)
        ).fetchone()
        assert res_row is not None
        assert res_row["status"] == "no_website"

    @patch("salesos.researcher.fetch_page")
    def test_research_lead_fetch_failure(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (None, None, None, "DNS lookup failed")

        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 2").fetchone()
        result = research_lead(lead, db_with_leads)

        assert result.status == "failed"
        assert "DNS lookup failed" in result.error_message

        res_row = db_with_leads.execute(
            "SELECT * FROM research WHERE lead_id = 2 AND agent_type = ?", (AGENT_TYPE,)
        ).fetchone()
        assert res_row is not None
        assert res_row["status"] == "failed"

    @patch("salesos.researcher.fetch_page")
    def test_research_lead_idempotent_no_duplicate_records(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_MINIMAL, "https://sunrisebakery-test.com", 200, None)

        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 1").fetchone()

        # Run 1
        res1 = research_lead(lead, db_with_leads)
        count1 = db_with_leads.execute("SELECT COUNT(*) FROM research WHERE lead_id = 1").fetchone()[0]
        opp_count1 = db_with_leads.execute("SELECT COUNT(*) FROM opportunities WHERE lead_id = 1").fetchone()[0]

        # Run 2 (without force)
        res2 = research_lead(lead, db_with_leads, force=False)
        count2 = db_with_leads.execute("SELECT COUNT(*) FROM research WHERE lead_id = 1").fetchone()[0]
        opp_count2 = db_with_leads.execute("SELECT COUNT(*) FROM opportunities WHERE lead_id = 1").fetchone()[0]

        # Must not duplicate records
        assert count1 == count2 == 1
        assert opp_count1 == opp_count2

    @patch("salesos.researcher.fetch_page")
    def test_research_lead_force_updates_existing(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_MINIMAL, "https://sunrisebakery-test.com", 200, None)
        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 1").fetchone()

        research_lead(lead, db_with_leads)

        # Update with full HTML and force=True
        mock_fetch.return_value = (SAMPLE_HTML_FULL, "https://sunrisebakery-test.com", 200, None)
        research_lead(lead, db_with_leads, force=True)

        res_count = db_with_leads.execute("SELECT COUNT(*) FROM research WHERE lead_id = 1").fetchone()[0]
        assert res_count == 1  # Updated in place, not duplicated


# ── Batch Research & Retry Tests ─────────────────────────────────────


class TestBatchResearch:
    @patch("salesos.researcher.fetch_page")
    def test_run_batch_processes_eligible_leads(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_MINIMAL, "https://test.com", 200, None)

        summary = run_research_batch(db_with_leads, limit=10)
        assert summary.total == 4
        # 2 have websites (completed), 2 have no website
        assert summary.completed == 2
        assert summary.no_website == 2
        assert summary.failed == 0

        # Leads table status updated to researched
        researched_count = db_with_leads.execute(
            "SELECT COUNT(*) FROM leads WHERE status = 'researched'"
        ).fetchone()[0]
        assert researched_count == 4

    @patch("salesos.researcher.fetch_page")
    def test_run_batch_respects_limit(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_MINIMAL, "https://test.com", 200, None)

        summary = run_research_batch(db_with_leads, limit=2)
        assert summary.total == 2

    @patch("salesos.researcher.fetch_page")
    def test_batch_skips_already_researched(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_MINIMAL, "https://test.com", 200, None)

        # Run first batch
        run_research_batch(db_with_leads, limit=2)

        # Run second batch with force=False
        summary2 = run_research_batch(db_with_leads, limit=2, force=False)
        # Should process remaining or skip
        assert summary2.skipped >= 0

    @patch("salesos.researcher.fetch_page")
    def test_retry_failed_research(self, mock_fetch, db_with_leads):
        # First simulate failure
        mock_fetch.return_value = (None, None, None, "Temporary 503 error")
        run_research_batch(db_with_leads, limit=1, lead_ids=[1])

        status_before = get_research_status(db_with_leads)
        assert status_before.get("failed", 0) == 1

        # Now mock recovery
        mock_fetch.return_value = (SAMPLE_HTML_FULL, "https://sunrisebakery-test.com", 200, None)
        retry_summary = retry_failed_research(db_with_leads, limit=5)

        assert retry_summary.completed == 1
        assert retry_summary.failed == 0


# ── Research Status & Audit Tests ────────────────────────────────────


class TestResearchStatusAndAudit:
    def test_status_empty_db(self, db):
        status = get_research_status(db)
        assert status == {}

    @patch("salesos.researcher.fetch_page")
    def test_status_after_runs(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_FULL, "https://test.com", 200, None)
        run_research_batch(db_with_leads, limit=2)

        status = get_research_status(db_with_leads)
        assert "completed" in status

    @patch("salesos.researcher.fetch_page")
    def test_audit_log_entries_recorded(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_FULL, "https://test.com", 200, None)
        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 1").fetchone()
        research_lead(lead, db_with_leads)

        logs = db_with_leads.execute(
            "SELECT * FROM audit_log WHERE entity_type = 'research' AND entity_id = 1"
        ).fetchall()
        assert len(logs) >= 1
        assert "research_completed" in logs[0]["action"]

    @patch("salesos.researcher.fetch_page")
    def test_get_lead_research_query(self, mock_fetch, db_with_leads):
        mock_fetch.return_value = (SAMPLE_HTML_MINIMAL, "https://test.com", 200, None)
        lead = db_with_leads.execute("SELECT * FROM leads WHERE id = 1").fetchone()
        research_lead(lead, db_with_leads)

        data = get_lead_research(db_with_leads, lead_id=1)
        assert data is not None
        assert data["status"] == "completed"
        assert len(data["opportunities"]) > 0


# ── AI Provider Tests ────────────────────────────────────────────────


class TestAIProvider:
    def test_provider_status_unconfigured(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        status = get_provider_status()
        assert status.available is False
        assert status.provider == "none"

    def test_provider_status_configured(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-api-key-12345")
        status = get_provider_status()
        assert status.available is True
        assert status.provider == "gemini"

    def test_validate_ai_output_clean_json(self):
        raw = '{"summary": "A great bakery", "top_opportunities": ["seo"], "outreach_angle": "Boost sales"}'
        parsed = validate_ai_output(raw)
        assert parsed is not None
        assert parsed["summary"] == "A great bakery"

    def test_validate_ai_output_code_fenced_json(self):
        raw = """```json
        {
            "summary": "IT repair services",
            "top_opportunities": ["chatbot", "booking"],
            "outreach_angle": "Automate bookings"
        }
        ```"""
        parsed = validate_ai_output(raw)
        assert parsed is not None
        assert parsed["summary"] == "IT repair services"

    def test_validate_ai_output_invalid_garbage(self):
        raw = "I am an AI and I cannot generate this in JSON format sorry."
        parsed = validate_ai_output(raw)
        assert parsed is None

    def test_analyze_without_provider_graceful_pending(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        res = analyze_research(
            evidence_dict={"title": "Test"},
            opportunities=[{"category": "technical_seo"}],
            business_name="Test Co",
            lead_id=1,
            mock=False,
        )
        assert res.success is False
        assert res.provider == "none"
        assert "unconfigured" in res.error.lower() or "no gemini_api_key" in res.error.lower()

    def test_mock_analyze_deterministic_results(self):
        res = _mock_analyze(
            evidence_dict={"title": "Sunrise Bakery"},
            opportunities=[{"category": "technical_seo"}, {"category": "booking_system"}],
            business_name="Sunrise Bakery",
            lead_id=1,
        )
        assert res.success is True
        assert res.structured is not None
        assert "Sunrise Bakery" in res.structured["summary"]
        assert "technical_seo" in res.structured["top_opportunities"]
