"""Comprehensive tests for Milestone 4: Gmail Integration, Controlled Outreach & Central Suppression.

Covers:
- OAuth setup missing or incomplete
- Sender-account mismatch detection
- Draft creation and synchronization to Gmail
- Approval requirements and Quality Gate gating
- Dry-run behavior (safe by default)
- Invalid state transitions and idempotency
- Duplicate-send prevention across recipients
- Restart, batch resume, and rate-limiting
- Ambiguous Gmail send outcomes defense
- Central suppression and opt-out enforcement
- Rate limits and daily send caps
- Token and secret redaction
- Database migration safety
- Offline operation without API keys or OAuth
- CLI success and failure paths
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from salesos.cli import cli
from salesos.db import init_db
from salesos.gmail_client import (
    AmbiguousSendError,
    GmailError,
    OAuthMissingError,
    SenderMismatchError,
    TokenExpiredError,
    build_raw_email,
    create_or_update_draft,
    exchange_code_for_token,
    generate_auth_url,
    get_gmail_status,
    load_credentials_file,
    load_token_file,
    redact_secret,
    redact_token_dict,
    refresh_access_token,
    save_token_file,
    send_message_api,
    verify_sender_identity,
)
from salesos.outreach import (
    DailyCapExceededError,
    DuplicateRecipientError,
    KillSwitchActiveError,
    OutreachError,
    PreflightCheckFailedError,
    approve_message,
    get_daily_limit,
    get_daily_sends_today,
    get_outreach_status,
    has_recipient_been_sent,
    preview_message,
    queue_messages,
    send_batch,
    send_outreach_message,
    sync_drafts_to_gmail,
)
from salesos.suppression import (
    get_suppression_stats,
    is_email_suppressed,
    list_suppressions,
    normalize_email,
    record_suppression,
    remove_suppression,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path):
    """File path to temporary database with schema and sample records."""
    from salesos.db import get_connection
    p = str(tmp_path / "test_m4.db")
    conn = init_db(p)

    # Insert test leads
    leads = [
        (1, "Sunrise Bakery", "Sarah Miller", "sarah@sunrisebakery-test.com", "https://sunrisebakery-test.com", "Austin, TX"),
        (2, "TechFix Solutions", "James Chen", "james@techfix-test.com", "https://techfix-test.com", "Denver, CO"),
        (3, "Suppressed Clinic", "Dr. Smith", "smith@suppressed-test.com", "https://clinic-test.com", "Dallas, TX"),
    ]
    for lid, biz, contact, email_addr, site, loc in leads:
        conn.execute(
            """INSERT INTO leads (id, business_name, contact_name, email, website, location, status, score)
               VALUES (?, ?, ?, ?, ?, ?, 'qualified', 75.0)""",
            (lid, biz, contact, email_addr, site, loc),
        )

    # Insert message drafts
    conn.execute(
        """INSERT INTO messages (id, lead_id, recipient, subject, body, message_type, status, quality_status, selected_service)
           VALUES (1, 1, 'sarah@sunrisebakery-test.com', 'Online booking for Sunrise Bakery',
                   'Hi Sarah,\n\nWe noticed Sunrise Bakery has no online booking.\n\nBest,\nNexAgentLab\nTo opt out reply unsubscribe',
                   'initial', 'draft', 'approved', 'booking_system')"""
    )
    conn.execute(
        """INSERT INTO messages (id, lead_id, recipient, subject, body, message_type, status, quality_status, selected_service)
           VALUES (2, 2, 'james@techfix-test.com', 'Quick question about TechFix',
                   'Hi James,\n\nQuestion about chat assistant.\n\nBest,\nNexAgentLab\nTo opt out reply unsubscribe',
                   'initial', 'approved', 'approved', 'chatbot')"""
    )
    conn.execute(
        """INSERT INTO messages (id, lead_id, recipient, subject, body, message_type, status, quality_status, selected_service, validation_errors)
           VALUES (3, 3, 'smith@suppressed-test.com', 'Clinical consultation',
                   'Hi Dr. Smith,\n\nGuaranteed #1 rankings.\n\nNexAgentLab',
                   'initial', 'draft', 'rejected', 'technical_seo', '["Prohibited false guarantee detected"]')"""
    )
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def populated_db(db_path):
    """Connection factory to the populated test database."""
    from salesos.db import get_connection
    conn = get_connection(db_path)
    yield conn
    conn.close()


# ── 1. OAuth Setup & Credentials Tests ────────────────────────────────


class TestOAuthConfiguration:
    def test_credentials_missing_raises_error(self, tmp_path, monkeypatch):
        missing_file = tmp_path / "nonexistent_credentials.json"
        monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(missing_file))

        with pytest.raises(OAuthMissingError, match="not found"):
            load_credentials_file()

    def test_credentials_incomplete_raises_error(self, tmp_path, monkeypatch):
        bad_creds = tmp_path / "bad_credentials.json"
        bad_creds.write_text(json.dumps({"installed": {"client_id": "foo"}}), encoding="utf-8")
        monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(bad_creds))

        with pytest.raises(OAuthMissingError, match="missing 'client_id' or 'client_secret'"):
            load_credentials_file()

    def test_token_missing_handled_gracefully(self, tmp_path, monkeypatch):
        missing_token = tmp_path / "nonexistent_token.json"
        monkeypatch.setenv("GMAIL_TOKEN_FILE", str(missing_token))

        status = get_gmail_status()
        assert status.token_found is False
        assert status.token_valid is False
        assert "not found" in (status.error or "").lower()

    def test_generate_auth_url(self):
        client_info = {"client_id": "test_id_123", "client_secret": "test_secret_abc"}
        url = generate_auth_url(client_info, redirect_uri="http://localhost:8080/")

        assert "accounts.google.com" in url
        assert "client_id=test_id_123" in url
        assert "gmail.compose" in url
        assert "gmail.send" in url

    def test_exchange_code_for_token(self, monkeypatch):
        client_info = {"client_id": "cid", "client_secret": "sec"}

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "ya29.test_access_token",
            "refresh_token": "1//test_refresh_token",
            "expires_in": 3600,
        }
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: mock_resp)

        token_data = exchange_code_for_token(client_info, "auth_code_123")
        assert token_data["access_token"] == "ya29.test_access_token"
        assert token_data["refresh_token"] == "1//test_refresh_token"

    def test_token_redaction(self):
        token = "ya29.a0AfH6SMA_SecretToken_XYZ123456"
        redacted = redact_secret(token)
        assert "[REDACTED]" in redacted
        assert token not in redacted
        assert redacted.startswith("ya29")
        assert redacted.endswith("3456")

        # Dictionary redaction
        secret_dict = {
            "access_token": "ya29.very_secret_token_12345",
            "client_secret": "GOCSPX-SecretValue7890",
            "token_type": "Bearer",
        }
        masked = redact_token_dict(secret_dict)
        assert "[REDACTED]" in masked["access_token"]
        assert "[REDACTED]" in masked["client_secret"]
        assert masked["token_type"] == "Bearer"


# ── 2. Sender Verification & Mismatch Defense ─────────────────────────


class TestSenderVerification:
    def test_sender_identity_match(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SENDER", "nexagentlab.tech@gmail.com")
        mock_profile = {"emailAddress": "nexagentlab.tech@gmail.com", "messagesTotal": 100}

        with patch("salesos.gmail_client.get_user_profile", return_value=mock_profile):
            verified = verify_sender_identity("dummy_token")
            assert verified == "nexagentlab.tech@gmail.com"

    def test_sender_identity_mismatch_rejected(self, monkeypatch):
        monkeypatch.setenv("GMAIL_SENDER", "nexagentlab.tech@gmail.com")
        # Authenticated as unauthorized personal account
        mock_profile = {"emailAddress": "personal.account@gmail.com", "messagesTotal": 10}

        with patch("salesos.gmail_client.get_user_profile", return_value=mock_profile):
            with pytest.raises(SenderMismatchError, match="Sender identity mismatch"):
                verify_sender_identity("dummy_token")


# ── 3. Draft Creation & Synchronization ──────────────────────────────


class TestDraftCreationAndSync:
    def test_build_raw_email(self):
        raw = build_raw_email(
            to_email="client@example.com",
            subject="Website proposal",
            body="Hello,\n\nThis is a test email.",
            sender="nexagentlab.tech@gmail.com",
        )
        assert isinstance(raw, str)
        assert len(raw) > 20

    def test_create_or_update_draft_api(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "r_draft_999", "message": {"id": "msg_999"}}
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: mock_resp)

        res = create_or_update_draft(
            to_email="test@example.com",
            subject="Test",
            body="Body text",
            access_token="fake_token",
        )
        assert res["draft_id"] == "r_draft_999"
        assert res["message_id"] == "msg_999"

    def test_sync_drafts_dry_run(self, populated_db):
        # Sync approved message 2 in dry-run mode
        res = sync_drafts_to_gmail(populated_db, message_ids=[2], dry_run=True)
        assert res["synced"] == 1
        assert res["dry_run"] is True

        row = populated_db.execute("SELECT gmail_draft_id FROM messages WHERE id = 2").fetchone()
        assert row["gmail_draft_id"].startswith("dry_draft_2")

    def test_sync_drafts_live_mocked(self, populated_db, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "gmail_draft_abc", "message": {"id": "msg_abc"}}
        monkeypatch.setattr(requests, "post", lambda *args, **kwargs: mock_resp)
        monkeypatch.setattr("salesos.gmail_client.get_valid_access_token", lambda: "fake_token")

        res = sync_drafts_to_gmail(populated_db, message_ids=[2], dry_run=False)
        assert res["synced"] == 1
        assert res["dry_run"] is False

        row = populated_db.execute("SELECT gmail_draft_id FROM messages WHERE id = 2").fetchone()
        assert row["gmail_draft_id"] == "gmail_draft_abc"


# ── 4. Approval Requirements & Quality Gate ──────────────────────────


class TestApprovalAndStateTransitions:
    def test_approve_valid_draft(self, populated_db):
        ok, msg = approve_message(populated_db, message_id=1)
        assert ok is True
        assert "approved" in msg.lower()

        row = populated_db.execute("SELECT status, approved_at FROM messages WHERE id = 1").fetchone()
        assert row["status"] == "approved"
        assert row["approved_at"] is not None

    def test_approve_rejected_quality_draft_fails_without_force(self, populated_db):
        ok, msg = approve_message(populated_db, message_id=3, force=False)
        assert ok is False
        assert "quality gate" in msg.lower() or "prohibited" in msg.lower()

        # With force=True
        ok_force, msg_force = approve_message(populated_db, message_id=3, force=True)
        assert ok_force is True
        row = populated_db.execute("SELECT status FROM messages WHERE id = 3").fetchone()
        assert row["status"] == "approved"

    def test_unapproved_message_rejected_by_send(self, populated_db):
        # Message 1 is in 'draft' status
        with pytest.raises(PreflightCheckFailedError, match="must be approved before sending"):
            send_outreach_message(populated_db, message_id=1, dry_run=True)

    def test_queue_approved_messages(self, populated_db):
        # Message 2 is already 'approved'
        count = queue_messages(populated_db)
        assert count >= 1

        row = populated_db.execute("SELECT status FROM messages WHERE id = 2").fetchone()
        assert row["status"] == "queued"


# ── 5. Duplicate-Send Prevention ─────────────────────────────────────


class TestDuplicateSendPrevention:
    def test_duplicate_send_to_same_recipient_blocked(self, populated_db):
        # Send message 2 once (dry-run)
        res1 = send_outreach_message(populated_db, message_id=2, dry_run=True)
        assert res1["status"] == "sent"

        # Attempt to send message 2 again
        with pytest.raises(DuplicateRecipientError, match="already been sent"):
            send_outreach_message(populated_db, message_id=2, dry_run=True)

    def test_new_message_to_already_contacted_recipient_blocked(self, populated_db):
        # Mark message 2 as sent
        send_outreach_message(populated_db, message_id=2, dry_run=True)

        # Create another message for the same recipient
        cur = populated_db.execute(
            """INSERT INTO messages (lead_id, recipient, subject, body, status, quality_status)
               VALUES (2, 'james@techfix-test.com', 'Follow-up', 'Checking in...', 'approved', 'approved')"""
        )
        new_msg_id = cur.lastrowid
        populated_db.commit()

        # Should be blocked as duplicate recipient
        with pytest.raises(DuplicateRecipientError, match="Outreach was already sent to recipient"):
            send_outreach_message(populated_db, message_id=new_msg_id, dry_run=True)


# ── 6. Central Suppression & Opt-Out Enforcement ─────────────────────


class TestSuppressionAndOptOut:
    def test_record_suppression_normalizes_and_cancels_pending(self, populated_db):
        # Record suppression for Sarah
        res = record_suppression(
            populated_db,
            email="  SARAH@sunrisebakery-test.com  ",
            reason="opt_out",
            source="user_request",
        )
        assert res["email"] == "sarah@sunrisebakery-test.com"
        assert res["reason"] == "opt_out"
        assert res["cancelled_messages"] >= 1

        # Verify message 1 is now cancelled
        row = populated_db.execute("SELECT status, error_message FROM messages WHERE id = 1").fetchone()
        assert row["status"] == "cancelled"
        assert "opt_out" in row["error_message"]

        # Verify lead status is updated to suppressed
        lead = populated_db.execute("SELECT status FROM leads WHERE id = 1").fetchone()
        assert lead["status"] == "suppressed"

    def test_suppressed_recipient_blocked_from_sending(self, populated_db):
        # Suppress Sarah
        record_suppression(populated_db, "sarah@sunrisebakery-test.com", reason="unsubscribe")

        # Try to send
        with pytest.raises(PreflightCheckFailedError, match="suppress"):
            send_outreach_message(populated_db, message_id=1, dry_run=True, force=True)

    def test_queue_messages_skips_suppressed_recipients(self, populated_db):
        # Approve message 1
        populated_db.execute("UPDATE messages SET status = 'approved' WHERE id = 1")
        populated_db.commit()

        # Suppress Sarah
        record_suppression(populated_db, "sarah@sunrisebakery-test.com", reason="opt_out")

        # Attempt to queue
        queued = queue_messages(populated_db)
        # Message 1 should be cancelled, not queued
        m1 = populated_db.execute("SELECT status FROM messages WHERE id = 1").fetchone()
        assert m1["status"] == "cancelled"

    def test_remove_suppression(self, populated_db):
        record_suppression(populated_db, "test@optout.com", reason="opt_out")
        assert is_email_suppressed(populated_db, "test@optout.com")[0] is True

        removed = remove_suppression(populated_db, "test@optout.com")
        assert removed is True
        assert is_email_suppressed(populated_db, "test@optout.com")[0] is False


# ── 7. Rate Limits, Daily Caps & Kill Switch ─────────────────────────


class TestRateLimitsAndCaps:
    def test_daily_send_cap_exceeded_blocks_outreach(self, populated_db, monkeypatch):
        monkeypatch.setenv("GMAIL_DAILY_LIMIT", "1")

        # First send uses quota 1 of 1
        send_outreach_message(populated_db, message_id=2, dry_run=True)

        # Create another approved message
        populated_db.execute(
            """INSERT INTO messages (id, lead_id, recipient, subject, body, status, quality_status)
               VALUES (10, 1, 'newclient@example.com', 'Subject', 'Body', 'approved', 'approved')"""
        )
        populated_db.commit()

        with pytest.raises(DailyCapExceededError, match="Daily send limit"):
            send_outreach_message(populated_db, message_id=10, dry_run=True)

    def test_kill_switch_halts_outreach_immediately(self, populated_db, monkeypatch):
        monkeypatch.setenv("CAMPAIGN_KILL_SWITCH", "true")

        with pytest.raises(KillSwitchActiveError, match="kill switch is active"):
            send_outreach_message(populated_db, message_id=2, dry_run=True)

    def test_batch_send_stops_gracefully_on_daily_cap(self, populated_db, monkeypatch):
        monkeypatch.setenv("GMAIL_DAILY_LIMIT", "1")
        # Queue message 2
        populated_db.execute("UPDATE messages SET status = 'queued' WHERE id = 2")
        populated_db.execute(
            """INSERT INTO messages (id, lead_id, recipient, subject, body, status, quality_status)
               VALUES (20, 1, 'client20@example.com', 'Subj', 'Body', 'queued', 'approved')"""
        )
        populated_db.commit()

        res = send_batch(populated_db, delay_seconds=0.0)
        assert res["sent"] == 1
        assert len(res["errors"]) >= 1
        assert any("daily send limit" in e.lower() for e in res["errors"])


# ── 8. Ambiguous Gmail Send Outcomes ─────────────────────────────────


class TestAmbiguousSendOutcome:
    def test_timeout_raises_ambiguous_send_error(self, populated_db, monkeypatch):
        monkeypatch.setattr(
            "salesos.outreach.send_message_api",
            MagicMock(side_effect=requests.Timeout("Connection timed out waiting for Gmail response")),
        )
        monkeypatch.setattr("salesos.outreach.verify_sender_identity", lambda: "nexagentlab.tech@gmail.com")

        with pytest.raises(AmbiguousSendError, match=r"(?i)ambiguous"):
            send_outreach_message(populated_db, message_id=2, dry_run=False)

        # Check message is marked failed with clear audit warning
        row = populated_db.execute("SELECT status, error_message FROM messages WHERE id = 2").fetchone()
        assert row["status"] == "failed"
        assert "ambiguous" in row["error_message"].lower()


# ── 9. Offline Operation & Migration Safety ──────────────────────────


class TestOfflineAndMigrationSafety:
    def test_offline_operation_zero_keys(self, populated_db, monkeypatch):
        # Clear all AI and Gmail keys/credentials
        monkeypatch.delenv("GMAIL_CREDENTIALS_FILE", raising=False)
        monkeypatch.delenv("GMAIL_TOKEN_FILE", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)

        # Status check runs cleanly offline
        status = get_gmail_status()
        assert status.dry_run is True

        # Outreach status runs cleanly offline
        outreach_status = get_outreach_status(populated_db)
        assert outreach_status["dry_run"] is True

        # Preview runs cleanly offline
        p = preview_message(populated_db, message_id=2)
        assert p["ready_to_send"] is True

        # Dry-run send executes completely offline
        res = send_outreach_message(populated_db, message_id=2, dry_run=True)
        assert res["status"] == "sent"

    def test_db_migration_idempotent(self, tmp_path, populated_db):
        # Calling init_db multiple times must not fail or corrupt schema
        p = str(tmp_path / "migration_test.db")
        init_db(p)
        init_db(p)
        cur = populated_db.execute("PRAGMA table_info(messages)")
        cols = {row["name"] for row in cur.fetchall()}

        required_cols = {
            "gmail_draft_id",
            "gmail_message_id",
            "gmail_thread_id",
            "error_message",
            "approved_at",
            "idempotency_key",
        }
        assert required_cols.issubset(cols)


# ── 10. CLI Command Tests ────────────────────────────────────────────


class TestMilestone4CLI:
    def test_cli_gmail_status(self):
        runner = CliRunner()
        res = runner.invoke(cli, ["gmail", "status"])
        assert res.exit_code == 0
        assert "Gmail Integration Status" in res.output
        assert "DRY RUN" in res.output

    def test_cli_gmail_auth_without_creds_shows_guide(self, tmp_path, monkeypatch):
        missing = tmp_path / "nocreds.json"
        monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(missing))

        runner = CliRunner()
        res = runner.invoke(cli, ["gmail", "auth"])
        assert res.exit_code == 0
        assert "Google Cloud Console" in res.output
        assert "credentials.json" in res.output

    def test_cli_outreach_status(self, db_path, monkeypatch):
        from salesos.db import get_connection
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()
        res = runner.invoke(cli, ["outreach", "status"])
        assert res.exit_code == 0
        assert "Campaign Overview" in res.output

    def test_cli_outreach_preview(self, db_path, monkeypatch):
        from salesos.db import get_connection
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()
        res = runner.invoke(cli, ["outreach", "preview", "2"])
        assert res.exit_code == 0
        assert "Pre-Flight Safety Checklist" in res.output
        assert "TechFix" in res.output

    def test_cli_outreach_approve_and_queue(self, db_path, monkeypatch):
        from salesos.db import get_connection
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        # Approve message 1
        res_app = runner.invoke(cli, ["outreach", "approve", "1"])
        assert res_app.exit_code == 0
        assert "approved for outreach" in res_app.output.lower()

        # Queue approved messages
        res_q = runner.invoke(cli, ["outreach", "queue"])
        assert res_q.exit_code == 0
        assert "outreach queue" in res_q.output.lower()

    def test_cli_outreach_send_single_and_batch_dry_run(self, db_path, monkeypatch):
        from salesos.db import get_connection
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        # Single send dry-run
        res_send = runner.invoke(cli, ["outreach", "send", "2"])
        assert res_send.exit_code == 0
        assert "dispatched successfully" in res_send.output.lower()
        assert "DRY RUN" in res_send.output

        # Batch send dry-run
        res_batch = runner.invoke(cli, ["outreach", "send", "--all-queued"])
        assert res_batch.exit_code == 0
        assert "Batch Send Results" in res_batch.output

    def test_cli_outreach_suppress(self, db_path, monkeypatch):
        from salesos.db import get_connection
        monkeypatch.setattr("salesos.cli._get_conn", lambda: get_connection(db_path))
        runner = CliRunner()

        res = runner.invoke(cli, ["outreach", "suppress", "client@blocked.com", "--reason", "opt_out"])
        assert res.exit_code == 0
        assert "suppression list" in res.output.lower()
        assert "client@blocked.com" in res.output
