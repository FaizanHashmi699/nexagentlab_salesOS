# NexAgentLab SalesOS

**Autonomous AI Sales System** for [NexAgentLab.tech](https://nexagentlab.tech)

> Local-first, privacy-respecting sales automation built with Python, SQLite, and AI agents.

## Status

🟢 **Milestone 1** — Lead import, validation, deduplication, and CLI  
🟢 **Milestone 2** — Research Agent & Opportunity Identification  
🟢 **Milestone 3** — Lead Qualification, Service Matching & Personalization  
🟢 **Milestone 4** — Gmail OAuth Integration & Controlled Outreach  
🟢 **Milestone 5** — Reply Intelligence, Classification & Follow-up Automation  
🟢 **Milestone 6** — Meeting Management, Sales Pipeline & Deal Intelligence  

## Quick Start (Windows)

### 1. Prerequisites

- Python 3.11+
- pip

### 2. Setup

```powershell
cd G:\nexagentlab_salesOS

# Create virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Initialize the database
salesos init
```

### 3. Import Leads

```powershell
# Preview a lead file (no changes made)
salesos preview data\samples\synthetic_leads.csv

# Import leads (dry run — validates without writing)
salesos import data\samples\synthetic_leads.csv --dry-run

# Import leads for real
salesos import data\samples\synthetic_leads.csv

# List imported leads
salesos list

# Show statistics
salesos stats

# Check for duplicates
salesos duplicates

# Export leads
salesos export output.csv
salesos export output.json --format json
```

### 4. Research Leads & Identify Opportunities

```powershell
# Preview research extraction for a URL or lead ID (read-only, no database write)
salesos research preview https://example.com
salesos research preview 1

# Check aggregate research status and AI provider availability
salesos research status

# Run research on eligible imported leads (batch limit configurable)
salesos research run --limit 10

# Force re-run research for a specific lead ID
salesos research run --lead-id 1 --force

# Retry leads whose previous research attempt failed
salesos research retry --limit 20

# Export research findings and opportunities to CSV or JSON
salesos research export data\exports\research_summary.csv
salesos research export data\exports\research_summary.json --format json
```

### 5. Qualify Leads & Match Services

```powershell
# Run qualification scoring on all eligible researched leads
salesos qualify run

# Run qualification with custom batch size or minimum score threshold
salesos qualify run --limit 20 --min-score 50

# Inspect qualification breakdown and explainable scoring for a lead
salesos qualify show 1
```

### 6. Generate Personalized Outreach Drafts

```powershell
# Generate personalized email drafts for qualified leads (template fallback if no API key)
salesos personalize run

# Generate draft for a specific lead or enforce a minimum score
salesos personalize run --lead-id 1
salesos personalize run --limit 10 --min-score 60

# List stored email drafts and review their Quality Gate status
salesos drafts list

# Inspect a generated draft with validation report and service match
salesos drafts show 1

# Export approved drafts to CSV or JSON for review
salesos drafts export data\exports\outreach_drafts.csv
salesos drafts export data\exports\outreach_drafts.json --format json
```

### 7. Gmail Integration & Authentication

```powershell
# Inspect Gmail OAuth status, token validity, and sender identity
salesos gmail status

# Authenticate with Google OAuth 2.0 (displays consent URL and prompts for auth code)
salesos gmail auth

# Authenticate with a provided authorization code directly
salesos gmail auth --code YOUR_AUTH_CODE
```

### 8. Controlled Outreach & Safe Dispatch

```powershell
# Preview an outreach draft and inspect complete pre-flight checklist
salesos outreach preview 1

# Explicitly approve a draft message for outreach
salesos outreach approve 1
salesos outreach approve 1 --force

# Queue all approved messages for outbound dispatch
salesos outreach queue
salesos outreach queue --limit 10 --service booking_system

# Synchronize queued drafts into Gmail Drafts folder for web review (dry-run default)
salesos outreach sync-drafts
salesos outreach sync-drafts --live

# Dispatch outreach message (DRY RUN by default — simulates send safely)
salesos outreach send 1
salesos outreach send --all-queued

# Live sending (Requires explicit --live flag AND valid OAuth account match)
salesos outreach send 1 --live
salesos outreach send --all-queued --live

# Add an email to central suppression (instantly cancels any pending outreach)
salesos outreach suppress client@example.com --reason opt_out

# View campaign outreach metrics, quota limits, and safety status
salesos outreach status
```

### 9. Reply Intelligence & Follow-Up Automation

```powershell
# Synchronize inbound customer replies from Gmail (dry run preview)
salesos replies sync --dry-run

# Synchronize inbound customer replies live using Gmail historyId
salesos replies sync

# Force full recovery sync if history cursor expires
salesos replies sync --force-recovery

# List received replies and their classification states
salesos replies list
salesos replies list --classification positive_interest
salesos replies list --needs-review

# View detailed reply information with email thread and suggested response draft
salesos replies show <REPLY_ID>

# Classify unclassified replies (deterministic rules + free LLM fallback)
salesos replies classify --pending

# Human review and approve suggested response
salesos replies review <REPLY_ID> --approve-reply
salesos replies review <REPLY_ID> --override-class question --reply-text "Custom response..."

# Check inbound synchronization and classification metrics
salesos replies status

# Evaluate unresponsive leads and plan business-day follow-up steps (skips weekends)
salesos followups plan

# List follow-ups that have reached their scheduled due date
salesos followups due

# Preview a scheduled follow-up draft and recipient details
salesos followups preview <FOLLOWUP_ID>

# Explicitly approve a scheduled follow-up draft for outreach
salesos followups approve <FOLLOWUP_ID>

# Cancel a scheduled follow-up
salesos followups cancel <FOLLOWUP_ID> --reason "Client contacted via phone"

# View follow-up sequence automation status and pipeline summary
salesos followups status

# View unified CRM conversation timeline for a specific lead
salesos leads timeline <LEAD_ID>
```

### 10. Meeting Management & Sales Pipeline Intelligence

```powershell
# List client discovery meetings (scheduled, completed, cancelled)
salesos meetings list
salesos meetings list --status scheduled

# Inspect deep meeting details, attendees, and recorded outcomes
salesos meetings show <MEETING_ID>

# Schedule a new meeting manually
salesos meetings create --lead-id 1 --title "Discovery Call" --time "2026-11-15 14:00"

# Reschedule or update a meeting
salesos meetings update <MEETING_ID> --time "2026-11-16 15:00" --url "https://meet.google.com/abc"

# Cancel a meeting with an auditable reason
salesos meetings cancel <MEETING_ID> --reason "Client requested reschedule"

# Mark a meeting as completed (auto-advances linked deal stage)
salesos meetings complete <MEETING_ID>

# Generate evidence-grounded meeting preparation brief (cites research evidence IDs)
salesos meetings brief <MEETING_ID>
salesos meetings brief <MEETING_ID> --no-ai

# Record structured post-meeting review and outcomes (generates draft follow-up if requested)
salesos meetings outcome <MEETING_ID> --notes "Interested in booking automation" --proposal yes --followup --next-step "Send proposal"

# Synchronize meetings with Calendly API (optional)
salesos meetings sync

# View interactive sales pipeline board grouped by stages
salesos pipeline board

# Inspect single deal history and evidentiary stage transitions
salesos pipeline show <DEAL_ID>

# Advance deal to next stage with evidence validation
salesos pipeline move <DEAL_ID> <STAGE>
salesos pipeline move <DEAL_ID> won --win-notes "Contract signed for $3,500"
salesos pipeline move <DEAL_ID> lost --loss-reason "Budget constraints"
salesos pipeline move <DEAL_ID> <STAGE> --force --reason "Manual executive override"

# Update deal value, owner, next action, or probability
salesos pipeline update <DEAL_ID> --value 3500 --owner "Saif" --next-action "Send contract"

# Generate comprehensive executive sales pipeline report
salesos pipeline report

# Export pipeline deals to CSV or JSON
salesos pipeline export data/exports/pipeline.csv
salesos pipeline export data/exports/pipeline.json --format json
```

### 11. Run Tests

```powershell
pytest tests/ -v
# Total tests: 222 passed (100% pass rate, zero regressions across Milestones 1–6)
```

## Google Cloud OAuth Setup (Zero Paid Services)

To authenticate your Gmail account (`nexagentlab.tech@gmail.com`) for draft synchronization or live sending:

1. Open [Google Cloud Console](https://console.cloud.google.com).
2. Create or select a project (e.g., `NexAgentLab SalesOS`).
3. Navigate to **APIs & Services > Library**, search for **Gmail API**, and click **Enable**.
4. Go to **APIs & Services > OAuth consent screen**:
   - Choose User Type: **External**.
   - Fill in App Name (`NexAgentLab SalesOS`) and User Support Email (`nexagentlab.tech@gmail.com`).
   - Add Test User: `nexagentlab.tech@gmail.com`.
5. Navigate to **APIs & Services > Credentials > Create Credentials > OAuth client ID**:
   - Application type: **Desktop app**.
   - Name: `SalesOS Local Client`.
6. Download the generated JSON credentials file and save it as `credentials.json` in the project root (or configure `GMAIL_CREDENTIALS_FILE`).
7. Run `salesos gmail auth`, open the provided URL in your browser, sign in with `nexagentlab.tech@gmail.com`, and provide the authorization code.

Tokens are stored securely in `data/token.json` and are automatically refreshed. Secrets and tokens are never printed, committed, or leaked in logs.

> [!IMPORTANT]
> **Milestone 5 Scope Requirement**: Inbound reply monitoring and classification requires reading incoming messages (`https://www.googleapis.com/auth/gmail.readonly`). If you authenticated prior to Milestone 5, run `salesos gmail auth` to grant the readonly scope. You can verify your active scopes at any time with `salesos gmail status`.

## Explicit Live-Send Activation Procedure

By default, SalesOS operates in **DRY-RUN / DRAFT-ONLY** mode:
1. All sends are simulated.
2. `GMAIL_DRY_RUN=true` is the default.
3. Drafts can be synced to your Gmail web interface using `salesos outreach sync-drafts --live` for visual human review before sending.

To enable live email dispatch:
1. Ensure your authorized account matches `nexagentlab.tech@gmail.com` via `salesos gmail status`.
2. Inspect the draft and pre-flight checklist: `salesos outreach preview <ID>`.
3. Approve the message: `salesos outreach approve <ID>`.
4. Run live dispatch explicitly:
   ```powershell
   salesos outreach send <ID> --live
   ```
   Or set `GMAIL_DRY_RUN=false` in your `.env` file.

### Emergency Kill Switch
If you ever need to instantly halt all sending operations, set:
```powershell
$env:CAMPAIGN_KILL_SWITCH="true"
```
Or in `.env`: `CAMPAIGN_KILL_SWITCH=true`. All outreach commands will immediately refuse to send.

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌───────────┐
│  CSV / XLSX  │────▶│   Importer   │────▶│  SQLite   │
│  JSON files  │     │  (validate,  │     │  Database  │
└─────────────┘     │  dedup, map) │     └───────────┘
                    └──────────────┘           │
                          │                    ▼
                    ┌──────────────┐     ┌───────────┐
                    │   CLI (Click │◀───▶│  Reports   │
                    │   + Rich)    │     │  & Export  │
                    └──────────────┘     └───────────┘
```

### Database Tables

| Table | Purpose |
|-------|---------|
| `leads` | Core lead records with unique email constraint |
| `imports` | Import history with file hash, counts, status |
| `research` | AI research findings per lead |
| `opportunities` | Identified business improvement opportunities |
| `campaigns` | Outreach campaign configuration & status |
| `messages` | Email drafts, queue, and delivery records |
| `replies` | Classified inbound replies, sentiment, suggested response |
| `followups` | Business-day scheduled follow-up steps, review status |
| `sync_checkpoints` | Incremental Gmail historyId synchronization state |
| `suppression` | Opt-out / bounce / invalid email blocklist |
| `deals` | Sales opportunities, pipeline stages, values, owner, and stage evidence |
| `meetings` | Scheduled and completed client calls, briefs, and structured outcomes |
| `audit_log` | Full audit trail of all system actions |

## Configuration

Copy `.env.example` to `.env` and fill in values as needed:

```powershell
copy .env.example .env
```

### Free AI Provider Options (Zero Required Spending)
The system works out-of-the-box with high-quality deterministic email generation templates if no API key is provided. To enable LLM-generated personalization, set one of the following free-tier keys:
- **GroqCloud (Recommended free-tier speed & limits)**: Set `GROQ_API_KEY` (uses `llama-3.3-70b-versatile` or `llama-3.1-8b-instant`).
- **Google Gemini**: Set `GEMINI_API_KEY` or `GOOGLE_API_KEY` (uses `gemini-1.5-flash`).
- **OpenRouter**: Set `OPENROUTER_API_KEY` (uses `meta-llama/llama-3.3-70b-instruct:free`).

Rate limiting, backoff, and prompt-injection sanitization are built-in.

## Safety Rules

- **No real emails** are ever sent in test or development mode.
- **Dry-run mode** is the default for outreach (future milestone).
- **Deduplication** prevents overwriting existing lead data.
- **Suppression list** is checked before every operation.
- **Audit log** records all data modifications.
- Credentials are never stored in code or committed to Git.

## License

Proprietary — NexAgentLab.tech
