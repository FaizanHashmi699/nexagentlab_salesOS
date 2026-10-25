"""Lead import engine with validation, deduplication, and tracking.

Supports CSV, XLSX, and JSON files.  Every import is recorded in the
``imports`` table with row counts, error details, and a SHA-256 file hash
for traceability.  Deduplication checks the current batch, the existing
database, and the suppression list before inserting.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from pydantic import ValidationError

from .db import audit_log, get_connection, init_db, transaction
from .models import (
    REQUIRED_FIELDS,
    LeadRecord,
    apply_column_mapping,
    auto_map_columns,
)


# ── Import result container ─────────────────────────────────────────

class ImportResult:
    """Aggregated results of one import operation."""

    def __init__(self) -> None:
        self.total_rows: int = 0
        self.imported: int = 0
        self.duplicates: int = 0
        self.errors: int = 0
        self.skipped: int = 0
        self.error_details: list[dict] = []
        self.duplicate_details: list[dict] = []
        self.imported_leads: list[dict] = []
        self.import_id: int | None = None

    @property
    def summary(self) -> dict:
        return {
            "total_rows": self.total_rows,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "skipped": self.skipped,
            "import_id": self.import_id,
        }


# ── Helpers ──────────────────────────────────────────────────────────

def _file_hash(filepath: str) -> str:
    """SHA-256 hash of a file for import deduplication."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ── File readers ─────────────────────────────────────────────────────

def read_file(filepath: str) -> tuple[list[dict], str]:
    """Read a CSV, XLSX, or JSON file into a list of row dicts.

    Returns ``(rows, format_type)`` where *format_type* is one of
    ``'csv'``, ``'xlsx'``, or ``'json'``.

    Raises :class:`ValueError` for unsupported formats.
    """
    suffix = Path(filepath).suffix.lower()

    if suffix == ".csv":
        df = pd.read_csv(filepath, dtype=str, keep_default_na=False)
        return df.to_dict("records"), "csv"

    if suffix in (".xlsx", ".xls"):
        df = pd.read_excel(filepath, dtype=str, keep_default_na=False)
        return df.to_dict("records"), "xlsx"

    if suffix == ".json":
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data, "json"
        if isinstance(data, dict) and "leads" in data:
            return data["leads"], "json"
        raise ValueError('JSON must be a list of objects or {"leads": [...]}')

    raise ValueError(f"Unsupported file format: {suffix}")


# ── Validation & mapping ────────────────────────────────────────────

def validate_and_map(
    rows: list[dict],
    column_mapping: dict[str, str] | None = None,
) -> tuple[list[LeadRecord], list[dict]]:
    """Validate rows through Pydantic and return (valid, errors)."""
    if not rows:
        return [], []

    if column_mapping is None:
        headers = list(rows[0].keys())
        column_mapping = auto_map_columns(headers)

    valid: list[LeadRecord] = []
    errors: list[dict] = []

    for i, row in enumerate(rows, start=1):
        mapped = apply_column_mapping(row, column_mapping)
        try:
            lead = LeadRecord(**mapped)
            valid.append(lead)
        except ValidationError as e:
            errors.append({
                "row": i,
                "data": {k: v for k, v in mapped.items() if v},
                "errors": [err["msg"] for err in e.errors()],
            })
        except Exception as e:
            errors.append({
                "row": i,
                "data": {k: str(v) for k, v in row.items() if v},
                "errors": [str(e)],
            })

    return valid, errors


# ── Deduplication ────────────────────────────────────────────────────

def check_duplicates(
    leads: list[LeadRecord],
    conn: sqlite3.Connection,
) -> tuple[list[LeadRecord], list[dict]]:
    """Filter duplicates against the batch, existing DB rows, and suppression.

    Returns ``(new_leads, duplicate_info)``.
    """
    new: list[LeadRecord] = []
    duplicates: list[dict] = []
    seen_emails: set[str] = set()

    for lead in leads:
        email = lead.email.lower()

        # 1. Within-batch duplicate
        if email in seen_emails:
            duplicates.append({
                "email": lead.email,
                "business_name": lead.business_name,
                "reason": "duplicate_in_batch",
            })
            continue

        # 2. Existing database record
        existing = conn.execute(
            "SELECT id, business_name, email FROM leads WHERE email = ?",
            (email,),
        ).fetchone()
        if existing:
            duplicates.append({
                "email": lead.email,
                "business_name": lead.business_name,
                "reason": "exists_in_database",
                "existing_id": existing["id"],
                "existing_business": existing["business_name"],
            })
            continue

        # 3. Suppression list
        suppressed = conn.execute(
            "SELECT id, reason FROM suppression WHERE email = ?",
            (email,),
        ).fetchone()
        if suppressed:
            duplicates.append({
                "email": lead.email,
                "business_name": lead.business_name,
                "reason": f"suppressed ({suppressed['reason']})",
            })
            continue

        seen_emails.add(email)
        new.append(lead)

    return new, duplicates


# ── Preview (read-only) ─────────────────────────────────────────────

def preview_import(
    filepath: str,
    column_mapping: dict[str, str] | None = None,
) -> dict:
    """Preview an import without touching the database.

    Returns a summary dict with sample data, validation results, and
    detected column mapping.
    """
    rows, fmt = read_file(filepath)
    valid, errors = validate_and_map(rows, column_mapping)

    seen: set[str] = set()
    batch_dupes = 0
    unique_valid: list[LeadRecord] = []
    for lead in valid:
        if lead.email in seen:
            batch_dupes += 1
        else:
            seen.add(lead.email)
            unique_valid.append(lead)

    return {
        "file": filepath,
        "format": fmt,
        "total_rows": len(rows),
        "valid_rows": len(valid),
        "unique_valid": len(unique_valid),
        "batch_duplicates": batch_dupes,
        "error_rows": len(errors),
        "errors": errors[:10],
        "sample_leads": [lead.model_dump() for lead in unique_valid[:5]],
        "detected_mapping": auto_map_columns(list(rows[0].keys())) if rows else {},
    }


# ── Full import ──────────────────────────────────────────────────────

def import_leads(
    filepath: str,
    conn: sqlite3.Connection,
    column_mapping: dict[str, str] | None = None,
    dry_run: bool = False,
    source_override: str | None = None,
) -> ImportResult:
    """Import leads from a file into the database.

    Parameters
    ----------
    filepath:
        Path to a CSV, XLSX, or JSON file.
    conn:
        Open SQLite connection (with schema already initialized).
    column_mapping:
        Explicit mapping of ``{standard_field: original_header}``.
        If *None*, columns are auto-detected from headers.
    dry_run:
        Validate and check duplicates but do **not** write to the DB.
    source_override:
        If set, overrides the ``source`` field for every imported lead.

    Returns
    -------
    ImportResult
        Counts, error details, and the ``import_id`` (if not dry-run).
    """
    result = ImportResult()

    # Read
    rows, fmt = read_file(filepath)
    result.total_rows = len(rows)
    if not rows:
        return result

    # Validate
    valid_leads, validation_errors = validate_and_map(rows, column_mapping)
    result.errors = len(validation_errors)
    result.error_details = validation_errors

    # Dry-run: check duplicates but don't write
    if dry_run:
        new_leads, dupe_info = check_duplicates(valid_leads, conn)
        result.duplicates = len(dupe_info)
        result.duplicate_details = dupe_info
        result.imported = len(new_leads)
        result.skipped = result.errors + result.duplicates
        return result

    # Determine column mapping for the import record
    if column_mapping is None and rows:
        resolved_mapping = auto_map_columns(list(rows[0].keys()))
    else:
        resolved_mapping = column_mapping or {}

    fhash = _file_hash(filepath)
    mapping_json = json.dumps(resolved_mapping)

    with transaction(conn):
        # Create import record
        cursor = conn.execute(
            """INSERT INTO imports
               (filename, file_hash, format, total_rows, status, column_mapping)
               VALUES (?, ?, ?, ?, 'in_progress', ?)""",
            (os.path.basename(filepath), fhash, fmt, len(rows), mapping_json),
        )
        import_id = cursor.lastrowid
        result.import_id = import_id

        # Deduplicate
        new_leads, dupe_info = check_duplicates(valid_leads, conn)
        result.duplicates = len(dupe_info)
        result.duplicate_details = dupe_info

        # Insert
        for lead in new_leads:
            data = lead.model_dump()
            if source_override:
                data["source"] = source_override

            conn.execute(
                """INSERT INTO leads
                   (business_name, email, website, contact_name, location,
                    phone, source, external_id, tags, notes, status, import_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?)""",
                (
                    data["business_name"],
                    data["email"],
                    data.get("website"),
                    data.get("contact_name"),
                    data.get("location"),
                    data.get("phone"),
                    data.get("source"),
                    data.get("external_id"),
                    data.get("tags"),
                    data.get("notes"),
                    import_id,
                ),
            )
            result.imported += 1
            result.imported_leads.append(data)

        result.skipped = result.errors + result.duplicates

        # Finalize import record
        status = "completed" if result.errors == 0 else "partial"
        conn.execute(
            """UPDATE imports SET
               imported_count = ?, skipped_count = ?, error_count = ?,
               duplicate_count = ?, status = ?, errors = ?,
               completed_at = datetime('now')
               WHERE id = ?""",
            (
                result.imported,
                result.skipped,
                result.errors,
                result.duplicates,
                status,
                json.dumps(result.error_details),
                import_id,
            ),
        )

        audit_log(
            conn, "import", import_id, "import_completed",
            json.dumps(result.summary),
        )

    return result
