"""Gmail API Client & OAuth 2.0 Integration for NexAgentLab SalesOS.

Handles official Google OAuth 2.0 authentication, token refresh, sender identity
verification (nexagentlab.tech@gmail.com), draft creation/sync, live email dispatch,
token redaction, and ambiguous network outcome defense.
"""

from __future__ import annotations

import base64
import email.message
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Official Google OAuth & Gmail API endpoints
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

DEFAULT_SENDER = "nexagentlab.tech@gmail.com"
DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.readonly",
]


# ── Exceptions ───────────────────────────────────────────────────────


class GmailError(Exception):
    """Base exception for Gmail operations."""


class OAuthMissingError(GmailError):
    """Raised when credentials.json or token.json is missing or invalid."""


class SenderMismatchError(GmailError):
    """Raised when authenticated Gmail account does not match configured sender."""


class TokenExpiredError(GmailError):
    """Raised when OAuth token is expired and cannot be refreshed."""


class AmbiguousSendError(GmailError):
    """Raised when a network failure occurs during send and delivery state is uncertain."""


class HistoryExpiredError(GmailError):
    """Raised when startHistoryId has expired or is invalid, requiring full recovery sync."""


class InsufficientScopeError(GmailError):
    """Raised when required Gmail read permissions are not granted."""


# ── Models ───────────────────────────────────────────────────────────


class GmailStatus(BaseModel):
    """Current status of Gmail OAuth and connectivity."""

    credentials_path: str
    credentials_found: bool = False
    token_path: str
    token_found: bool = False
    token_valid: bool = False
    token_expired: bool = False
    can_refresh: bool = False
    configured_sender: str = DEFAULT_SENDER
    authenticated_email: Optional[str] = None
    identity_matched: bool = False
    granted_scopes: list[str] = Field(default_factory=list)
    has_read_scope: bool = False
    dry_run: bool = True
    kill_switch_active: bool = False
    error: Optional[str] = None


# ── Secret Redaction ─────────────────────────────────────────────────


def redact_secret(val: str | None) -> str:
    """Redact a secret token, API key, or authorization code."""
    if not val:
        return "[NOT SET]"
    val_str = str(val).strip()
    if len(val_str) <= 8:
        return "[REDACTED]"
    return f"{val_str[:4]}...[REDACTED]...{val_str[-4:]}"


def redact_token_dict(data: dict) -> dict:
    """Return a copy of token/credentials dictionary with sensitive secrets redacted."""
    sensitive_keys = {
        "access_token",
        "refresh_token",
        "client_secret",
        "code",
        "id_token",
    }
    redacted = {}
    for k, v in data.items():
        if isinstance(v, dict):
            redacted[k] = redact_token_dict(v)
        elif k in sensitive_keys and isinstance(v, str):
            redacted[k] = redact_secret(v)
        else:
            redacted[k] = v
    return redacted


# ── Path & Config Resolution ─────────────────────────────────────────


def get_credentials_path() -> Path:
    """Resolve the path to credentials.json."""
    custom = os.environ.get("GMAIL_CREDENTIALS_FILE")
    if custom:
        return Path(custom).resolve()

    # Check project root, then data/
    root_candidate = Path("credentials.json").resolve()
    if root_candidate.exists():
        return root_candidate

    data_candidate = Path("data/credentials.json").resolve()
    if data_candidate.exists():
        return data_candidate

    return root_candidate


def get_token_path() -> Path:
    """Resolve the path to token.json."""
    custom = os.environ.get("GMAIL_TOKEN_FILE")
    if custom:
        return Path(custom).resolve()

    # Preferred location: data/token.json, fallback to project root
    data_candidate = Path("data/token.json").resolve()
    if data_candidate.exists():
        return data_candidate

    root_candidate = Path("token.json").resolve()
    if root_candidate.exists():
        return root_candidate

    return data_candidate


def get_configured_sender() -> str:
    """Get the authorized sending email address."""
    return os.environ.get("GMAIL_SENDER", DEFAULT_SENDER).strip().lower()


def is_dry_run() -> bool:
    """Check if dry-run mode is enabled (default is True for safety)."""
    val = os.environ.get("GMAIL_DRY_RUN", "true").strip().lower()
    return val not in ("false", "0", "no")


def is_kill_switch_active() -> bool:
    """Check if emergency campaign kill switch is active."""
    val = os.environ.get("CAMPAIGN_KILL_SWITCH", "false").strip().lower()
    return val in ("true", "1", "yes")


# ── OAuth Helpers ────────────────────────────────────────────────────


def load_credentials_file(path: Path | None = None) -> dict:
    """Load and parse Google OAuth client credentials JSON."""
    p = path or get_credentials_path()
    if not p.exists():
        raise OAuthMissingError(
            f"Google OAuth credentials file not found at: {p}\n"
            f"Please download credentials.json from Google Cloud Console "
            f"(APIs & Services > Credentials > Create Credentials > OAuth client ID > Desktop app)."
        )

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise OAuthMissingError(f"Failed to read credentials file at {p}: {e}")

    # Standard Google format: {"installed": {...}} or {"web": {...}}
    client_info = data.get("installed") or data.get("web") or data
    if not client_info.get("client_id") or not client_info.get("client_secret"):
        raise OAuthMissingError(
            f"Invalid credentials file at {p}: missing 'client_id' or 'client_secret'."
        )

    return client_info


def load_token_file(path: Path | None = None) -> dict:
    """Load saved token.json."""
    p = path or get_token_path()
    if not p.exists():
        raise OAuthMissingError(
            f"OAuth token file not found at: {p}\n"
            f"Run 'salesos gmail auth' to authenticate your Gmail account."
        )

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise OAuthMissingError(f"Failed to read token file at {p}: {e}")

    return data


def save_token_file(token_data: dict, path: Path | None = None) -> Path:
    """Save OAuth token data securely to token.json."""
    p = path or get_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)
    return p


def generate_auth_url(
    client_info: dict,
    redirect_uri: str = "http://localhost:8080/",
    state: str = "salesos_auth",
) -> str:
    """Construct Google OAuth 2.0 authorization URL."""
    params = {
        "client_id": client_info["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(DEFAULT_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    req = requests.Request("GET", GOOGLE_AUTH_URL, params=params)
    return req.prepare().url


def exchange_code_for_token(
    client_info: dict,
    code: str,
    redirect_uri: str = "http://localhost:8080/",
) -> dict:
    """Exchange authorization code for access and refresh tokens."""
    payload = {
        "code": code.strip(),
        "client_id": client_info["client_id"],
        "client_secret": client_info["client_secret"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    resp = requests.post(GOOGLE_TOKEN_URL, data=payload, timeout=15)
    if resp.status_code != 200:
        raise GmailError(
            f"Failed to exchange auth code for tokens ({resp.status_code}): {resp.text}"
        )

    token_data = resp.json()
    token_data["client_id"] = client_info["client_id"]
    token_data["client_secret"] = client_info["client_secret"]
    token_data["created_at"] = time.time()
    return token_data


def refresh_access_token(token_data: dict) -> dict:
    """Refresh an expired access token using the stored refresh_token."""
    refresh_token = token_data.get("refresh_token")
    client_id = token_data.get("client_id")
    client_secret = token_data.get("client_secret")

    if not refresh_token:
        raise TokenExpiredError("Cannot refresh access token: missing refresh_token")

    # If client info is not inside token data, load from credentials file
    if not client_id or not client_secret:
        try:
            creds = load_credentials_file()
            client_id = creds.get("client_id")
            client_secret = creds.get("client_secret")
        except Exception:
            pass

    if not client_id or not client_secret:
        raise TokenExpiredError("Cannot refresh access token: missing client credentials")

    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    resp = requests.post(GOOGLE_TOKEN_URL, data=payload, timeout=15)
    if resp.status_code != 200:
        raise TokenExpiredError(
            f"Token refresh failed ({resp.status_code}): {resp.text}"
        )

    refreshed = resp.json()
    token_data["access_token"] = refreshed["access_token"]
    token_data["expires_in"] = refreshed.get("expires_in", 3600)
    token_data["created_at"] = time.time()
    if "scope" in refreshed:
        token_data["scope"] = refreshed["scope"]

    save_token_file(token_data)
    logger.info("Successfully refreshed OAuth access token.")
    return token_data


def get_valid_access_token() -> str:
    """Load token and refresh automatically if expired."""
    token_data = load_token_file()
    access_token = token_data.get("access_token")
    created_at = token_data.get("created_at", 0)
    expires_in = token_data.get("expires_in", 3600)

    # Allow 60 seconds buffer
    is_expired = (time.time() - created_at) > (expires_in - 60)
    if is_expired or not access_token:
        token_data = refresh_access_token(token_data)
        access_token = token_data["access_token"]

    return access_token


# ── User Profile & Identity Verification ─────────────────────────────


def get_user_profile(access_token: str | None = None) -> dict:
    """Fetch authenticated Gmail user profile."""
    token = access_token or get_valid_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(f"{GMAIL_API_BASE}/profile", headers=headers, timeout=15)
    if resp.status_code != 200:
        raise GmailError(f"Failed to fetch Gmail profile ({resp.status_code}): {resp.text}")
    return resp.json()


def verify_sender_identity(access_token: str | None = None) -> str:
    """Verify that the authenticated Gmail account matches the configured sender.

    Returns:
        str: Verified email address.

    Raises:
        SenderMismatchError: If the authenticated account does not match GMAIL_SENDER.
    """
    profile = get_user_profile(access_token)
    authenticated_email = profile.get("emailAddress", "").strip().lower()
    expected_sender = get_configured_sender()

    if authenticated_email != expected_sender:
        raise SenderMismatchError(
            f"Sender identity mismatch! Configured sender is '{expected_sender}', "
            f"but authenticated Gmail account is '{authenticated_email}'.\n"
            f"Please authenticate with the official agency account ({expected_sender})."
        )

    return authenticated_email


# ── MIME Message Creation ────────────────────────────────────────────


def build_raw_email(
    to_email: str,
    subject: str,
    body: str,
    sender: str | None = None,
) -> str:
    """Build an RFC 2822 email message and base64url-encode it for Gmail API."""
    from_addr = sender or get_configured_sender()

    msg = email.message.EmailMessage()
    msg["To"] = to_email.strip()
    msg["From"] = f"NexAgentLab <{from_addr}>"
    msg["Subject"] = subject.strip()
    msg.set_content(body.strip(), subtype="plain", charset="utf-8")

    raw_bytes = msg.as_bytes()
    return base64.urlsafe_b64encode(raw_bytes).decode("utf-8")


# ── Drafts API ───────────────────────────────────────────────────────


def create_or_update_draft(
    to_email: str,
    subject: str,
    body: str,
    draft_id: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Create a new Gmail draft or update an existing one.

    Returns:
        dict: {"id": draft_id, "message_id": message_id}
    """
    token = access_token or get_valid_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    raw = build_raw_email(to_email, subject, body)
    payload = {"message": {"raw": raw}}

    if draft_id:
        url = f"{GMAIL_API_BASE}/drafts/{draft_id}"
        resp = requests.put(url, headers=headers, json=payload, timeout=20)
    else:
        url = f"{GMAIL_API_BASE}/drafts"
        resp = requests.post(url, headers=headers, json=payload, timeout=20)

    if resp.status_code not in (200, 201):
        raise GmailError(f"Failed to create/update Gmail draft ({resp.status_code}): {resp.text}")

    data = resp.json()
    return {
        "draft_id": data.get("id"),
        "message_id": data.get("message", {}).get("id"),
    }


# ── Send API ─────────────────────────────────────────────────────────


def send_message_api(
    to_email: str,
    subject: str,
    body: str,
    draft_id: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Send an email via Gmail API with ambiguous outcome defense.

    Returns:
        dict: {"message_id": ..., "thread_id": ...}

    Raises:
        AmbiguousSendError: If a connection error occurs during dispatch.
        GmailError: If Gmail returns an error response.
    """
    token = access_token or get_valid_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    raw = build_raw_email(to_email, subject, body)

    try:
        if draft_id:
            # Send using existing draft
            url = f"{GMAIL_API_BASE}/drafts/send"
            payload = {"id": draft_id}
            resp = requests.post(url, headers=headers, json=payload, timeout=25)
        else:
            # Direct send
            url = f"{GMAIL_API_BASE}/messages/send"
            payload = {"raw": raw}
            resp = requests.post(url, headers=headers, json=payload, timeout=25)

    except (requests.Timeout, requests.ConnectionError) as exc:
        logger.error("Ambiguous network outcome during Gmail send to %s: %s", to_email, exc)
        raise AmbiguousSendError(
            f"Network timeout/error during email dispatch to {to_email}. "
            f"Delivery outcome is ambiguous. Do NOT blindly re-send without verifying in Gmail sent box: {exc}"
        ) from exc

    if resp.status_code not in (200, 201):
        raise GmailError(f"Gmail send API rejected message ({resp.status_code}): {resp.text}")

    data = resp.json()
    return {
        "message_id": data.get("id"),
        "thread_id": data.get("threadId"),
    }


# ── Inbound Message Retrieval & History Sync ─────────────────────────


def has_readonly_scope(token_data: dict) -> bool:
    """Check if token possesses permissions to read messages/history."""
    scopes_raw = token_data.get("scope", "")
    scopes = scopes_raw.split() if isinstance(scopes_raw, str) else (scopes_raw or [])
    return any(
        "readonly" in s or "mail.google.com" in s or "modify" in s
        for s in scopes
    )


def list_inbox_messages(
    query: str = "label:INBOX",
    max_results: int = 50,
    page_token: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Query Gmail for list of message IDs matching criteria."""
    token = access_token or get_valid_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {"q": query, "maxResults": max_results}
    if page_token:
        params["pageToken"] = page_token

    resp = requests.get(f"{GMAIL_API_BASE}/messages", headers=headers, params=params, timeout=20)
    if resp.status_code == 403:
        raise InsufficientScopeError(
            "Access forbidden (403): Gmail read permissions missing. "
            "Please run 'salesos gmail auth' to authorize reading inbound replies."
        )
    if resp.status_code != 200:
        raise GmailError(f"Failed to list messages ({resp.status_code}): {resp.text}")

    return resp.json()


def get_history(
    start_history_id: str,
    max_results: int = 100,
    page_token: str | None = None,
    access_token: str | None = None,
) -> dict:
    """Retrieve incremental message events since start_history_id."""
    token = access_token or get_valid_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {
        "startHistoryId": str(start_history_id),
        "maxResults": max_results,
        "historyTypes": "messageAdded",
    }
    if page_token:
        params["pageToken"] = page_token

    resp = requests.get(f"{GMAIL_API_BASE}/history", headers=headers, params=params, timeout=20)
    if resp.status_code == 404:
        raise HistoryExpiredError(
            f"History ID {start_history_id} has expired or is outside Google's retention window. "
            "Full recovery sync required."
        )
    if resp.status_code == 403:
        raise InsufficientScopeError("Access forbidden (403): Read permissions missing.")
    if resp.status_code != 200:
        raise GmailError(f"Failed to fetch Gmail history ({resp.status_code}): {resp.text}")

    return resp.json()


def get_message_detail(
    message_id: str,
    access_token: str | None = None,
) -> dict:
    """Fetch complete raw message payload by ID."""
    token = access_token or get_valid_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        f"{GMAIL_API_BASE}/messages/{message_id}",
        headers=headers,
        params={"format": "full"},
        timeout=20,
    )
    if resp.status_code != 200:
        raise GmailError(f"Failed to fetch message {message_id} ({resp.status_code}): {resp.text}")

    return resp.json()


def _decode_body_data(data_b64: str) -> str:
    """Safely decode base64url message part body."""
    try:
        # Standardize padding
        rem = len(data_b64) % 4
        if rem > 0:
            data_b64 += "=" * (4 - rem)
        decoded_bytes = base64.urlsafe_b64decode(data_b64)
        return decoded_bytes.decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_gmail_message(data: dict) -> dict:
    """Extract standard fields, headers, and clean body text from Gmail API JSON."""
    msg_id = data.get("id", "")
    thread_id = data.get("threadId", "")
    history_id = data.get("historyId", "")
    snippet = data.get("snippet", "")

    headers_list = data.get("payload", {}).get("headers", [])
    headers: dict[str, str] = {}
    for h in headers_list:
        headers[h.get("name", "").lower()] = h.get("value", "")

    from_header = headers.get("from", "")
    to_header = headers.get("to", "")
    subject = headers.get("subject", "")
    date_str = headers.get("date", "")
    in_reply_to = headers.get("in-reply-to", "")
    references = headers.get("references", "")
    message_id_header = headers.get("message-id", "")

    # Extract clean email address from "From: Name <email@domain.com>"
    import re
    email_match = re.search(r"<([^>]+)>", from_header)
    clean_from_email = email_match.group(1).strip().lower() if email_match else from_header.strip().lower()

    # Extract body text from payload
    body_text = ""
    payload = data.get("payload", {})

    def extract_from_part(part: dict) -> str:
        mime_type = part.get("mimeType", "")
        data_b64 = part.get("body", {}).get("data", "")

        if mime_type == "text/plain" and data_b64:
            return _decode_body_data(data_b64)

        if "parts" in part:
            for subpart in part["parts"]:
                txt = extract_from_part(subpart)
                if txt:
                    return txt

        # Fallback to html if plain not found
        if mime_type == "text/html" and data_b64:
            raw_html = _decode_body_data(data_b64)
            # Basic tag stripping
            clean = re.sub(r"<[^>]+>", " ", raw_html)
            return " ".join(clean.split())

        return ""

    body_text = extract_from_part(payload)
    if not body_text and payload.get("body", {}).get("data"):
        body_text = _decode_body_data(payload["body"]["data"])

    if not body_text:
        body_text = snippet

    return {
        "gmail_message_id": msg_id,
        "gmail_thread_id": thread_id,
        "history_id": history_id,
        "from_email": clean_from_email,
        "from_header": from_header,
        "to_email": to_header,
        "subject": subject,
        "date": date_str,
        "in_reply_to": in_reply_to,
        "references": references,
        "message_id_header": message_id_header,
        "body": body_text.strip(),
        "snippet": snippet,
        "raw_headers": json.dumps(headers),
    }


# ── Status Query ─────────────────────────────────────────────────────


def get_gmail_status() -> GmailStatus:
    """Inspect local OAuth setup, credentials, and connection health."""
    creds_p = get_credentials_path()
    token_p = get_token_path()
    sender = get_configured_sender()
    dry_run = is_dry_run()
    kill_switch = is_kill_switch_active()

    status = GmailStatus(
        credentials_path=str(creds_p),
        credentials_found=creds_p.exists(),
        token_path=str(token_p),
        token_found=token_p.exists(),
        configured_sender=sender,
        dry_run=dry_run,
        kill_switch_active=kill_switch,
    )

    if not token_p.exists():
        status.error = "OAuth token not found. Run 'salesos gmail auth' to authenticate."
        return status

    try:
        token_data = load_token_file(token_p)
        created_at = token_data.get("created_at", 0)
        expires_in = token_data.get("expires_in", 3600)
        has_refresh = bool(token_data.get("refresh_token"))

        status.can_refresh = has_refresh
        is_expired = (time.time() - created_at) > expires_in
        status.token_expired = is_expired
        status.token_valid = not is_expired or has_refresh

        # Parse scopes
        scopes_raw = token_data.get("scope", "")
        if isinstance(scopes_raw, str):
            status.granted_scopes = scopes_raw.split()
        elif isinstance(scopes_raw, list):
            status.granted_scopes = scopes_raw
        status.has_read_scope = any(
            "readonly" in s or "mail.google.com" in s or "modify" in s
            for s in status.granted_scopes
        )

        # Check sender identity if token is valid
        if status.token_valid:
            try:
                profile = get_user_profile()
                auth_email = profile.get("emailAddress", "").strip().lower()
                status.authenticated_email = auth_email
                status.identity_matched = (auth_email == sender)
                if not status.identity_matched:
                    status.error = (
                        f"Sender mismatch: Authenticated as '{auth_email}', "
                        f"expected '{sender}'"
                    )
            except Exception as e:
                status.error = f"Failed to verify profile with Gmail API: {e}"

    except Exception as e:
        status.error = f"Error reading token: {e}"

    return status
