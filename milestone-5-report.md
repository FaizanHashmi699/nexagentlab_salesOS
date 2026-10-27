# Milestone 5 Report: Inbound Reply Monitoring, AI Reply Classification & Human-Controlled Follow-up Automation

**Project:** NexAgentLab SalesOS  
**Agency Target:** NexAgentLab (`https://nexagentlab.tech` / `nexagentlab.tech@gmail.com`)  
**Completed Date:** October 27, 2026  
**Status:** Complete & Fully Verified  
**Test Suite:** 188 passed / 188 total (100% pass rate across Milestones 1–5, 0 regressions)

---

## 1. Executive Summary

Milestone 5 implements an autonomous inbound communication lifecycle for NexAgentLab SalesOS, closing the sales loop while maintaining human-in-the-loop safety controls:

1. **Inbound Reply Synchronization:** Incremental, low-bandwidth polling via Gmail `historyId` with automatic cursor persistence and fallback recovery synchronization when cursor expires or becomes invalid.
2. **Deterministic & AI-Assisted Reply Classification:** 10 structured classification categories with immediate deterministic rules and optional free-tier LLM fallback (Groq / Gemini / OpenRouter) safeguarded against prompt injection.
3. **Immediate Compliance & Lead Protection:** Instant suppression on opt-out/unsubscribe requests; bounce detection marking sent messages as bounced and suppressing the address; instant cancellation of cold follow-up sequences upon any reply or engagement.
4. **Business-Day Follow-Up Sequence Engine:** Safe, non-intrusive scheduling algorithm that strictly honors business days (skipping Saturdays and Sundays), generates non-destructive reviewable drafts, and strictly forbids silent bulk or automated live dispatch.
5. **Unified CRM Lead Timeline:** Full-lifecycle event timeline combining imports, research, qualification, drafts, sends, inbound replies, classifications, follow-ups, and audit history.
6. **Suggested Response Generation:** AI/template response drafting for positive interest, meeting requests, and questions, ready for human review, editing, and approval via CLI.
7. **Complete Zero-Regression Test Suite:** 39 comprehensive unit and integration tests added, bringing total test suite from 149 to 188 passing tests.

---

## 2. Core Architecture & Implemented Components

```
                    ┌────────────────────────────┐
                    │    Gmail Inbound Sync      │
                    │ (historyId cursor / check) │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │   Thread & Lead Matching   │
                    │(In-Reply-To, Thread, Email)│
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                    ┌────────────────────────────┐
                    │ 10-Category Classification │
                    │ (Regex Rules + Free LLM)   │
                    └─────────────┬──────────────┘
                                  │
         ┌────────────────────────┼────────────────────────┐
         ▼                        ▼                        ▼
┌──────────────────┐    ┌──────────────────┐    ┌─────────────────────┐
│   Unsubscribe    │    │      Bounce      │    │  Positive Interest  │
│ - Instant Suppress│   │ - Status: bounced│    │ - Cancel Follow-ups │
│ - Cancel Followup│    │ - Suppress Email │    │ - Draft Suggested   │
└──────────────────┘    └──────────────────┘    │   Response          │
                                                └─────────────────────┘
```

### 2.1 Database Schema Enhancements (`src/salesos/db.py`)
- **`replies` Table:** Migrated to support standalone incoming emails (`lead_id` nullable), Gmail message and thread IDs (`gmail_message_id`, `gmail_thread_id`), classification metadata (`confidence`, `explanation`, `needs_review`, `suggested_reply`, `is_classified`, `history_id`).
- **`followups` Table:** Manages planned, scheduled, approved, cancelled, and sent follow-up steps per lead, tracking `step_number`, `due_date`, `status`, `generated_message_id`, and `cancellation_reason`.
- **`sync_checkpoints` Table:** Stores incremental cursor state (`last_history_id`, `last_sync_at`, `status`) to avoid fetching duplicate messages.
- **Backward-Compatible Migration:** `_migrate_db()` dynamically inspects SQLite `PRAGMA table_info` and applies non-destructive `ALTER TABLE` statements automatically.

### 2.2 Gmail Inbound Reply Synchronization (`src/salesos/reply_syncer.py`)
- **Incremental Polling:** Uses Gmail `history.list(startHistoryId=...)` to retrieve only new messages since the last recorded checkpoint.
- **Expired History Recovery:** Catches `HistoryExpiredError` (Gmail 404 / expired history cursor) and automatically transitions to a safe recovery scan of recent inbox messages.
- **Agency Exclusion:** Discards outbound agency messages (`nexagentlab.tech@gmail.com`) to prevent echo replies.
- **Accurate Lead Matching:** Multi-stage resolution:
  1. Primary: Exact `threadId` match against previously sent outreach messages.
  2. Secondary: `In-Reply-To` or `References` header matching stored Gmail message IDs.
  3. Tertiary: Normalized recipient email address match against known leads.
  4. Unmatched: Safely stored with `lead_id = NULL` for manual inspection.
- **Readonly Scope Enforcement:** Explicitly verifies `https://www.googleapis.com/auth/gmail.readonly` before attempting sync.

### 2.3 10-Category Reply Classification (`src/salesos/classifier.py`)
Classifies replies into 10 explicit categories:
1. `positive_interest`: Expresses interest in NexAgentLab offerings.
2. `meeting_request`: Explicitly asks for a call, meeting, or calendar invite.
3. `question`: Inquires about pricing, integration, services, or technical capabilities.
4. `objection`: Raises budget constraints, existing agency contracts, or timing issues.
5. `not_interested`: Direct, polite refusal ("not at this time", "pass").
6. `unsubscribe`: Opt-out request ("remove me", "unsubscribe", "stop").
7. `out_of_office`: Auto-responder / vacation message.
8. `bounce_or_delivery_failure`: Mailer-daemon / bounce notifications (e.g., 550 User unknown).
9. `ambiguous`: Short or unclear responses ("k", "maybe", thumbs up).
10. `unrelated`: Spam, newsletters, or third-party cold pitches.

**Two-Tier Classification Engine:**
1. **Tier 1 (Deterministic Rules):** Fast regex patterns for high-confidence matching of bounces, auto-responders, unsubscribe commands, and common meeting phrases.
2. **Tier 2 (AI Classification Fallback):** Calls configured free LLM provider (`GroqCloud` / `Gemini` / `OpenRouter`). Untrusted inbound email bodies are sanitized (`re.sub` control characters, truncated to 1,200 chars) with prompt injection countermeasures instructing the LLM to treat email body strictly as unverified data.

### 2.4 Immediate Safety Triggers
- **Unsubscribe:** Immediately adds recipient email to `suppression` table with reason `inbound_unsubscribe` and cancels all pending follow-ups.
- **Bounce:** Updates original sent message status to `bounced`, records suppression reason `inbound_bounce`, and updates lead status to `bounced`.
- **Engagement (Positive / Meeting / Objection):** Automatically cancels cold outreach follow-ups for that lead so automated messages never collide with real human sales conversations.

### 2.5 Business-Day Follow-Up Engine (`src/salesos/followup_scheduler.py`)
- **Weekend Skipping Calendar:** `add_business_days()` calculates delay exclusively across Monday through Friday (skipping Saturday and Sunday).
- **Configurable Sequence:**
  - Step 1: Default 3 business days after initial outreach.
  - Step 2: Default 5 business days after Step 1.
  - Maximum 2 follow-ups per lead (`FOLLOWUP_MAX_STEPS`).
- **Safety First:** Follow-up generation creates drafts in the database. Drafts are never sent automatically. They require explicit human approval via `salesos followups approve <ID>`.

### 2.6 Suggested Response Drafting
For `positive_interest`, `meeting_request`, and `question`, the system generates a draft response grounding itself in NexAgentLab's capabilities (`https://nexagentlab.tech`) and calendar link (`https://calendly.com/nexagentlab-tech/intro`). The draft is stored in `replies.suggested_reply` and flagged with `needs_review = 1`.

### 2.7 Lead CRM Timeline (`src/salesos/timeline.py`)
Aggregates lead touchpoints chronologically:
- Lead import & metadata
- Website research & identified opportunities
- Qualification score & service match
- Initial personalized draft creation & approval
- Live or dry-run outbound dispatch
- Inbound reply receipt & AI classification
- Follow-up sequence scheduling & status changes
- Suppression / opt-out records & audit log entries

---

## 3. CLI Commands

### Reply Intelligence Commands
```powershell
# Sync inbound messages (dry-run)
salesos replies sync --dry-run

# Sync inbound messages live
salesos replies sync

# Force full recovery sync
salesos replies sync --force-recovery

# List synced replies
salesos replies list
salesos replies list --classification positive_interest
salesos replies list --needs-review

# View reply detail with suggested response
salesos replies show 1

# Classify pending replies
salesos replies classify --pending

# Human review and response approval
salesos replies review 1 --approve-reply
salesos replies review 1 --override-class question --reply-text "Hi, here are our prices..."

# Overall reply intelligence health and metrics
salesos replies status
```

### Follow-Up Engine Commands
```powershell
# Plan follow-up steps for eligible leads
salesos followups plan

# View follow-ups currently due
salesos followups due

# Preview a follow-up draft
salesos followups preview 1

# Approve follow-up for outreach queue
salesos followups approve 1

# Cancel a follow-up
salesos followups cancel 1 --reason "Customer engaged via phone"

# Overall follow-up pipeline metrics
salesos followups status
```

### CRM Lead Timeline Command
```powershell
# View comprehensive history for a lead
salesos leads timeline 1
```

---

## 4. Test Suite Verification

Full test suite execution confirms complete functionality with 0 regressions:

```
============================= test session starts =============================
platform win32 -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0
rootdir: G:\nexagentlab_salesOS
configfile: pyproject.toml
collected 188 items

tests/test_importer.py ......................... [ 13%]
tests/test_milestone3.py ................................... [ 32%]
tests/test_milestone4.py ........................................... [ 55%]
tests/test_milestone5.py ....................................... [ 75%]
tests/test_researcher.py .............................................. [100%]

====================== 188 passed, 2 warnings in 38.41s =======================
```

### Milestone 5 Test Coverage (`tests/test_milestone5.py` - 39 tests):
- **Business Day Logic (2 tests):** Adding days within the same week and skipping across weekends.
- **Gmail Inbound Sync (9 tests):** Header parsing, thread matching, `In-Reply-To` matching, sender email fallback, unmatched handling, outbound agency exclusion, idempotent sync prevention, expired cursor recovery sync, missing `readonly` scope error.
- **Reply Classification (16 tests):** All 10 categories supported, deterministic rule matching for various phrases (unsubscribe, bounce, out of office, meeting requests, positive interest, not interested, objections, questions, ambiguous), unsubscribe instant suppression, bounce handling, positive interest follow-up cancellation, prompt injection defense.
- **Follow-Up Scheduling (4 tests):** Step 1 planning, due follow-up retrieval & approval, follow-up cancellation, duplicate prevention.
- **CRM Timeline (1 test):** Full lifecycle event reconstruction.
- **CLI Commands (7 tests):** `replies sync`, `replies status`, `replies list`, `replies show`, `replies review`, `followups plan`, `followups status`, `leads timeline`.

---

## 5. Security, Privacy & Compliance Verification

1. **OAuth Scopes & Least Privilege:** Added `https://www.googleapis.com/auth/gmail.readonly` strictly for reading inbound replies. No delete or mailbox administrative permissions are requested.
2. **Anti-Spam & Opt-Out Compliance:** Immediate, irreversible suppression upon any unsubscribe or opt-out signal. Bounced emails are marked and suppressed immediately to protect sender reputation.
3. **No Unattended Sending:** All follow-ups and reply responses are generated as reviewable drafts (`status = 'draft'`). Live outreach requires human intervention and explicit approval.
4. **Zero Paid API Requirement:** Works with deterministic rules and templates; supports free hosted models (Groq, Gemini, OpenRouter) with zero paid subscriptions.
5. **Prompt Injection Defense:** Untrusted inbound email bodies are sanitized and wrapped in protective delimiter tags with system prompt guardrails.
