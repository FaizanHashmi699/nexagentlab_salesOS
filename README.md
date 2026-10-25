# NexAgentLab SalesOS

**Autonomous AI Sales System** for [NexAgentLab.tech](https://nexagentlab.tech)

> Local-first, privacy-respecting sales automation built with Python, SQLite, and AI agents.

## Status

🟢 **Milestone 1** — Lead import, validation, deduplication, and CLI  
⬜ Milestone 2 — Research & opportunity agents  
⬜ Milestone 3 — Personalization & quality agents  
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

### 4. Run Tests

```powershell
pytest tests/ -v
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

No API keys are required for Milestone 1 (lead import & management).

## Safety Rules

- **No real emails** are ever sent in test or development mode.
- **Dry-run mode** is the default for outreach (future milestone).
- **Deduplication** prevents overwriting existing lead data.
- **Suppression list** is checked before every operation.
- **Audit log** records all data modifications.
- Credentials are never stored in code or committed to Git.

## License

Proprietary — NexAgentLab.tech
