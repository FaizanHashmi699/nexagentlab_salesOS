"""Database initialization and connection management for SalesOS.

All persistent data is stored in a single SQLite database. The schema
covers the full sales pipeline: imports, leads, research, opportunities,
campaigns, messages, replies, suppression, and audit logging.
"""

import sqlite3
import os
from pathlib import Path
from contextlib import contextmanager

DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
)

SCHEMA = """
-- ============================================================
-- Import tracking
-- ============================================================
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    file_hash TEXT,
    format TEXT NOT NULL CHECK (format IN ('csv', 'xlsx', 'json')),
    total_rows INTEGER DEFAULT 0,
    imported_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    duplicate_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'partial')),
    column_mapping TEXT,   -- JSON dict
    errors TEXT,           -- JSON array of error details
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- ============================================================
-- Lead records
-- ============================================================
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,
    business_name TEXT NOT NULL,
    website TEXT,
    location TEXT,
    contact_name TEXT,
    email TEXT NOT NULL,
    phone TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'imported',
    score REAL,
    tags TEXT,              -- JSON array
    notes TEXT,
    import_id INTEGER REFERENCES imports(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_business ON leads(business_name);

-- ============================================================
-- Research findings (populated by future Research Agent)
-- ============================================================
CREATE TABLE IF NOT EXISTS research (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    agent_type TEXT NOT NULL,
    findings TEXT,          -- JSON
    confidence REAL,
    status TEXT DEFAULT 'pending',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_research_lead ON research(lead_id);

-- ============================================================
-- Opportunities (populated by future Opportunity Agent)
-- ============================================================
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    category TEXT NOT NULL,
    description TEXT,
    confidence REAL,
    evidence TEXT,           -- JSON
    priority INTEGER DEFAULT 0,
    status TEXT DEFAULT 'identified',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_opportunities_lead ON opportunities(lead_id);

-- ============================================================
-- Campaigns
-- ============================================================
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'draft'
        CHECK (status IN ('draft', 'active', 'paused', 'completed', 'cancelled')),
    config TEXT,             -- JSON
    daily_limit INTEGER DEFAULT 20,
    total_sent INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Messages (outbound)
-- ============================================================
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    campaign_id INTEGER REFERENCES campaigns(id),
    recipient TEXT,
    subject TEXT,
    body TEXT,
    message_type TEXT DEFAULT 'initial'
        CHECK (message_type IN ('initial', 'follow_up', 'reply')),
    status TEXT DEFAULT 'draft'
        CHECK (status IN ('draft', 'queued', 'approved', 'sent', 'failed', 'bounced', 'cancelled')),
    quality_status TEXT DEFAULT 'pending'
        CHECK (quality_status IN ('pending', 'approved', 'rejected', 'manual_review')),
    selected_service TEXT,
    qualification_score REAL,
    evidence_references TEXT,
    provider TEXT,
    model TEXT,
    validation_errors TEXT,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    scheduled_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_messages_lead ON messages(lead_id);
CREATE INDEX IF NOT EXISTS idx_messages_campaign ON messages(campaign_id);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);

-- ============================================================
-- Replies (inbound)
-- ============================================================
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER REFERENCES messages(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    classification TEXT
        CHECK (classification IN (
            'interested', 'not_interested', 'objection', 'referral',
            'out_of_office', 'unsubscribe', 'bounce', 'other'
        )),
    subject TEXT,
    body TEXT,
    raw_headers TEXT,
    received_at TEXT,
    processed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_replies_lead ON replies(lead_id);
CREATE INDEX IF NOT EXISTS idx_replies_message ON replies(message_id);

-- ============================================================
-- Suppression list (opt-outs, bounces, invalids)
-- ============================================================
CREATE TABLE IF NOT EXISTS suppression (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    reason TEXT NOT NULL
        CHECK (reason IN (
            'opt_out', 'unsubscribe', 'bounce', 'complaint',
            'manual', 'invalid', 'rejected'
        )),
    source TEXT,
    original_lead_id INTEGER REFERENCES leads(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_suppression_email ON suppression(email);

-- ============================================================
-- Audit log
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,            -- JSON
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
"""


def get_data_dir() -> str:
    """Get the data directory path from environment or default."""
    return os.environ.get("SALESOS_DATA_DIR", DEFAULT_DATA_DIR)


def get_db_path(data_dir: str | None = None) -> str:
    """Get the database file path, creating the data directory if needed."""
    if data_dir is None:
        data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "salesos.db")


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Create a new database connection with recommended pragmas."""
    if db_path is None:
        db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Context manager for atomic database transactions.

    Commits on success, rolls back on any exception.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_db(conn: sqlite3.Connection) -> None:
    """Migrate existing tables to add new optional columns if missing."""
    cursor = conn.execute("PRAGMA table_info(messages)")
    columns = {row["name"] for row in cursor.fetchall()}

    migrations = [
        ("recipient", "ALTER TABLE messages ADD COLUMN recipient TEXT"),
        ("quality_status", "ALTER TABLE messages ADD COLUMN quality_status TEXT DEFAULT 'pending'"),
        ("selected_service", "ALTER TABLE messages ADD COLUMN selected_service TEXT"),
        ("qualification_score", "ALTER TABLE messages ADD COLUMN qualification_score REAL"),
        ("evidence_references", "ALTER TABLE messages ADD COLUMN evidence_references TEXT"),
        ("provider", "ALTER TABLE messages ADD COLUMN provider TEXT"),
        ("model", "ALTER TABLE messages ADD COLUMN model TEXT"),
        ("validation_errors", "ALTER TABLE messages ADD COLUMN validation_errors TEXT"),
    ]
    for col_name, sql in migrations:
        if col_name not in columns:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass


def init_db(db_path: str | None = None) -> sqlite3.Connection:
    """Initialize the database with the full schema.

    Safe to call repeatedly — all statements use IF NOT EXISTS.
    """
    conn = get_connection(db_path)
    conn.executescript(SCHEMA)
    _migrate_db(conn)
    conn.commit()
    return conn


def audit_log(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int | None,
    action: str,
    details: str | None = None,
) -> None:
    """Record an entry in the audit log."""
    conn.execute(
        "INSERT INTO audit_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (entity_type, entity_id, action, details),
    )
