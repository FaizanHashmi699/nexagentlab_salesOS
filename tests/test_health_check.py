"""Tests for System Health Check, Integration Audit & CLI Commands."""

import sqlite3
import pytest
from click.testing import CliRunner

from salesos.cli import cli
from salesos.db import SCHEMA, _migrate_db
from salesos.health_check import (
    CheckStatus,
    ReadinessLevel,
    check_ai_providers,
    check_calendly,
    check_database,
    check_gmail,
    check_pipeline_dag,
    check_prospect_discovery,
    check_website_research,
    run_all_health_checks,
)


@pytest.fixture
def health_conn(tmp_path):
    """Temporary SQLite database connection."""
    db_file = str(tmp_path / "test_health.db")
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate_db(conn)
    conn.commit()
    yield conn
    conn.close()


def test_check_database_pass(health_conn):
    res = check_database(conn=health_conn)
    assert res.status == CheckStatus.PASS
    assert res.component == "database"
    assert "healthy" in res.summary


def test_check_website_research_safe():
    res = check_website_research(live=False)
    assert res.status == CheckStatus.PASS
    assert res.details.get("ssrf_protection_verified") is True


def test_check_prospect_discovery_not_implemented():
    res = check_prospect_discovery()
    assert res.status == CheckStatus.NOT_IMPLEMENTED
    assert "not implemented" in res.summary.lower()


def test_check_gmail_offline_status():
    res = check_gmail(live=False)
    assert res.status in (CheckStatus.PASS, CheckStatus.NOT_CONFIGURED)
    assert res.component == "gmail_oauth"


def test_check_ai_providers_status():
    res = check_ai_providers()
    assert res.status in (CheckStatus.PASS, CheckStatus.NOT_CONFIGURED)
    assert res.component == "ai_providers"


def test_check_calendly_status():
    res = check_calendly()
    assert res.status == CheckStatus.PASS
    assert res.details.get("webhook_hmac_sha256_verified") is True


def test_check_pipeline_dag():
    res = check_pipeline_dag()
    assert res.status == CheckStatus.PASS
    assert res.details.get("dag_valid") is True
    assert res.details.get("stages_count") == 10


def test_run_all_health_checks(health_conn):
    report = run_all_health_checks(conn=health_conn, live=False)
    assert report.readiness in (
        ReadinessLevel.READY_FOR_LOCAL_TESTING_ONLY,
        ReadinessLevel.READY_FOR_CONTROLLED_PILOT,
    )
    data = report.to_dict()
    assert "timestamp" in data
    assert "readiness" in data
    assert len(data["checks"]) == 7

    md_text = report.to_markdown()
    assert "# NexAgentLab SalesOS — Integration Health & Acceptance Report" in md_text


def test_cli_integrations_status():
    runner = CliRunner()
    result = runner.invoke(cli, ["integrations", "status"])
    assert result.exit_code == 0
    assert "Integration Status" in result.output
    assert "System Readiness Level" in result.output


def test_cli_integrations_check(tmp_path):
    runner = CliRunner()
    json_out = str(tmp_path / "report.json")
    md_out = str(tmp_path / "report.md")

    result = runner.invoke(cli, ["integrations", "check", "--json", json_out, "--md", md_out])
    assert result.exit_code == 0
    assert "Integration Health Check Results" in result.output
    assert "Readiness Classification" in result.output
