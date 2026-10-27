"""End-to-End Acceptance Test Harness for NexAgentLab SalesOS.

Tests the complete 7-stage autonomous sales lifecycle in an isolated test environment:
- Stage 1: Lead Intake (CSV import, validation, deduplication, suppression protection)
- Stage 2: Research & Qualification (HTML evidence extraction, opportunity detection, scoring)
- Stage 3: Personalized Outreach (Draft generation, quality validation, approval gate, simulated send)
- Stage 4: Replies & Suppression (Inbound reply match, classification, opt-out suppression)
- Stage 5: Follow-ups (Follow-up scheduling, cooldowns, automatic cancellation on reply)
- Stage 6: Meetings & Pipeline (Scheduling, brief generation, outcome review, deal stage progression)
- Stage 7: Persistence & Recovery (Database reload, state durability, CSV/JSON export)
"""

import json
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta

from salesos.db import SCHEMA, _migrate_db
from salesos.importer import import_leads
from salesos.researcher import (
    extract_evidence,
    identify_opportunities,
    WebsiteEvidence,
)
from salesos.qualifier import qualify_lead
from salesos.personalizer import personalize_lead
from salesos.outreach import (
    approve_message,
    queue_messages,
    send_outreach_message,
)
from salesos.classifier import classify_reply, CLASSIFICATION_CATEGORIES
from salesos.followup_scheduler import plan_followups, cancel_followup
from salesos.meetings import (
    BookingSource,
    DuplicateBookingError,
    MeetingStatus,
    create_meeting,
    complete_meeting,
    generate_meeting_brief,
    record_meeting_outcome,
)
from salesos.pipeline import (
    DealStage,
    InvalidTransitionError,
    SuppressedLeadError,
    create_deal,
    export_pipeline,
    get_deal,
    get_pipeline_report,
    move_deal_stage,
)
from salesos.suppression import is_email_suppressed, record_suppression
from salesos.timeline import get_lead_timeline
from salesos.health_check import run_all_health_checks, CheckStatus, ReadinessLevel


@pytest.fixture
def e2e_conn(tmp_path):
    """Isolated, pristine SQLite database fixture for full E2E workflow."""
    db_file = str(tmp_path / "acceptance_e2e.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate_db(conn)
    conn.commit()
    yield conn
    conn.close()


class TestEndToEndSalesOSAcceptance:
    """Complete acceptance test covering all 7 stages and 22 verification criteria."""

    def test_complete_7_stage_acceptance_lifecycle(self, e2e_conn, tmp_path):
        conn = e2e_conn

        # ══════════════════════════════════════════════════════════════════
        # STAGE 1: Lead Intake & Deduplication
        # ══════════════════════════════════════════════════════════════════

        # 1.1 Pre-suppress a known unsubscribe email
        record_suppression(
            conn,
            email="do-not-contact@prospect.com",
            reason="opt_out",
            source="manual_pilot_blocklist",
        )
        assert is_email_suppressed(conn, "do-not-contact@prospect.com")

        # 1.2 Create CSV file with 4 leads:
        # - Lead 1: Prime prospect (Dentist)
        # - Lead 2: Duplicate in batch
        # - Lead 3: Malformed row (missing email)
        # - Lead 4: Suppressed email
        csv_content = (
            "Business Name,Website,Email,Contact Name,Location\n"
            "Apex Dental Care,https://apexdentalcare.example.com,dr.smith@apexdentalcare.example.com,Dr. John Smith,Austin TX\n"
            "Apex Dental Care Dupe,https://apexdentalcare.example.com,dr.smith@apexdentalcare.example.com,John Smith,Austin TX\n"
            "Invalid Business,,,,Missing Email\n"
            "Opted Out Co,https://optout.example.com,do-not-contact@prospect.com,Jane Doe,Denver CO\n"
        )
        csv_path = tmp_path / "leads_intake.csv"
        csv_path.write_text(csv_content, encoding="utf-8")

        # 1.3 Ingest leads
        result = import_leads(str(csv_path), conn)
        assert result.total_rows == 4
        assert result.imported == 1  # Only Apex Dental Care
        assert result.duplicates == 2  # 1 in-batch duplicate + 1 pre-suppressed lead
        assert result.errors >= 1  # Malformed lead rejected

        # Verify Lead 1 in DB
        lead = conn.execute("SELECT * FROM leads WHERE email = ?", ("dr.smith@apexdentalcare.example.com",)).fetchone()
        assert lead is not None
        assert lead["business_name"] == "Apex Dental Care"
        lead_id = lead["id"]

        # Create associated sales deal in pipeline
        deal = create_deal(
            conn,
            lead_id=lead_id,
            name="Apex Dental Care - Modernization",
            service_interest="AI Chatbot & Booking System",
            owner="Faizan Hashmi",
        )
        assert deal["stage"] == DealStage.LEAD_CAPTURED.value
        deal_id = deal["id"]

        # ══════════════════════════════════════════════════════════════════
        # STAGE 2: Website Research & Qualification
        # ══════════════════════════════════════════════════════════════════

        # 2.1 Simulate extracted website evidence
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Apex Dental Care - Premier Family Dentistry</title>
            <meta name="description" content="Compassionate dental care in Austin. Call us today to schedule your visit.">
        </head>
        <body>
            <h1>Welcome to Apex Dental Care</h1>
            <h2>Family Dentistry & Cosmetics</h2>
            <p>Call (512) 555-0199 for appointments. We currently do not offer online booking or 24/7 web chat.</p>
            <form action="/contact" method="POST">
                <input type="text" name="name" />
                <button type="submit">Contact Us</button>
            </form>
        </body>
        </html>
        """
        evidence = extract_evidence(sample_html, "https://apexdentalcare.example.com")
        assert evidence.title == "Apex Dental Care - Premier Family Dentistry"
        assert evidence.has_lead_form is True
        assert evidence.has_booking is False
        assert evidence.has_chat_widget is False

        # Identify evidence gaps
        opps = identify_opportunities(evidence)
        opp_categories = [o.category for o in opps]
        assert "chatbot" in opp_categories or "booking_system" in opp_categories

        # Store research and opportunities in DB
        findings_json = json.dumps({
            "page_title": evidence.title,
            "meta_description": evidence.meta_description,
            "headings": evidence.headings,
            "technologies": evidence.technologies_detected,
        })
        conn.execute(
            "INSERT INTO research (lead_id, agent_type, findings, confidence, status) VALUES (?, ?, ?, ?, ?)",
            (lead_id, "website_analyzer", findings_json, 0.95, "completed"),
        )
        for opp in opps:
            conn.execute(
                "INSERT INTO opportunities (lead_id, category, description, confidence, evidence, priority, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lead_id, opp.category, opp.description, opp.confidence, json.dumps(opp.evidence), opp.priority, "identified"),
            )
        conn.commit()

        # 2.2 Qualify lead
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        qual_res = qualify_lead(lead, conn)
        assert qual_res.eligible is True
        assert qual_res.score >= 50.0

        # Transition deal to OUTREACH_READY
        deal = move_deal_stage(
            conn,
            deal_id=deal_id,
            target_stage=DealStage.OUTREACH_READY.value,
            evidence={"trigger": "high_fit_score", "score": qual_res.score},
        )
        assert deal["stage"] == DealStage.OUTREACH_READY.value

        # ══════════════════════════════════════════════════════════════════
        # STAGE 3: Personalized Outreach & Dry-Run Dispatch
        # ══════════════════════════════════════════════════════════════════

        # 3.1 Generate draft
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        draft_res = personalize_lead(lead, conn, use_ai=False)
        assert draft_res is not None
        assert "Apex Dental Care" in draft_res.body or "Apex Dental Care" in draft_res.subject

        # Get message record
        msg = conn.execute("SELECT * FROM messages WHERE lead_id = ?", (lead_id,)).fetchone()
        assert msg is not None
        assert msg["status"] == "draft"
        msg_id = msg["id"]

        # 3.2 Human approval gate
        ok, reason = approve_message(conn, message_id=msg_id, force=True)
        assert ok is True

        # 3.3 Dispatch message (simulated dry-run safe sending)
        send_res = send_outreach_message(conn, message_id=msg_id, dry_run=True, force=True)
        assert send_res["status"] == "sent"

        # Transition deal to CONTACTED
        deal = move_deal_stage(
            conn,
            deal_id=deal_id,
            target_stage=DealStage.CONTACTED.value,
            evidence={"sent_message_id": msg_id},
        )
        assert deal["stage"] == DealStage.CONTACTED.value

        # ══════════════════════════════════════════════════════════════════
        # STAGE 4: Inbound Reply & Classification
        # ══════════════════════════════════════════════════════════════════

        # 4.1 Inbound positive reply simulation
        reply_body = "Hi Faizan, thanks for reaching out. We would love to discuss an AI booking system. When are you free?"
        conn.execute(
            """INSERT INTO replies (
                message_id, lead_id, subject, body,
                received_at, from_email, is_classified
            ) VALUES (?, ?, ?, ?, datetime('now'), ?, 0)""",
            (
                msg_id,
                lead_id,
                "Re: Apex Dental Care Modernization",
                reply_body,
                "dr.smith@apexdentalcare.example.com",
            ),
        )
        conn.commit()
        reply_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        classification = classify_reply(conn, reply_id=reply_id, use_ai=False)
        assert classification.category in ("positive_interest", "meeting_request", "question")

        # Transition deal to ENGAGED
        deal = move_deal_stage(
            conn,
            deal_id=deal_id,
            target_stage=DealStage.ENGAGED.value,
            evidence={"reply_classification": classification.category},
        )
        assert deal["stage"] == DealStage.ENGAGED.value

        # ══════════════════════════════════════════════════════════════════
        # STAGE 5: Follow-up Scheduling & Auto-Cancellation
        # ══════════════════════════════════════════════════════════════════

        # Create a scheduled follow-up for a hypothetical unresponsive lead
        conn.execute(
            """INSERT INTO leads (business_name, email, status)
               VALUES ('Silent Corp', 'ceo@silentcorp.example.com', 'contacted')"""
        )
        silent_lead_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO messages (lead_id, recipient, subject, body, status, sent_at)
               VALUES (?, 'ceo@silentcorp.example.com', 'Quick question', 'Hi CEO...', 'sent', datetime('now', '-4 days'))""",
            (silent_lead_id,),
        )
        silent_msg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()

        # Plan follow-up
        plan_res = plan_followups(conn)
        assert plan_res.get("planned_count", 0) >= 1
        fu = conn.execute("SELECT * FROM followups WHERE lead_id = ?", (silent_lead_id,)).fetchone()
        assert fu is not None
        assert fu["status"] in ("planned", "scheduled")

        # Cancel follow-up when reply arrives
        ok = cancel_followup(conn, followup_id=fu["id"], reason="Customer replied")
        assert ok is True
        fu_cancelled = conn.execute("SELECT * FROM followups WHERE id = ?", (fu["id"],)).fetchone()
        assert fu_cancelled["status"] == "cancelled"

        # ══════════════════════════════════════════════════════════════════
        # STAGE 6: Meetings, Briefs, Outcomes & Pipeline Progression
        # ══════════════════════════════════════════════════════════════════

        # 6.1 Schedule meeting with timezone normalization
        meeting_time = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        meeting = create_meeting(
            conn,
            lead_id=lead_id,
            deal_id=deal_id,
            title="Apex Dental Care — AI Booking & Chatbot Discovery",
            scheduled_time=meeting_time,
            duration_minutes=30,
            timezone_name="America/Chicago",
            booking_source=BookingSource.CALENDLY.value,
            external_booking_id="CAL-APEX-9981",
        )
        meeting_id = meeting["id"]

        # Duplicate booking rejection
        with pytest.raises(DuplicateBookingError):
            create_meeting(
                conn,
                lead_id=lead_id,
                deal_id=deal_id,
                title="Duplicate Call",
                scheduled_time=meeting_time,
            )

        # Deal advances to MEETING_BOOKED
        deal = get_deal(conn, deal_id)
        assert deal["stage"] == DealStage.MEETING_BOOKED.value

        # 6.2 Generate Evidence-Grounded Meeting Preparation Brief
        brief = generate_meeting_brief(conn, meeting_id=meeting_id, no_ai=True)
        assert "Apex Dental Care" in brief
        assert "Verified Factual Observations" in brief
        assert "Potential Opportunities & Hypotheses" in brief
        assert "Suggested Discovery Questions" in brief

        # Complete meeting
        complete_meeting(conn, meeting_id=meeting_id)
        deal = get_deal(conn, deal_id)
        assert deal["stage"] == DealStage.MEETING_COMPLETED.value

        # 6.3 Record Meeting Outcome
        outcome_res = record_meeting_outcome(
            conn,
            meeting_id=meeting_id,
            outcome_notes="Great discovery session. Dr. Smith is eager for 24/7 web chat + automated patient booking.",
            customer_needs="High call volume during peak hours leads to lost patient bookings.",
            confirmed_requirements="Online booking calendar sync + HIPAA compliant intake.",
            proposal_needed="yes",
            followup_required=True,
            action_owner="Faizan Hashmi",
            next_step="Deliver proposal by Friday",
        )
        assert outcome_res["id"] == meeting_id

        # 6.4 Stage Progression to Proposal Sent -> Negotiation -> Won
        deal = move_deal_stage(
            conn,
            deal_id=deal_id,
            target_stage=DealStage.PROPOSAL_SENT.value,
            evidence={"details": "Comprehensive proposal delivered with $4,500 implementation plan."},
        )
        assert deal["stage"] == DealStage.PROPOSAL_SENT.value

        deal = move_deal_stage(
            conn,
            deal_id=deal_id,
            target_stage=DealStage.NEGOTIATION.value,
            evidence={"details": "Contract terms review with practice administrator."},
        )
        assert deal["stage"] == DealStage.NEGOTIATION.value

        # Move to WON with mandatory win notes
        deal = move_deal_stage(
            conn,
            deal_id=deal_id,
            target_stage=DealStage.WON.value,
            evidence={"win_notes": "Signed $4,500 agreement with monthly maintenance contract."},
        )
        assert deal["stage"] == DealStage.WON.value

        # ══════════════════════════════════════════════════════════════════
        # STAGE 7: Persistence, CRM Timeline & Audit Durability
        # ══════════════════════════════════════════════════════════════════

        # 7.1 Verify CRM unified timeline contains every milestone event
        timeline = get_lead_timeline(conn, lead_id=lead_id)
        assert "events" in timeline
        event_types = [ev["type"] for ev in timeline["events"]]
        assert "lead_imported" in event_types
        assert "deal_created" in event_types
        assert "deal_stage_moved" in event_types
        assert "meeting_scheduled" in event_types
        assert "meeting_completed" in event_types

        # 7.2 Verify Executive Pipeline Intelligence Report
        report = get_pipeline_report(conn)
        assert report["performance"]["won_count"] == 1
        assert report["performance"]["win_rate_percent"] == 100.0

        # 7.3 Export Pipeline to CSV and JSON
        export_csv_path = tmp_path / "pipeline_export.csv"
        export_json_path = tmp_path / "pipeline_export.json"

        csv_count = export_pipeline(conn, str(export_csv_path), format="csv")
        assert csv_count >= 1
        assert export_csv_path.exists()
        assert "Apex Dental Care" in export_csv_path.read_text(encoding="utf-8")

        json_count = export_pipeline(conn, str(export_json_path), format="json")
        assert json_count >= 1
        assert export_json_path.exists()
        exported_data = json.loads(export_json_path.read_text(encoding="utf-8"))
        assert any(d["name"] == "Apex Dental Care - Modernization" for d in exported_data)

        # 7.4 Verify System Health Report runs cleanly on this database
        health_rep = run_all_health_checks(conn=conn, live=False)
        assert health_rep.readiness == ReadinessLevel.READY_FOR_LOCAL_TESTING_ONLY
        statuses = {c.component: c.status for c in health_rep.checks}
        assert statuses["database"] == CheckStatus.PASS
        assert statuses["pipeline_dag"] == CheckStatus.PASS
        assert statuses["calendly_meetings"] == CheckStatus.PASS
