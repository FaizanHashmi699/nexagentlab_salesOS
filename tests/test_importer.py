"""Tests for the SalesOS lead import system.

Covers file reading, validation, column mapping, deduplication,
import tracking, preview, audit logging, schema integrity, and
edge cases (malformed rows, empty files, suppression).

All tests use synthetic data and temporary databases — no real
leads, emails, or credentials are involved.
"""

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from salesos.db import init_db
from salesos.importer import (
    ImportResult,
    check_duplicates,
    import_leads,
    preview_import,
    read_file,
    validate_and_map,
)
from salesos.models import LeadRecord, apply_column_mapping, auto_map_columns


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    """Fresh test database."""
    db_path = str(tmp_path / "test.db")
    conn = init_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def sample_csv(tmp_path):
    """Standard 3-row CSV."""
    fp = tmp_path / "leads.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["business_name", "contact_name", "email", "website",
                     "phone", "location", "source"])
        w.writerow(["Acme Corp", "John Doe", "john@acme-test.com",
                     "acme-test.com", "555-1234", "New York, NY", "linkedin"])
        w.writerow(["Beta LLC", "Jane Smith", "jane@beta-test.com",
                     "beta-test.com", "555-5678", "Chicago, IL", "referral"])
        w.writerow(["Gamma Inc", "Bob Wilson", "bob@gamma-test.com",
                     "gamma-test.com", "555-9012", "Denver, CO", "cold_list"])
    return str(fp)


@pytest.fixture
def sample_csv_with_dupes(tmp_path):
    """CSV with an in-batch duplicate email."""
    fp = tmp_path / "dupes.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["business_name", "email", "source"])
        w.writerow(["Acme Corp", "john@acme-test.com", "linkedin"])
        w.writerow(["Acme Corp Copy", "john@acme-test.com", "cold_list"])
        w.writerow(["Beta LLC", "jane@beta-test.com", "referral"])
    return str(fp)


@pytest.fixture
def malformed_csv(tmp_path):
    """CSV with 3 invalid rows and 2 valid rows."""
    fp = tmp_path / "malformed.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["business_name", "email", "website"])
        w.writerow(["Valid Biz", "valid@test.com", "test.com"])        # valid
        w.writerow(["", "no-biz@test.com", ""])                        # no name
        w.writerow(["No Email", "", "test2.com"])                      # no email
        w.writerow(["Bad Email", "not-an-email", "test3.com"])         # bad email
        w.writerow(["Good Biz", "good@test.com", "good.com"])         # valid
    return str(fp)


@pytest.fixture
def sample_json(tmp_path):
    """JSON list of 2 leads."""
    fp = tmp_path / "leads.json"
    data = [
        {"business_name": "JSON Corp", "email": "json@corp-test.com",
         "website": "jsoncorp-test.com"},
        {"business_name": "Data LLC", "email": "data@llc-test.com",
         "location": "Seattle, WA"},
    ]
    with open(fp, "w") as f:
        json.dump(data, f)
    return str(fp)


@pytest.fixture
def sample_json_wrapped(tmp_path):
    """JSON wrapped in {"leads": [...]}."""
    fp = tmp_path / "wrapped.json"
    data = {"leads": [
        {"business_name": "Wrapped Co", "email": "wrapped@test.com"},
    ]}
    with open(fp, "w") as f:
        json.dump(data, f)
    return str(fp)


@pytest.fixture
def sample_xlsx(tmp_path):
    """XLSX with 2 leads."""
    openpyxl = pytest.importorskip("openpyxl")
    fp = tmp_path / "leads.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["business_name", "email", "contact_name", "location"])
    ws.append(["Excel Corp", "excel@corp-test.com", "Alice", "Boston, MA"])
    ws.append(["Sheet LLC", "sheet@llc-test.com", "Bob", "Dallas, TX"])
    wb.save(fp)
    return str(fp)


@pytest.fixture
def empty_csv(tmp_path):
    """CSV with headers only."""
    fp = tmp_path / "empty.csv"
    with open(fp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["business_name", "email"])
    return str(fp)


# ── File reading ─────────────────────────────────────────────────────

class TestReadFile:
    def test_read_csv(self, sample_csv):
        rows, fmt = read_file(sample_csv)
        assert fmt == "csv"
        assert len(rows) == 3
        assert rows[0]["business_name"] == "Acme Corp"

    def test_read_json(self, sample_json):
        rows, fmt = read_file(sample_json)
        assert fmt == "json"
        assert len(rows) == 2

    def test_read_json_wrapped(self, sample_json_wrapped):
        rows, fmt = read_file(sample_json_wrapped)
        assert fmt == "json"
        assert len(rows) == 1
        assert rows[0]["business_name"] == "Wrapped Co"

    def test_read_xlsx(self, sample_xlsx):
        rows, fmt = read_file(sample_xlsx)
        assert fmt == "xlsx"
        assert len(rows) == 2

    def test_read_empty_csv(self, empty_csv):
        rows, fmt = read_file(empty_csv)
        assert fmt == "csv"
        assert len(rows) == 0

    def test_unsupported_format(self, tmp_path):
        fp = tmp_path / "data.xml"
        fp.write_text("<data/>")
        with pytest.raises(ValueError, match="Unsupported"):
            read_file(str(fp))


# ── Validation ───────────────────────────────────────────────────────

class TestValidation:
    def test_valid_leads(self, sample_csv):
        rows, _ = read_file(sample_csv)
        valid, errors = validate_and_map(rows)
        assert len(valid) == 3
        assert len(errors) == 0

    def test_malformed_rows(self, malformed_csv):
        rows, _ = read_file(malformed_csv)
        valid, errors = validate_and_map(rows)
        assert len(valid) == 2   # "Valid Biz" and "Good Biz"
        assert len(errors) == 3  # missing name, missing email, bad email

    def test_email_normalization(self):
        lead = LeadRecord(business_name="Test", email="  USER@Test.COM  ")
        assert lead.email == "user@test.com"

    def test_website_normalization_adds_https(self):
        lead = LeadRecord(
            business_name="Test", email="t@t.com", website="example.com",
        )
        assert lead.website == "https://example.com"

    def test_website_preserves_existing_scheme(self):
        lead = LeadRecord(
            business_name="Test", email="t@t.com",
            website="http://example.com",
        )
        assert lead.website == "http://example.com"

    def test_website_none_when_empty(self):
        lead = LeadRecord(
            business_name="Test", email="t@t.com", website="",
        )
        assert lead.website is None

    def test_invalid_email_rejected(self):
        with pytest.raises(Exception):
            LeadRecord(business_name="Test", email="not-valid")

    def test_empty_email_rejected(self):
        with pytest.raises(Exception):
            LeadRecord(business_name="Test", email="")

    def test_empty_business_name_rejected(self):
        with pytest.raises(Exception):
            LeadRecord(business_name="", email="test@test.com")

    def test_empty_rows_returns_empty(self):
        valid, errors = validate_and_map([])
        assert valid == []
        assert errors == []


# ── Column mapping ───────────────────────────────────────────────────

class TestColumnMapping:
    def test_standard_headers(self):
        headers = ["business_name", "email", "website", "contact_name"]
        mapping = auto_map_columns(headers)
        assert mapping["business_name"] == "business_name"
        assert mapping["email"] == "email"

    def test_alias_headers(self):
        headers = ["company", "e-mail", "url", "full_name"]
        mapping = auto_map_columns(headers)
        assert mapping["business_name"] == "company"
        assert mapping["email"] == "e-mail"
        assert mapping["website"] == "url"
        assert mapping["contact_name"] == "full_name"

    def test_case_insensitive(self):
        headers = ["Business_Name", "EMAIL", "Website"]
        mapping = auto_map_columns(headers)
        assert "business_name" in mapping
        assert "email" in mapping

    def test_apply_mapping(self):
        mapping = {"business_name": "company", "email": "mail"}
        row = {"company": "Acme", "mail": "acme@test.com", "extra": "ignored"}
        result = apply_column_mapping(row, mapping)
        assert result["business_name"] == "Acme"
        assert result["email"] == "acme@test.com"
        assert "extra" not in result


# ── Deduplication ────────────────────────────────────────────────────

class TestDeduplication:
    def test_in_batch_dedup(self, db, sample_csv_with_dupes):
        rows, _ = read_file(sample_csv_with_dupes)
        valid, _ = validate_and_map(rows)
        new, dupes = check_duplicates(valid, db)
        assert len(new) == 2    # john@ (first) and jane@
        assert len(dupes) == 1  # second john@
        assert dupes[0]["reason"] == "duplicate_in_batch"

    def test_db_dedup(self, db, sample_csv):
        result1 = import_leads(sample_csv, db)
        assert result1.imported == 3

        result2 = import_leads(sample_csv, db)
        assert result2.imported == 0
        assert result2.duplicates == 3

    def test_suppression_blocks_import(self, db, sample_csv):
        db.execute(
            "INSERT INTO suppression (email, reason, source) VALUES (?, ?, ?)",
            ("john@acme-test.com", "opt_out", "manual"),
        )
        db.commit()

        result = import_leads(sample_csv, db)
        assert result.imported == 2   # jane + bob
        assert result.duplicates == 1  # john suppressed

    def test_case_insensitive_dedup(self, db, tmp_path):
        """Email dedup should be case-insensitive."""
        fp = tmp_path / "case.csv"
        with open(fp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["business_name", "email"])
            w.writerow(["A Corp", "Test@Example.COM"])
            w.writerow(["B Corp", "test@example.com"])
        result = import_leads(str(fp), db)
        assert result.imported == 1
        assert result.duplicates == 1


# ── Import operations ────────────────────────────────────────────────

class TestImport:
    def test_csv_import(self, db, sample_csv):
        result = import_leads(sample_csv, db)
        assert result.total_rows == 3
        assert result.imported == 3
        assert result.errors == 0
        assert result.duplicates == 0
        assert result.import_id is not None

        count = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        assert count == 3

    def test_json_import(self, db, sample_json):
        result = import_leads(sample_json, db)
        assert result.imported == 2
        assert result.errors == 0

    def test_xlsx_import(self, db, sample_xlsx):
        result = import_leads(sample_xlsx, db)
        assert result.imported == 2
        assert result.errors == 0

    def test_dry_run_no_db_writes(self, db, sample_csv):
        result = import_leads(sample_csv, db, dry_run=True)
        assert result.imported == 3

        count = db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        assert count == 0

    def test_import_with_errors(self, db, malformed_csv):
        result = import_leads(malformed_csv, db)
        assert result.imported == 2
        assert result.errors == 3
        assert len(result.error_details) == 3

    def test_import_tracking_record(self, db, sample_csv):
        result = import_leads(sample_csv, db)
        imp = db.execute(
            "SELECT * FROM imports WHERE id = ?", (result.import_id,),
        ).fetchone()
        assert imp["status"] == "completed"
        assert imp["imported_count"] == 3
        assert imp["error_count"] == 0
        assert imp["completed_at"] is not None

    def test_partial_status_on_errors(self, db, malformed_csv):
        result = import_leads(malformed_csv, db)
        imp = db.execute(
            "SELECT status FROM imports WHERE id = ?", (result.import_id,),
        ).fetchone()
        assert imp["status"] == "partial"

    def test_source_override(self, db, sample_csv):
        import_leads(sample_csv, db, source_override="test_source")
        leads = db.execute("SELECT source FROM leads").fetchall()
        for lead in leads:
            assert lead["source"] == "test_source"

    def test_no_overwrite_on_reimport(self, db, sample_csv):
        """Re-import must not overwrite existing records."""
        import_leads(sample_csv, db)

        db.execute(
            "UPDATE leads SET notes = 'custom' WHERE email = 'john@acme-test.com'",
        )
        db.commit()

        result2 = import_leads(sample_csv, db)
        assert result2.imported == 0
        assert result2.duplicates == 3

        lead = db.execute(
            "SELECT notes FROM leads WHERE email = 'john@acme-test.com'",
        ).fetchone()
        assert lead["notes"] == "custom"

    def test_empty_file_import(self, db, empty_csv):
        result = import_leads(empty_csv, db)
        assert result.total_rows == 0
        assert result.imported == 0


# ── Preview ──────────────────────────────────────────────────────────

class TestPreview:
    def test_preview_valid_file(self, sample_csv):
        info = preview_import(sample_csv)
        assert info["total_rows"] == 3
        assert info["valid_rows"] == 3
        assert info["error_rows"] == 0
        assert len(info["sample_leads"]) > 0
        assert info["format"] == "csv"

    def test_preview_with_errors(self, malformed_csv):
        info = preview_import(malformed_csv)
        assert info["valid_rows"] == 2
        assert info["error_rows"] == 3

    def test_preview_detects_batch_dupes(self, sample_csv_with_dupes):
        info = preview_import(sample_csv_with_dupes)
        assert info["batch_duplicates"] == 1


# ── Audit log ────────────────────────────────────────────────────────

class TestAuditLog:
    def test_import_creates_audit_entry(self, db, sample_csv):
        import_leads(sample_csv, db)
        entries = db.execute(
            "SELECT * FROM audit_log WHERE entity_type = 'import'",
        ).fetchall()
        assert len(entries) == 1
        assert entries[0]["action"] == "import_completed"

    def test_audit_contains_summary(self, db, sample_csv):
        import_leads(sample_csv, db)
        entry = db.execute(
            "SELECT details FROM audit_log WHERE entity_type = 'import'",
        ).fetchone()
        details = json.loads(entry["details"])
        assert details["imported"] == 3
        assert "total_rows" in details


# ── Schema integrity ────────────────────────────────────────────────

class TestSchema:
    def test_all_tables_exist(self, db):
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
        ).fetchall()
        table_names = {t["name"] for t in tables}
        expected = {
            "imports", "leads", "research", "opportunities",
            "campaigns", "messages", "replies", "suppression", "audit_log",
        }
        assert expected.issubset(table_names)

    def test_unique_email_constraint(self, db):
        db.execute(
            "INSERT INTO leads (business_name, email, status) "
            "VALUES (?, ?, 'imported')",
            ("Biz A", "test@test.com"),
        )
        db.commit()

        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO leads (business_name, email, status) "
                "VALUES (?, ?, 'imported')",
                ("Biz B", "test@test.com"),
            )

    def test_foreign_key_enforcement(self, db):
        """Inserting a lead with a nonexistent import_id should fail."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO leads (business_name, email, status, import_id) "
                "VALUES (?, ?, 'imported', ?)",
                ("FK Test", "fk@test.com", 99999),
            )
            db.commit()

    def test_idempotent_init(self, tmp_path):
        """Calling init_db twice should not error."""
        db_path = str(tmp_path / "idempotent.db")
        conn1 = init_db(db_path)
        conn1.close()
        conn2 = init_db(db_path)
        tables = conn2.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'",
        ).fetchone()[0]
        assert tables >= 9
        conn2.close()
