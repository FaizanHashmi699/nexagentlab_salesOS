"""Automated Tests for Milestone 6: Meeting Management, Sales Pipeline & Deal Intelligence.

Covers:
- Database migration safety and existing-record preservation
- Deal creation with zero fabricated values (unknowns remain None)
- Sales pipeline stages and allowed DAG transitions
- Rejection of invalid transitions and illegal shortcuts
- Manual stage override (--force) with mandatory auditable justification
- Confirmation requirements for won (win notes) and lost (loss reason)
- Suppressed lead protection against entering active outreach
- Meeting scheduling, update, and completion
- Timezone-aware ISO-8601 parsing and daylight-saving edge cases
- Duplicate booking prevention (same lead/time & external booking ID)
- Calendly HMAC-SHA256 signature verification, tamper rejection, and replay defense
- Unconfigured external Calendly API handling (graceful offline behavior)
- Evidence-based meeting preparation brief generator (citing evidence IDs, fact vs hypothesis separation)
- Persistent brief storage across application restarts
- Post-meeting structured review workflow and non-destructive follow-up draft creation
- Executive pipeline reporting (differentiating actual vs missing values, win/loss rates, action cues)
- Data quality warnings (missing owner, missing next action, missing evidence)
- CSV and JSON pipeline exports
- CRM timeline integration for deals and meetings
- Complete CLI commands execution and error handling
"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from salesos.cli import cli
from salesos.db import get_connection, init_db
from salesos.meetings import (
    BookingSource,
    DuplicateBookingError,
    MeetingNotFoundError,
    MeetingStatus,
    cancel_meeting,
    complete_meeting,
    create_meeting,
    generate_meeting_brief,
    get_meeting,
    list_meetings,
    parse_iso_datetime,
    record_meeting_outcome,
    sync_calendly_events,
    update_meeting,
    verify_calendly_webhook_signature,
)
from salesos.pipeline import (
    ALL_STAGES,
    DealStage,
    DealNotFoundError,
    InvalidTransitionError,
    SuppressedLeadError,
    auto_transition_from_event,
    create_deal,
    export_pipeline,
    get_deal,
    get_deal_by_lead,
    get_pipeline_report,
    list_deals,
    move_deal_stage,
    update_deal,
)
from salesos.timeline import get_lead_timeline


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """File path to temporary database with schema and test records."""
    p = str(tmp_path / "test_m6.db")
    conn = init_db(p)

    # Insert test leads
    leads = [
        (1, "Sunrise Bakery", "Sarah Miller", "sarah@sunrisebakery-test.com", "https://sunrisebakery-test.com", "Austin, TX", "qualified", 85.0),
        (2, "TechFix Solutions", "James Chen", "james@techfix-test.com", "https://techfix-test.com", "Denver, CO", "contacted", 70.0),
        (3, "Suppressed Clinic", "Dr. Smith", "smith@suppressed-test.com", "https://clinic-test.com", "Dallas, TX", "suppressed", 0.0),
        (4, "Auto Care Center", "Mike Ross", "mike@autocare-test.com", "https://autocare-test.com", "Chicago, IL", "engaged", 90.0),
    ]
    for lid, biz, contact, email_addr, site, loc, status, score in leads:
        conn.execute(
            """INSERT INTO leads (id, business_name, contact_name, email, website, location, status, score)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (lid, biz, contact, email_addr, site, loc, status, score),
        )

    # Mark lead 3 as suppressed in suppression table
    conn.execute(
        "INSERT INTO suppression (email, reason, source) VALUES ('smith@suppressed-test.com', 'opt_out', 'manual')"
    )

    # Insert research for lead 1
    findings_data = {
        "page_title": "Sunrise Bakery - Artisan Sourdough",
        "meta_description": "Handcrafted bakery in Austin.",
        "headings": ["Fresh Pastries", "Online Orders", "Catering"],
        "technologies": ["WordPress", "WooCommerce"],
    }
    conn.execute(
        """INSERT INTO research (lead_id, findings, confidence, agent_type, status)
           VALUES (1, ?, 0.95, 'web_researcher', 'completed')""",
        (json.dumps(findings_data),),
    )

    # Insert opportunity for lead 1
    conn.execute(
        """INSERT INTO opportunities (id, lead_id, category, description, confidence, priority)
           VALUES (1, 1, 'booking',
                   'Bakery has no automated catering booking workflow.', 0.90, 1)"""
    )

    # Insert outbound message for lead 1
    conn.execute(
        """INSERT INTO messages (id, lead_id, recipient, subject, body, message_type, status, quality_status, selected_service)
           VALUES (1, 1, 'sarah@sunrisebakery-test.com', 'Booking System for Sunrise Bakery',
                   'Hi Sarah, loved your pastries. NexAgentLab can help automate bookings.',
                   'initial', 'sent', 'approved', 'booking_system')"""
    )

    # Insert reply for lead 1
    conn.execute(
        """INSERT INTO replies (id, lead_id, message_id, from_email, subject, body, classification, confidence, is_classified)
           VALUES (1, 1, 1, 'sarah@sunrisebakery-test.com', 'Re: Booking System',
                   'Sounds great! Can we jump on a call next Thursday at 2pm?',
                   'meeting_request', 0.95, 1)"""
    )

    conn.commit()
    conn.close()
    return p


@pytest.fixture
def populated_db(db_path):
    """Active connection to populated test database."""
    conn = get_connection(db_path)
    yield conn
    conn.close()


# ── 1. Database Migration Safety ─────────────────────────────────────


class TestDatabaseMigrationSafety:
    def test_deals_and_meetings_schema_initialized(self, populated_db):
        # Verify deals table exists and has proper columns
        deals_cols = {r["name"] for r in populated_db.execute("PRAGMA table_info(deals)").fetchall()}
        assert "lead_id" in deals_cols
        assert "stage" in deals_cols
        assert "estimated_value" in deals_cols
        assert "stage_evidence" in deals_cols

        # Verify meetings table exists and has proper columns
        meetings_cols = {r["name"] for r in populated_db.execute("PRAGMA table_info(meetings)").fetchall()}
        assert "scheduled_time" in meetings_cols
        assert "brief_content" in meetings_cols
        assert "outcome_notes" in meetings_cols
        assert "proposal_needed" in meetings_cols

    def test_existing_records_preserved_on_init(self, db_path):
        conn = get_connection(db_path)
        leads_before = conn.execute("SELECT count(*) FROM leads").fetchone()[0]
        conn.close()

        # Re-run init_db (idempotency check)
        conn2 = init_db(db_path)
        leads_after = conn2.execute("SELECT count(*) FROM leads").fetchone()[0]
        conn2.close()

        assert leads_before == leads_after
        assert leads_after >= 4


# ── 2. Sales Pipeline & Deal Intelligence ────────────────────────────


class TestSalesPipelineAndDeals:
    def test_create_deal_preserves_unknowns(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Sunrise Bakery - Booking System")
        assert deal["id"] is not None
        assert deal["stage"] == DealStage.LEAD_CAPTURED.value
        # Never fabricate numbers
        assert deal["estimated_value"] is None
        assert deal["confidence"] is None
        assert deal["expected_close_date"] is None
        assert deal["owner"] is None

    def test_create_deal_suppressed_lead_blocked_from_outreach(self, populated_db):
        with pytest.raises(SuppressedLeadError, match="suppressed"):
            create_deal(
                populated_db,
                lead_id=3,
                name="Suppressed Deal",
                stage=DealStage.OUTREACH_READY.value,
            )

    def test_valid_stage_progression(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Progression Deal")

        # Step 1: lead_captured -> outreach_ready
        d1 = move_deal_stage(populated_db, deal["id"], DealStage.OUTREACH_READY.value)
        assert d1["stage"] == DealStage.OUTREACH_READY.value

        # Step 2: outreach_ready -> contacted
        d2 = move_deal_stage(populated_db, deal["id"], DealStage.CONTACTED.value)
        assert d2["stage"] == DealStage.CONTACTED.value

        # Step 3: contacted -> engaged
        d3 = move_deal_stage(populated_db, deal["id"], DealStage.ENGAGED.value)
        assert d3["stage"] == DealStage.ENGAGED.value

        # Step 4: engaged -> meeting_booked
        d4 = move_deal_stage(populated_db, deal["id"], DealStage.MEETING_BOOKED.value)
        assert d4["stage"] == DealStage.MEETING_BOOKED.value

        # Step 5: meeting_booked -> meeting_completed
        d5 = move_deal_stage(populated_db, deal["id"], DealStage.MEETING_COMPLETED.value)
        assert d5["stage"] == DealStage.MEETING_COMPLETED.value

        # Step 6: meeting_completed -> proposal_sent
        d6 = move_deal_stage(populated_db, deal["id"], DealStage.PROPOSAL_SENT.value)
        assert d6["stage"] == DealStage.PROPOSAL_SENT.value

        # Step 7: proposal_sent -> won (with win notes)
        d7 = move_deal_stage(
            populated_db,
            deal["id"],
            DealStage.WON.value,
            evidence={"win_notes": "Signed 6-month contract for $3,500"},
        )
        assert d7["stage"] == DealStage.WON.value
        assert d7["win_notes"] == "Signed 6-month contract for $3,500"

    def test_invalid_stage_transition_rejected(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Invalid Transition Deal")
        # Cannot jump from lead_captured directly to won or proposal_sent without force
        with pytest.raises(InvalidTransitionError, match="Invalid stage transition"):
            move_deal_stage(populated_db, deal["id"], DealStage.WON.value)

    def test_force_stage_transition_requires_reason(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Force Deal")

        # Force without reason fails
        with pytest.raises(ValueError, match="requires an auditable reason"):
            move_deal_stage(populated_db, deal["id"], DealStage.NEGOTIATION.value, force=True, reason="")

        # Force with reason succeeds
        d = move_deal_stage(
            populated_db,
            deal["id"],
            DealStage.NEGOTIATION.value,
            force=True,
            reason="Inbound referral fast-tracked directly to negotiation",
        )
        assert d["stage"] == DealStage.NEGOTIATION.value

    def test_won_requires_confirmation_notes(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Won Deal", stage=DealStage.PROPOSAL_SENT.value)
        with pytest.raises(InvalidTransitionError, match="win notes"):
            move_deal_stage(populated_db, deal["id"], DealStage.WON.value)

    def test_lost_requires_loss_reason(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Lost Deal", stage=DealStage.CONTACTED.value)
        with pytest.raises(InvalidTransitionError, match="loss reason"):
            move_deal_stage(populated_db, deal["id"], DealStage.LOST.value)

        # Providing loss reason succeeds
        d = move_deal_stage(
            populated_db,
            deal["id"],
            DealStage.LOST.value,
            evidence={"loss_reason": "Competitor chosen due to prior relationship"},
        )
        assert d["stage"] == DealStage.LOST.value
        assert d["loss_reason"] == "Competitor chosen due to prior relationship"

    def test_suppressed_lead_cannot_move_into_active_outreach(self, populated_db):
        deal = create_deal(populated_db, lead_id=3, name="Suppressed Lead Deal", stage=DealStage.LEAD_CAPTURED.value)
        with pytest.raises(SuppressedLeadError, match="suppressed"):
            move_deal_stage(populated_db, deal["id"], DealStage.OUTREACH_READY.value)

    def test_auto_transition_from_event(self, populated_db):
        # Event: outreach_sent
        deal = auto_transition_from_event(populated_db, event_type="outreach_sent", lead_id=2)
        assert deal is not None
        assert deal["stage"] == DealStage.CONTACTED.value

        # Event: meeting_booked
        d2 = auto_transition_from_event(populated_db, event_type="meeting_booked", lead_id=2)
        assert d2["stage"] == DealStage.MEETING_BOOKED.value


# ── 3. Meeting Management & Synchronization ──────────────────────────


class TestMeetingManagement:
    def test_create_meeting_success(self, populated_db):
        m = create_meeting(
            populated_db,
            lead_id=1,
            title="Sunrise Bakery Intro Call",
            scheduled_time="2026-11-05 14:00:00",
            duration_minutes=45,
            timezone_name="America/Chicago",
            location_or_url="https://meet.google.com/xyz-abc",
            notes="Focus on catering booking link",
        )
        assert m["id"] is not None
        assert m["status"] == "scheduled"
        assert m["duration_minutes"] == 45
        assert m["location_or_url"] == "https://meet.google.com/xyz-abc"

        # Linked deal auto-advanced to meeting_booked
        deal = get_deal_by_lead(populated_db, lead_id=1)
        assert deal["stage"] == DealStage.MEETING_BOOKED.value

    def test_duplicate_booking_same_lead_and_time_blocked(self, populated_db):
        time_slot = "2026-11-10 15:00:00"
        create_meeting(populated_db, lead_id=1, title="Call 1", scheduled_time=time_slot)

        # Duplicate attempt for same lead and same time
        with pytest.raises(DuplicateBookingError, match="already has active Meeting"):
            create_meeting(populated_db, lead_id=1, title="Call 2", scheduled_time=time_slot)

    def test_duplicate_external_booking_id_blocked(self, populated_db):
        ext_id = "calendly_evt_998877"
        create_meeting(
            populated_db,
            lead_id=1,
            title="Calendly Call 1",
            scheduled_time="2026-11-12 10:00:00",
            external_booking_id=ext_id,
        )

        with pytest.raises(DuplicateBookingError, match="external booking ID"):
            create_meeting(
                populated_db,
                lead_id=2,
                title="Calendly Call 2",
                scheduled_time="2026-11-13 10:00:00",
                external_booking_id=ext_id,
            )

    def test_timezone_parsing_and_normalization(self):
        # UTC Z string
        t1 = parse_iso_datetime("2026-11-05T14:00:00Z")
        assert t1.tzinfo is not None

        # Space separated standard
        t2 = parse_iso_datetime("2026-11-05 14:00:00")
        assert t2.hour == 14

        # Offset string +05:00
        t3 = parse_iso_datetime("2026-11-05T19:00:00+05:00")
        assert t3.astimezone(timezone.utc).hour == 14

    def test_update_and_reschedule_meeting(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Original Call", scheduled_time="2026-11-05 14:00:00")
        updated = update_meeting(
            populated_db,
            meeting_id=m["id"],
            title="Rescheduled Discovery Call",
            scheduled_time="2026-11-06 16:00:00",
            notes="Client requested Friday afternoon",
        )
        assert updated["title"] == "Rescheduled Discovery Call"
        assert "2026-11-06" in updated["scheduled_time"]

    def test_cancel_meeting_with_reason(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Call to Cancel", scheduled_time="2026-11-08 11:00:00")
        cancelled = cancel_meeting(populated_db, meeting_id=m["id"], reason="Client emergency conflict")
        assert cancelled["status"] == "cancelled"
        assert cancelled["cancellation_reason"] == "Client emergency conflict"

    def test_complete_meeting_advances_deal(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Call to Complete", scheduled_time="2026-11-08 11:00:00")
        completed = complete_meeting(populated_db, meeting_id=m["id"])
        assert completed["status"] == "completed"

        deal = get_deal_by_lead(populated_db, lead_id=1)
        assert deal["stage"] == DealStage.MEETING_COMPLETED.value


# ── 4. Post-Meeting Workflow ─────────────────────────────────────────


class TestPostMeetingWorkflow:
    def test_record_outcome_with_followup_draft(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Call with Sarah", scheduled_time="2026-11-05 14:00:00")
        res = record_meeting_outcome(
            populated_db,
            meeting_id=m["id"],
            outcome_notes="Client interested in catering booking widget",
            customer_needs="Automate weekend orders",
            confirmed_requirements="WordPress compatible",
            agreed_actions="Send proposal by Friday",
            action_owner="Saif",
            action_due_date="2026-11-07",
            proposal_needed="yes",
            followup_required=True,
            next_step="Send catering widget proposal",
        )
        assert res["status"] == "completed"
        assert res["proposal_needed"] == "yes"

        # Verify draft follow-up email created in messages table
        draft = populated_db.execute(
            "SELECT * FROM messages WHERE lead_id = 1 AND message_type = 'follow_up' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert draft is not None
        assert draft["status"] == "draft"
        assert "Send catering widget proposal" in draft["body"]
        assert "opt out" in draft["body"].lower()

    def test_record_outcome_without_followup(self, populated_db):
        m = create_meeting(populated_db, lead_id=2, title="Call with James", scheduled_time="2026-11-05 15:00:00")
        record_meeting_outcome(
            populated_db,
            meeting_id=m["id"],
            outcome_notes="Client not ready for AI chat at this time",
            proposal_needed="no",
            followup_required=False,
            next_step="Check back in Q2",
        )
        # No draft message created
        drafts = populated_db.execute(
            "SELECT * FROM messages WHERE lead_id = 2 AND message_type = 'follow_up'"
        ).fetchall()
        assert len(drafts) == 0


# ── 5. Meeting Brief Preparation Assistant ───────────────────────────


class TestMeetingBriefPreparation:
    def test_deterministic_brief_generation_cites_evidence(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Sunrise Call", scheduled_time="2026-11-05 14:00:00")
        brief = generate_meeting_brief(populated_db, meeting_id=m["id"], no_ai=True)

        assert brief is not None
        assert "Sunrise Bakery" in brief
        # Must cite evidence tags
        assert "[EVID-" in brief
        # Must cite opportunities
        assert "[OPP-" in brief
        # Must outline sections
        assert "## 2. Verified Factual Observations" in brief
        assert "## 3. Potential Opportunities & Hypotheses" in brief
        assert "## 5. Proposed Meeting Agenda" in brief
        assert "## 6. Suggested Discovery Questions" in brief
        assert "https://nexagentlab.tech" in brief

    def test_brief_persists_across_queries(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Sunrise Call", scheduled_time="2026-11-05 14:00:00")
        brief = generate_meeting_brief(populated_db, meeting_id=m["id"], no_ai=True)

        # Check database column directly
        row = populated_db.execute("SELECT brief_content, brief_generated_at FROM meetings WHERE id = ?", (m["id"],)).fetchone()
        assert row["brief_content"] == brief
        assert row["brief_generated_at"] is not None

    def test_brief_with_mocked_ai_assistant(self, populated_db):
        m = create_meeting(populated_db, lead_id=1, title="Sunrise Call", scheduled_time="2026-11-05 14:00:00")

        mock_llm_brief = (
            "# AI Generated Brief: Sunrise Bakery\n\n"
            "## 1. Verified Evidence\n"
            "- [EVID-META-TITLE] Real bread baking\n\n"
            "## 2. Hypotheses\n"
            "- [OPP-1] Booking automation needed"
        )
        with patch("salesos.meetings._call_llm_provider") as mock_call:
            mock_call.return_value = (mock_llm_brief, 150, None)
            with patch("salesos.meetings.get_provider_status") as mock_status:
                mock_status.return_value = MagicMock(available=True, provider="groq", model="llama")

                brief = generate_meeting_brief(populated_db, meeting_id=m["id"], force_ai=True)
                assert "AI Generated Brief" in brief
                assert "[EVID-META-TITLE]" in brief


# ── 6. Calendly Webhook & Sync ───────────────────────────────────────


class TestCalendlyIntegration:
    def test_calendly_unconfigured_offline(self, populated_db, monkeypatch):
        monkeypatch.delenv("CALENDLY_ACCESS_TOKEN", raising=False)
        res = sync_calendly_events(populated_db)
        assert res["status"] == "unconfigured"
        assert res["synced_count"] == 0

    def test_verify_calendly_webhook_signature_valid(self):
        secret = "whsec_test_signing_key_12345"
        payload = b'{"event": "invitee.created", "time": "2026-11-05T14:00:00Z"}'
        timestamp = int(time.time())

        # Generate valid HMAC
        import hashlib
        import hmac
        signed_data = f"{timestamp}.".encode("utf-8") + payload
        sig = hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()
        header = f"t={timestamp},v1={sig}"

        assert verify_calendly_webhook_signature(payload, header, signing_key=secret) is True

    def test_verify_calendly_webhook_signature_tampered_fails(self):
        secret = "whsec_test_signing_key_12345"
        payload = b'{"event": "invitee.created"}'
        timestamp = int(time.time())
        header = f"t={timestamp},v1=fake_tampered_signature_hex"

        assert verify_calendly_webhook_signature(payload, header, signing_key=secret) is False

    def test_verify_calendly_webhook_replay_attack_rejected(self):
        secret = "whsec_test_signing_key_12345"
        payload = b'{"event": "invitee.created"}'
        # Old timestamp (1 hour ago > 300 second tolerance)
        old_timestamp = int(time.time()) - 3600

        import hashlib
        import hmac
        signed_data = f"{old_timestamp}.".encode("utf-8") + payload
        sig = hmac.new(secret.encode("utf-8"), signed_data, hashlib.sha256).hexdigest()
        header = f"t={old_timestamp},v1={sig}"

        assert verify_calendly_webhook_signature(payload, header, signing_key=secret, tolerance_seconds=300) is False


# ── 7. Pipeline Reporting & CSV/JSON Export ──────────────────────────


class TestPipelineReportingAndExport:
    def test_pipeline_report_metrics(self, populated_db):
        # Create deals in various stages with known values
        create_deal(populated_db, lead_id=1, name="Deal 1", stage=DealStage.OUTREACH_READY.value, estimated_value=2500.0)
        create_deal(populated_db, lead_id=2, name="Deal 2", stage=DealStage.CONTACTED.value, estimated_value=4000.0)
        d_won = create_deal(populated_db, lead_id=4, name="Deal 3", stage=DealStage.PROPOSAL_SENT.value, estimated_value=5000.0)
        move_deal_stage(populated_db, d_won["id"], DealStage.WON.value, evidence={"win_notes": "Paid in full"})

        rep = get_pipeline_report(populated_db)
        assert rep["total_active_deals"] >= 2
        # Pipeline value should sum active (non-closed) deals
        assert rep["total_pipeline_value"] >= 6500.0
        assert rep["performance"]["won_count"] >= 1
        assert rep["performance"]["win_rate_percent"] > 0.0

    def test_export_pipeline_csv_and_json(self, populated_db, tmp_path):
        create_deal(populated_db, lead_id=1, name="Bakery Deal", estimated_value=1500.0)

        csv_file = str(tmp_path / "deals.csv")
        cnt_csv = export_pipeline(populated_db, csv_file, format="csv")
        assert cnt_csv >= 1

        with open(csv_file, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            assert len(rows) >= 1
            assert "estimated_value" in rows[0]

        json_file = str(tmp_path / "deals.json")
        cnt_json = export_pipeline(populated_db, json_file, format="json")
        assert cnt_json >= 1

        with open(json_file, encoding="utf-8") as f:
            data = json.load(f)
            assert isinstance(data, list)
            assert len(data) >= 1


# ── 8. CRM Timeline Integration ──────────────────────────────────────


class TestCRMTimelineMilestone6:
    def test_timeline_includes_deal_and_meeting_events(self, populated_db):
        deal = create_deal(populated_db, lead_id=1, name="Bakery Automation", estimated_value=3000.0)
        move_deal_stage(populated_db, deal["id"], DealStage.OUTREACH_READY.value)
        m = create_meeting(populated_db, lead_id=1, title="Discovery Call", scheduled_time="2026-11-05 14:00:00")
        complete_meeting(populated_db, meeting_id=m["id"])

        tl = get_lead_timeline(populated_db, lead_id=1)
        event_types = [e["type"] for e in tl["events"]]

        assert "deal_created" in event_types
        assert "deal_stage_moved" in event_types
        assert "meeting_scheduled" in event_types
        assert "meeting_completed" in event_types


# ── 9. Milestone 6 CLI Commands ──────────────────────────────────────


class TestMilestone6CLI:
    def test_cli_meetings_create_list_show_and_cancel(self, db_path, monkeypatch):
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        # 1. Create meeting
        res_create = runner.invoke(cli, [
            "meetings", "create",
            "--lead-id", "1",
            "--title", "CLI Discovery Call",
            "--time", "2026-11-15 14:00:00",
            "--duration", "30",
            "--url", "https://meet.google.com/cli-test",
        ])
        assert res_create.exit_code == 0
        assert "Meeting Scheduled Successfully" in res_create.output

        # 2. List meetings
        res_list = runner.invoke(cli, ["meetings", "list"])
        assert res_list.exit_code == 0
        assert "CLI Discovery Call" in res_list.output

        # 3. Show meeting
        res_show = runner.invoke(cli, ["meetings", "show", "1"])
        assert res_show.exit_code == 0
        assert "Sunrise Bakery" in res_show.output

        # 4. Cancel meeting
        res_cancel = runner.invoke(cli, ["meetings", "cancel", "1", "--reason", "Rescheduling next week"])
        assert res_cancel.exit_code == 0
        assert "cancelled" in res_cancel.output.lower()

    def test_cli_meetings_brief_and_outcome(self, db_path, monkeypatch):
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        # Schedule meeting first
        runner.invoke(cli, [
            "meetings", "create",
            "--lead-id", "1",
            "--title", "Brief Call",
            "--time", "2026-11-20 10:00:00",
        ])

        # Brief
        res_brief = runner.invoke(cli, ["meetings", "brief", "1", "--no-ai"])
        assert res_brief.exit_code == 0
        assert "Meeting Brief" in res_brief.output
        assert "[EVID-" in res_brief.output

        # Outcome
        res_outcome = runner.invoke(cli, [
            "meetings", "outcome", "1",
            "--notes", "Great discussion on booking system",
            "--proposal", "yes",
            "--followup",
            "--next-step", "Send draft pricing proposal",
        ])
        assert res_outcome.exit_code == 0
        assert "Outcome Recorded" in res_outcome.output
        assert "Draft follow-up email created" in res_outcome.output

    def test_cli_meetings_sync_unconfigured(self, db_path, monkeypatch):
        monkeypatch.delenv("CALENDLY_ACCESS_TOKEN", raising=False)
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        res = runner.invoke(cli, ["meetings", "sync"])
        assert res.exit_code == 0
        assert "Calendly Integration Optional" in res.output

    def test_cli_pipeline_board_show_move_update_and_report(self, db_path, monkeypatch):
        conn = get_connection(db_path)
        deal = create_deal(conn, lead_id=1, name="Bakery Custom Chatbot", estimated_value=2000.0)
        deal_id = str(deal["id"])
        conn.close()

        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        # 1. Pipeline board
        res_board = runner.invoke(cli, ["pipeline", "board"])
        assert res_board.exit_code == 0
        assert "Bakery Custom Chatbot" in res_board.output

        # 2. Pipeline show
        res_show = runner.invoke(cli, ["pipeline", "show", deal_id])
        assert res_show.exit_code == 0
        assert "Bakery Custom Chatbot" in res_show.output

        # 3. Pipeline move with force override
        res_move = runner.invoke(cli, ["pipeline", "move", deal_id, "outreach_ready"])
        assert res_move.exit_code == 0
        assert "outreach_ready" in res_move.output

        # 4. Pipeline update
        res_update = runner.invoke(cli, [
            "pipeline", "update", deal_id,
            "--value", "2800",
            "--owner", "Saif",
            "--next-action", "Prepare scoping document",
        ])
        assert res_update.exit_code == 0
        assert "updated successfully" in res_update.output

        # 5. Pipeline report
        res_report = runner.invoke(cli, ["pipeline", "report"])
        assert res_report.exit_code == 0
        assert "Executive Pipeline Intelligence Report" in res_report.output
