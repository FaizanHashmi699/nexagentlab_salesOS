# NexAgentLab SalesOS

**Autonomous AI Sales System** for [NexAgentLab.tech](https://nexagentlab.tech)

> Local-first, privacy-respecting sales automation built with Python, SQLite, and AI agents.

## Status

🟢 **Milestone 1** — Lead import, validation, deduplication, and CLI  
🟢 **Milestone 2** — Research Agent & Opportunity Identification  
🟢 **Milestone 3** — Lead Qualification, Service Matching & Personalization  
⬜ Milestone 4 — Email integration (Gmail API)  
⬜ Milestone 5 — Reply management & sales pipeline  
⬜ Milestone 6 — Dashboard & campaign management  

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

### 7. Run Tests

```powershell
pytest tests/ -v
# Total tests: 114 passed
```

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
| `replies` | Classified inbound replies |
| `suppression` | Opt-out / bounce / invalid email blocklist |
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
