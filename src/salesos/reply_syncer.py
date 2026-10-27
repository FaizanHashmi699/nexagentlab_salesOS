"""Gmail Inbound Reply Synchronization Engine for NexAgentLab SalesOS.

Performs reliable, idempotent synchronization of incoming emails from Gmail API.
Supports incremental sync using Gmail history IDs, safe recovery sync on expired
cursors, durable database checkpoints, thread/header conversation matching,
agency-sender exclusion, and read-only dry-run preview mode.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .db import audit_log, transaction
from .gmail_client import (
    GmailError,
    HistoryExpiredError,
    InsufficientScopeError,
    OAuthMissingError,
    get_configured_sender,
    get_history,
    get_message_detail,
    get_user_profile,
    has_readonly_scope,
    list_inbox_messages,
    load_token_file,
    parse_gmail_message,
)
from .suppression import normalize_email

logger = logging.getLogger(__name__)

DEFAULT_CHECKPOINT_KEY = "gmail_inbound_sync"


@dataclass
class SyncResult:
    """Detailed summary of an inbound reply synchronization job."""

    messages_evaluated: int = 0
    new_replies_saved: int = 0
    duplicates_skipped: int = 0
    agency_sent_skipped: int = 0
    matched_count: int = 0
    unmatched_count: int = 0
    dry_run: bool = False
    sync_mode: str = "incremental"
    start_history_id: Optional[str] = None
    new_history_id: Optional[str] = None
    errors: list[str] = field(default_factory=list)
    preview_items: list[dict] = field(default_factory=list)


# ── Checkpoint Persistence ───────────────────────────────────────────


def get_last_checkpoint(
    conn: sqlite3.Connection,
    key: str = DEFAULT_CHECKPOINT_KEY,
) -> Optional[dict]:
    """Retrieve stored synchronization cursor checkpoint."""
    row = conn.execute(
        "SELECT * FROM sync_checkpoints WHERE checkpoint_key = ?",
        (key,),
    ).fetchone()
    if row:
        return dict(row)
    return None


def save_checkpoint(
    conn: sqlite3.Connection,
    key: str = DEFAULT_CHECKPOINT_KEY,
    history_id: Optional[str] = None,
    synced_count: int = 0,
    status: str = "success",
    error_message: Optional[str] = None,
) -> None:
    """Persist or update synchronization cursor checkpoint."""
    with transaction(conn):
        existing = conn.execute(
            "SELECT id FROM sync_checkpoints WHERE checkpoint_key = ?",
            (key,),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE sync_checkpoints SET
                   last_history_id = coalesce(?, last_history_id),
                   last_sync_at = datetime('now'),
                   messages_synced = messages_synced + ?,
                   status = ?,
                   error_message = ?,
                   updated_at = datetime('now')
                   WHERE id = ?""",
                (history_id, synced_count, status, error_message, existing["id"]),
            )
        else:
            conn.execute(
                """INSERT INTO sync_checkpoints (
                   checkpoint_key, last_history_id, last_sync_at,
                   messages_synced, status, error_message
                ) VALUES (?, ?, datetime('now'), ?, ?, ?)""",
                (key, history_id, synced_count, status, error_message),
            )


# ── Conversation & Lead Matching ─────────────────────────────────────


def match_message_to_lead(
    conn: sqlite3.Connection,
    parsed_email: dict,
) -> tuple[Optional[int], Optional[int]]:
    """Match an incoming email to a registered lead and outbound message.

    Matching hierarchy:
    1. Direct Gmail thread ID matching to a sent message.
    2. In-Reply-To or References matching to our outbound Gmail message ID.
    3. Sender email matching to recipient in messages table.
    4. Sender email matching to leads table.

    Returns:
        tuple[Optional[int], Optional[int]]: (lead_id, original_message_id)
    """
    thread_id = parsed_email.get("gmail_thread_id")
    in_reply_to = parsed_email.get("in_reply_to") or ""
    references = parsed_email.get("references") or ""
    from_email = normalize_email(parsed_email.get("from_email"))

    # 1. Match by thread ID
    if thread_id:
        row = conn.execute(
            """SELECT id, lead_id FROM messages
               WHERE gmail_thread_id = ?
               ORDER BY id DESC LIMIT 1""",
            (thread_id,),
        ).fetchone()
        if row:
            return row["lead_id"], row["id"]

    # 2. Match by In-Reply-To / References header
    if in_reply_to:
        clean_in_reply = in_reply_to.strip().strip("<>").strip()
        row = conn.execute(
            """SELECT id, lead_id FROM messages
               WHERE gmail_message_id = ?
               ORDER BY id DESC LIMIT 1""",
            (clean_in_reply,),
        ).fetchone()
        if row:
            return row["lead_id"], row["id"]

    # 3. Match by sender email to sent messages
    if from_email:
        row = conn.execute(
            """SELECT id, lead_id FROM messages
               WHERE lower(trim(recipient)) = ?
               ORDER BY id DESC LIMIT 1""",
            (from_email,),
        ).fetchone()
        if row:
            return row["lead_id"], row["id"]

        # 4. Match directly to leads table
        lead_row = conn.execute(
            """SELECT id FROM leads
               WHERE lower(trim(email)) = ?
               LIMIT 1""",
            (from_email,),
        ).fetchone()
        if lead_row:
            return lead_row["id"], None

    return None, None


# ── Synchronization Core ─────────────────────────────────────────────


def sync_inbound_replies(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    max_messages: int = 50,
    force_recovery: bool = False,
    query_filter: str = "label:INBOX",
) -> SyncResult:
    """Fetch, parse, match, and persist incoming replies from Gmail.

    Args:
        conn: SQLite connection.
        dry_run: When True, previews actions without altering DB or checkpoints.
        max_messages: Maximum batch size.
        force_recovery: When True, skips history ID cursor and scans inbox directly.
        query_filter: Gmail search query filter.

    Returns:
        SyncResult: Comprehensive metrics and preview items.
    """
    agency_sender = get_configured_sender()
    res = SyncResult(dry_run=dry_run)

    # Verify read scope if live sync
    if not dry_run:
        try:
            token_data = load_token_file()
            if not has_readonly_scope(token_data):
                raise InsufficientScopeError(
                    "Missing 'gmail.readonly' OAuth scope required to inspect incoming replies. "
                    "Run 'salesos gmail auth' to grant access."
                )
        except Exception as e:
            if not isinstance(e, InsufficientScopeError):
                logger.warning("Could not verify token scope: %s", e)
            else:
                raise

    # 1. Check existing sync cursor
    checkpoint = get_last_checkpoint(conn)
    start_history_id = checkpoint.get("last_history_id") if checkpoint else None
    res.start_history_id = start_history_id

    message_ids_to_process: list[str] = []
    new_history_cursor: Optional[str] = None

    # 2. Attempt incremental history sync if cursor is available
    if start_history_id and not force_recovery:
        res.sync_mode = "incremental"
        try:
            hist_resp = get_history(start_history_id=start_history_id, max_results=max_messages)
            new_history_cursor = hist_resp.get("historyId")

            history_records = hist_resp.get("history", [])
            for h in history_records:
                for added in h.get("messagesAdded", []):
                    mid = added.get("message", {}).get("id")
                    if mid and mid not in message_ids_to_process:
                        message_ids_to_process.append(mid)

        except HistoryExpiredError:
            logger.warning("History cursor %s expired. Performing recovery sync.", start_history_id)
            res.sync_mode = "recovery"
            res.errors.append("History cursor expired; executed recovery sync.")
            force_recovery = True
        except Exception as e:
            logger.error("Error during history sync: %s", e)
            res.errors.append(f"History query error: {e}")
            force_recovery = True

    # 3. Fallback to inbox query if initial, forced, or recovery
    if not start_history_id or force_recovery:
        if not res.sync_mode.startswith("recovery"):
            res.sync_mode = "initial"

        try:
            list_resp = list_inbox_messages(query=query_filter, max_results=max_messages)
            for m in list_resp.get("messages", []):
                mid = m.get("id")
                if mid and mid not in message_ids_to_process:
                    message_ids_to_process.append(mid)
        except Exception as e:
            res.errors.append(f"Inbox listing error: {e}")
            if not dry_run:
                save_checkpoint(conn, status="error", error_message=str(e))
            return res

    res.messages_evaluated = len(message_ids_to_process)

    # 4. Fetch details, parse, and match each message
    for mid in message_ids_to_process:
        # Check idempotency: skip already persisted message
        existing = conn.execute(
            "SELECT id FROM replies WHERE gmail_message_id = ?",
            (mid,),
        ).fetchone()
        if existing:
            res.duplicates_skipped += 1
            continue

        try:
            detail_data = get_message_detail(mid)
            parsed = parse_gmail_message(detail_data)
        except Exception as e:
            logger.error("Failed to fetch detail for message %s: %s", mid, e)
            res.errors.append(f"Detail fetch failed for {mid}: {e}")
            continue

        # Keep track of latest history ID seen
        msg_hist_id = parsed.get("history_id")
        if msg_hist_id and (not new_history_cursor or int(msg_hist_id) > int(new_history_cursor)):
            new_history_cursor = str(msg_hist_id)

        from_email = parsed["from_email"]

        # Filter out outgoing messages sent by agency account
        if from_email == agency_sender:
            res.agency_sent_skipped += 1
            continue

        # Match to lead and outbound message
        lead_id, outbound_msg_id = match_message_to_lead(conn, parsed)
        if lead_id:
            res.matched_count += 1
        else:
            res.unmatched_count += 1

        preview_record = {
            "gmail_message_id": mid,
            "gmail_thread_id": parsed["gmail_thread_id"],
            "from_email": from_email,
            "subject": parsed["subject"],
            "lead_id": lead_id,
            "outbound_msg_id": outbound_msg_id,
            "date": parsed["date"],
            "snippet": parsed["snippet"][:80],
        }

        if dry_run:
            res.preview_items.append(preview_record)
            res.new_replies_saved += 1
            continue

        # Persist new reply in DB
        with transaction(conn):
            cur = conn.execute(
                """INSERT INTO replies (
                   message_id, lead_id, subject, body, raw_headers,
                   received_at, processed_at, gmail_message_id,
                   gmail_thread_id, from_email, to_email,
                   needs_review, is_classified
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, 1, 0)""",
                (
                    outbound_msg_id,
                    lead_id,
                    parsed["subject"],
                    parsed["body"],
                    parsed["raw_headers"],
                    parsed["date"],
                    mid,
                    parsed["gmail_thread_id"],
                    from_email,
                    parsed["to_email"],
                ),
            )
            reply_db_id = cur.lastrowid

            audit_log(
                conn,
                "reply",
                reply_db_id,
                "reply_synced",
                json.dumps({
                    "gmail_message_id": mid,
                    "from_email": from_email,
                    "lead_id": lead_id,
                    "matched": bool(lead_id),
                }),
            )

        res.new_replies_saved += 1
        res.preview_items.append(preview_record)

    res.new_history_id = new_history_cursor

    # 5. Update synchronization checkpoint
    if not dry_run and new_history_cursor:
        save_checkpoint(
            conn,
            history_id=new_history_cursor,
            synced_count=res.new_replies_saved,
            status="success",
            error_message="; ".join(res.errors) if res.errors else None,
        )

    return res
