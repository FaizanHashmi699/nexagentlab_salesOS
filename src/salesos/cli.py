"""CLI interface for NexAgentLab SalesOS.

Provides commands for lead import, listing, deduplication inspection,
export, and database statistics.  Built with Click + Rich for a polished
terminal experience.
"""

from __future__ import annotations

import csv
import json
import os

import click
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .db import get_connection, get_db_path, init_db, transaction
from .importer import ImportResult, import_leads, preview_import
from .researcher import (
    extract_evidence,
    fetch_page,
    get_lead_research,
    get_research_status,
    identify_opportunities,
    retry_failed_research,
    run_research_batch,
)
from .ai_provider import get_provider_status
from .qualifier import (
    get_lead_qualification,
    get_qualification_status,
    qualify_lead,
    run_qualification_batch,
)
from .personalizer import (
    export_drafts,
    get_drafts,
    personalize_lead,
    run_personalization_batch,
)
from .gmail_client import (
    exchange_code_for_token,
    generate_auth_url,
    get_credentials_path,
    get_gmail_status,
    get_token_path,
    load_credentials_file,
    redact_secret,
    save_token_file,
    verify_sender_identity,
)
from .outreach import (
    approve_message,
    get_outreach_status,
    preview_message,
    queue_messages,
    send_batch,
    send_outreach_message,
    sync_drafts_to_gmail,
)
from .suppression import (
    get_suppression_stats,
    is_email_suppressed,
    list_suppressions,
    record_suppression,
)
from .reply_syncer import (
    get_last_checkpoint,
    sync_inbound_replies,
)
from .classifier import (
    CLASSIFICATION_CATEGORIES,
    classify_pending_replies,
    classify_reply,
)
from .followup_scheduler import (
    approve_followup,
    cancel_followup,
    get_due_followups,
    get_followups_status,
    plan_followups,
)
from .timeline import (
    get_lead_timeline,
)
from .pipeline import (
    ALL_STAGES,
    DealStage,
    InvalidTransitionError,
    SuppressedLeadError,
    create_deal,
    export_pipeline,
    get_deal,
    get_pipeline_report,
    list_deals,
    move_deal_stage,
    update_deal,
)
from .meetings import (
    BookingSource,
    DuplicateBookingError,
    MeetingStatus,
    cancel_meeting,
    complete_meeting,
    create_meeting,
    generate_meeting_brief,
    get_meeting,
    list_meetings,
    record_meeting_outcome,
    sync_calendly_events,
    update_meeting,
)
from .health_check import (
    CheckStatus,
    ReadinessLevel,
    run_all_health_checks,
)

# Force UTF-8 to avoid cp1252 encoding errors with Rich on Windows
import sys
if sys.platform == "win32":
    import io
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

console = Console(force_terminal=True)


def _get_conn():
    """Get a DB connection, auto-initializing if the DB doesn't exist."""
    db_path = get_db_path()
    if not os.path.exists(db_path):
        console.print("[yellow]Database not found. Initializing...[/yellow]")
        return init_db(db_path)
    return get_connection(db_path)


# ── CLI group ────────────────────────────────────────────────────────

@click.group()
@click.version_option(version="0.1.0", prog_name="SalesOS")
def cli():
    """NexAgentLab SalesOS — Lead Management CLI"""


# ── init ─────────────────────────────────────────────────────────────

@cli.command()
def init():
    """Initialize the database and data directory."""
    conn = init_db()
    console.print(
        Panel(
            "[green]✓ Database initialized successfully[/green]\n"
            f"[dim]Location: {get_db_path()}[/dim]",
            title="SalesOS Init",
            border_style="green",
        )
    )
    conn.close()


# ── import ───────────────────────────────────────────────────────────

@cli.command("import")
@click.argument("filepath", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Validate without importing")
@click.option("--source", default=None, help="Override source field for all leads")
def import_cmd(filepath: str, dry_run: bool, source: str | None):
    """Import leads from a CSV, XLSX, or JSON file."""
    conn = _get_conn()
    try:
        if dry_run:
            console.print(
                f"[yellow]DRY RUN[/yellow] — validating [bold]{filepath}[/bold]"
            )
        else:
            console.print(f"Importing from [bold]{filepath}[/bold]...")

        result = import_leads(
            filepath, conn, dry_run=dry_run, source_override=source,
        )
        _print_import_result(result, dry_run)
    finally:
        conn.close()


# ── preview ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("filepath", type=click.Path(exists=True))
def preview(filepath: str):
    """Preview a lead file without importing."""
    info = preview_import(filepath)

    console.print(
        Panel(
            f"[bold]File:[/bold] {info['file']}\n"
            f"[bold]Format:[/bold] {info['format']}\n"
            f"[bold]Total rows:[/bold] {info['total_rows']}\n"
            f"[bold]Valid:[/bold] {info['valid_rows']}  "
            f"[bold]Unique:[/bold] {info['unique_valid']}  "
            f"[bold]Batch dupes:[/bold] {info['batch_duplicates']}  "
            f"[bold]Errors:[/bold] {info['error_rows']}",
            title="Import Preview",
            border_style="cyan",
        )
    )

    if info["detected_mapping"]:
        table = Table(title="Detected Column Mapping", box=box.SIMPLE)
        table.add_column("Standard Field", style="green")
        table.add_column("File Column", style="cyan")
        for field, col in info["detected_mapping"].items():
            table.add_row(field, col)
        console.print(table)

    if info["sample_leads"]:
        table = Table(title="Sample Leads (first 5)", box=box.ROUNDED)
        table.add_column("Business", style="bold")
        table.add_column("Email", style="cyan")
        table.add_column("Contact")
        table.add_column("Location")
        table.add_column("Source")
        for lead in info["sample_leads"]:
            table.add_row(
                lead.get("business_name", ""),
                lead.get("email", ""),
                lead.get("contact_name", ""),
                lead.get("location", ""),
                lead.get("source", ""),
            )
        console.print(table)

    if info["errors"]:
        console.print(
            f"\n[red]Validation errors (showing first {len(info['errors'])}):[/red]"
        )
        for err in info["errors"]:
            console.print(f"  Row {err['row']}: {', '.join(err['errors'])}")


# ── list ─────────────────────────────────────────────────────────────

@cli.command("list")
@click.option("--status", default=None, help="Filter by lead status")
@click.option("--limit", default=50, help="Max rows to display")
@click.option("--search", default=None, help="Search by name or email")
def list_leads(status: str | None, limit: int, search: str | None):
    """List leads in the database."""
    conn = _get_conn()
    try:
        query = (
            "SELECT id, business_name, email, contact_name, "
            "location, source, status, created_at "
            "FROM leads WHERE 1=1"
        )
        params: list = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if search:
            query += " AND (business_name LIKE ? OR email LIKE ? OR contact_name LIKE ?)"
            params.extend([f"%{search}%"] * 3)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()

        if not rows:
            console.print("[yellow]No leads found.[/yellow]")
            return

        table = Table(title=f"Leads ({len(rows)} shown)", box=box.ROUNDED)
        table.add_column("ID", style="dim", width=5)
        table.add_column("Business", style="bold")
        table.add_column("Email", style="cyan")
        table.add_column("Contact")
        table.add_column("Location")
        table.add_column("Source")
        table.add_column("Status", style="green")

        for row in rows:
            table.add_row(
                str(row["id"]),
                row["business_name"],
                row["email"],
                row["contact_name"] or "",
                row["location"] or "",
                row["source"] or "",
                row["status"],
            )
        console.print(table)
    finally:
        conn.close()


# ── duplicates ───────────────────────────────────────────────────────

@cli.command()
def duplicates():
    """Show duplicate detection summary."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT email, COUNT(*) as cnt, "
            "GROUP_CONCAT(business_name, ' | ') as businesses "
            "FROM leads GROUP BY email HAVING cnt > 1"
        ).fetchall()

        if not rows:
            console.print("[green]✓ No duplicate emails found in database.[/green]")
            return

        table = Table(title="Duplicate Emails", box=box.ROUNDED)
        table.add_column("Email", style="cyan")
        table.add_column("Count", style="red")
        table.add_column("Businesses")

        for row in rows:
            table.add_row(row["email"], str(row["cnt"]), row["businesses"])
        console.print(table)
    finally:
        conn.close()


# ── export ───────────────────────────────────────────────────────────

@cli.command()
@click.argument("output", type=click.Path())
@click.option("--status", default=None, help="Filter by lead status")
@click.option(
    "--format", "fmt", default="csv",
    type=click.Choice(["csv", "json"]), help="Output format",
)
def export(output: str, status: str | None, fmt: str):
    """Export leads to CSV or JSON."""
    conn = _get_conn()
    try:
        query = "SELECT * FROM leads WHERE 1=1"
        params: list = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"

        rows = conn.execute(query, params).fetchall()

        if not rows:
            console.print("[yellow]No leads to export.[/yellow]")
            return

        columns = rows[0].keys()

        out_dir = os.path.dirname(os.path.abspath(output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if fmt == "csv":
            with open(output, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    writer.writerow(dict(row))
        else:
            with open(output, "w", encoding="utf-8") as f:
                json.dump([dict(row) for row in rows], f, indent=2)

        console.print(f"[green]✓ Exported {len(rows)} leads to {output}[/green]")
    finally:
        conn.close()


# ── stats ────────────────────────────────────────────────────────────

@cli.command()
def stats():
    """Show database statistics."""
    conn = _get_conn()
    try:
        lead_count = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        import_count = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
        suppressed = conn.execute("SELECT COUNT(*) FROM suppression").fetchone()[0]

        statuses = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM leads "
            "GROUP BY status ORDER BY cnt DESC"
        ).fetchall()

        console.print(
            Panel(
                f"[bold]Total Leads:[/bold] {lead_count}\n"
                f"[bold]Total Imports:[/bold] {import_count}\n"
                f"[bold]Suppressed:[/bold] {suppressed}",
                title="SalesOS Statistics",
                border_style="blue",
            )
        )

        if statuses:
            table = Table(title="Lead Status Breakdown", box=box.SIMPLE)
            table.add_column("Status", style="green")
            table.add_column("Count", style="bold", justify="right")
            for row in statuses:
                table.add_row(row["status"], str(row["cnt"]))
            console.print(table)

        recent = conn.execute(
            "SELECT id, filename, status, imported_count, error_count, "
            "duplicate_count, created_at "
            "FROM imports ORDER BY created_at DESC LIMIT 5"
        ).fetchall()

        if recent:
            table = Table(title="Recent Imports", box=box.ROUNDED)
            table.add_column("ID", style="dim")
            table.add_column("File", style="cyan")
            table.add_column("Status")
            table.add_column("Imported", justify="right", style="green")
            table.add_column("Errors", justify="right", style="red")
            table.add_column("Dupes", justify="right", style="yellow")
            table.add_column("Date")
            for row in recent:
                table.add_row(
                    str(row["id"]),
                    row["filename"],
                    row["status"],
                    str(row["imported_count"]),
                    str(row["error_count"]),
                    str(row["duplicate_count"]),
                    row["created_at"] or "",
                )
            console.print(table)
    finally:
        conn.close()


# ── Helpers ──────────────────────────────────────────────────────────

def _print_import_result(result: ImportResult, dry_run: bool = False) -> None:
    """Pretty-print import results."""
    prefix = "[DRY RUN] " if dry_run else ""
    status_color = "green" if result.errors == 0 else "yellow"

    console.print(
        Panel(
            f"[bold]Total rows:[/bold] {result.total_rows}\n"
            f"[bold green]Imported:[/bold green] {result.imported}\n"
            f"[bold yellow]Duplicates:[/bold yellow] {result.duplicates}\n"
            f"[bold red]Errors:[/bold red] {result.errors}\n"
            f"[bold]Skipped:[/bold] {result.skipped}",
            title=f"{prefix}Import Results",
            border_style=status_color,
        )
    )

    if result.duplicate_details:
        console.print(
            f"\n[yellow]Duplicates ({len(result.duplicate_details)}):[/yellow]"
        )
        for d in result.duplicate_details[:10]:
            console.print(
                f"  • {d['email']} ({d['business_name']}) — {d['reason']}"
            )

    if result.error_details:
        console.print(f"\n[red]Errors ({len(result.error_details)}):[/red]")
        for e in result.error_details[:10]:
            console.print(f"  • Row {e['row']}: {', '.join(e['errors'])}")


# ── Research subcommands ─────────────────────────────────────────────

@cli.group("research")
def research_group():
    """Website research and opportunity identification commands."""


@research_group.command("preview")
@click.argument("target")
def research_preview_cmd(target: str):
    """Preview research extraction on a URL or lead ID without saving.

    TARGET can be a URL (e.g. https://example.com) or an integer Lead ID.
    """
    website_url = None
    biz_name = "Target Website"

    if target.isdigit():
        conn = _get_conn()
        try:
            lead = conn.execute(
                "SELECT id, business_name, website FROM leads WHERE id = ?",
                (int(target),),
            ).fetchone()
            if not lead:
                console.print(f"[red]Lead ID {target} not found.[/red]")
                return
            if not lead["website"]:
                console.print(f"[yellow]Lead #{target} ({lead['business_name']}) has no website.[/yellow]")
                return
            website_url = lead["website"]
            biz_name = lead["business_name"]
        finally:
            conn.close()
    else:
        website_url = target
        if not website_url.startswith(("http://", "https://")):
            website_url = "https://" + website_url

    console.print(f"[cyan]Fetching and analyzing [bold]{website_url}[/bold]...[/cyan]")
    html, final_url, status_code, err = fetch_page(website_url)

    if err or not html:
        console.print(f"[red]Failed to fetch website:[/red] {err}")
        return

    evidence = extract_evidence(html, final_url or website_url)
    opportunities = identify_opportunities(evidence)

    # Display evidence panel
    console.print(
        Panel(
            f"[bold]Target:[/bold] {biz_name}\n"
            f"[bold]URL:[/bold] {final_url or website_url}\n"
            f"[bold]Title:[/bold] {evidence.title or '[dim]None[/dim]'}\n"
            f"[bold]Meta Description:[/bold] {evidence.meta_description or '[dim]None[/dim]'}\n"
            f"[bold]Headings Found:[/bold] {len(evidence.headings)}\n"
            f"[bold]Lead Form:[/bold] {'[green]Yes[/green]' if evidence.has_lead_form else '[yellow]No[/yellow]'}  "
            f"[bold]Booking System:[/bold] {'[green]Yes[/green]' if evidence.has_booking else '[yellow]No[/yellow]'}  "
            f"[bold]WhatsApp:[/bold] {'[green]Yes[/green]' if evidence.has_whatsapp else '[yellow]No[/yellow]'}  "
            f"[bold]Chat Widget:[/bold] {'[green]Yes[/green]' if evidence.has_chat_widget else '[yellow]No[/yellow]'}\n"
            f"[bold]Technologies:[/bold] {', '.join(evidence.technologies_detected) or '[dim]None detected[/dim]'}",
            title="Website Evidence Preview",
            border_style="cyan",
        )
    )

    if opportunities:
        table = Table(title=f"Identified Opportunities ({len(opportunities)})", box=box.ROUNDED)
        table.add_column("Category", style="cyan")
        table.add_column("Confidence", justify="right")
        table.add_column("Label")
        table.add_column("Priority", justify="right", style="bold")
        table.add_column("Description")

        for opp in opportunities:
            conf_style = "green" if opp.confidence >= 0.8 else "yellow" if opp.confidence >= 0.6 else "dim"
            table.add_row(
                opp.category,
                f"[{conf_style}]{opp.confidence:.2f}[/{conf_style}]",
                f"[{conf_style}]{opp.confidence_label}[/{conf_style}]",
                str(opp.priority),
                opp.description,
            )
        console.print(table)
    else:
        console.print("[green]No high-confidence opportunity gaps found.[/green]")


@research_group.command("run")
@click.option("--limit", default=50, help="Max leads to research in this batch")
@click.option("--lead-id", type=int, default=None, help="Research a specific lead ID")
@click.option("--force", is_flag=True, help="Re-run research even if already completed")
def research_run_cmd(limit: int, lead_id: int | None, force: bool):
    """Run research on eligible leads."""
    conn = _get_conn()
    try:
        lead_ids = [lead_id] if lead_id else None
        if lead_id:
            console.print(f"Researching lead [bold]#{lead_id}[/bold]...")
        else:
            console.print(f"Starting research batch (limit={limit}, force={force})...")

        summary = run_research_batch(conn, limit=limit, lead_ids=lead_ids, force=force)

        console.print(
            Panel(
                f"[bold]Total processed:[/bold] {summary.total}\n"
                f"[bold green]Completed:[/bold green] {summary.completed}\n"
                f"[bold cyan]No Website:[/bold cyan] {summary.no_website}\n"
                f"[bold red]Failed:[/bold red] {summary.failed}\n"
                f"[bold yellow]Skipped:[/bold yellow] {summary.skipped}\n"
                f"[bold]Duration:[/bold] {summary.duration_seconds}s",
                title="Research Run Summary",
                border_style="green" if summary.failed == 0 else "yellow",
            )
        )
    finally:
        conn.close()


@research_group.command("status")
def research_status_cmd():
    """Show aggregate research status and AI provider information."""
    conn = _get_conn()
    try:
        counts = get_research_status(conn)
        total_leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        researched_leads = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) FROM research WHERE status = 'completed'"
        ).fetchone()[0]
        opp_count = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]

        ai_status = get_provider_status()

        console.print(
            Panel(
                f"[bold]Total Leads:[/bold] {total_leads}\n"
                f"[bold green]Successfully Researched:[/bold green] {researched_leads}\n"
                f"[bold]Total Opportunities Identified:[/bold] {opp_count}\n"
                f"[bold]AI Provider:[/bold] {ai_status.provider} "
                f"({'[green]Available: ' + str(ai_status.model) + '[/green]' if ai_status.available else '[yellow]Unavailable (' + str(ai_status.reason) + ')[/yellow]'})\n",
                title="Research Status",
                border_style="blue",
            )
        )

        if counts:
            table = Table(title="Research Status Breakdown", box=box.SIMPLE)
            table.add_column("Status", style="cyan")
            table.add_column("Count", style="bold", justify="right")
            for status, cnt in counts.items():
                table.add_row(status, str(cnt))
            console.print(table)
    finally:
        conn.close()


@research_group.command("retry")
@click.option("--limit", default=20, help="Max failed leads to retry")
def research_retry_cmd(limit: int):
    """Retry research on leads that previously failed."""
    conn = _get_conn()
    try:
        console.print(f"Retrying failed research (limit={limit})...")
        summary = retry_failed_research(conn, limit=limit)

        console.print(
            Panel(
                f"[bold]Retried:[/bold] {summary.total}\n"
                f"[bold green]Succeeded:[/bold green] {summary.completed}\n"
                f"[bold red]Still Failed:[/bold red] {summary.failed}\n"
                f"[bold]Duration:[/bold] {summary.duration_seconds}s",
                title="Research Retry Summary",
                border_style="green" if summary.failed == 0 else "yellow",
            )
        )
    finally:
        conn.close()


@research_group.command("export")
@click.argument("output", type=click.Path())
@click.option("--format", "fmt", default="csv", type=click.Choice(["csv", "json"]), help="Output format")
@click.option("--lead-id", type=int, default=None, help="Export a specific lead ID")
def research_export_cmd(output: str, fmt: str, lead_id: int | None):
    """Export research findings and opportunities to CSV or JSON."""
    conn = _get_conn()
    try:
        query = (
            "SELECT l.id as lead_id, l.business_name, l.email, l.website, l.location, "
            "r.status as research_status, r.confidence as research_confidence, "
            "r.findings as research_findings, r.created_at as researched_at "
            "FROM leads l "
            "LEFT JOIN research r ON r.lead_id = l.id AND r.agent_type = 'website_researcher' "
            "WHERE 1=1"
        )
        params: list = []
        if lead_id:
            query += " AND l.id = ?"
            params.append(lead_id)

        rows = conn.execute(query, params).fetchall()
        if not rows:
            console.print("[yellow]No research records found to export.[/yellow]")
            return

        exported_data = []
        for row in rows:
            record = dict(row)
            opps = conn.execute(
                "SELECT category, description, confidence, priority, evidence FROM opportunities WHERE lead_id = ?",
                (row["lead_id"],),
            ).fetchall()
            record["opportunities"] = [
                {
                    "category": o["category"],
                    "description": o["description"],
                    "confidence": o["confidence"],
                    "priority": o["priority"],
                    "evidence": json.loads(o["evidence"]) if o["evidence"] else [],
                }
                for o in opps
            ]
            exported_data.append(record)

        out_dir = os.path.dirname(os.path.abspath(output))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if fmt == "json":
            with open(output, "w", encoding="utf-8") as f:
                json.dump(exported_data, f, indent=2)
        else:
            # Flatten for CSV
            with open(output, "w", newline="", encoding="utf-8") as f:
                fieldnames = [
                    "lead_id", "business_name", "email", "website", "location",
                    "research_status", "research_confidence", "opportunity_count",
                    "top_opportunities", "researched_at",
                ]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for item in exported_data:
                    top_opps_str = "; ".join(
                        f"{o['category']}({o['confidence']:.2f})"
                        for o in item["opportunities"][:3]
                    )
                    writer.writerow({
                        "lead_id": item["lead_id"],
                        "business_name": item["business_name"],
                        "email": item["email"],
                        "website": item["website"],
                        "location": item["location"],
                        "research_status": item["research_status"],
                        "research_confidence": item["research_confidence"],
                        "opportunity_count": len(item["opportunities"]),
                        "top_opportunities": top_opps_str,
                        "researched_at": item["researched_at"],
                    })

        console.print(f"[green]✓ Exported {len(exported_data)} research records to {output}[/green]")
    finally:
        conn.close()


# ── Qualification subcommands ────────────────────────────────────────

@cli.group("qualify")
def qualify_group():
    """Lead qualification and fit scoring commands."""


@qualify_group.command("run")
@click.option("--limit", default=50, help="Max leads to qualify in this batch")
@click.option("--force", is_flag=True, help="Re-qualify leads even if already scored")
def qualify_run_cmd(limit: int, force: bool):
    """Run qualification and scoring on eligible leads."""
    conn = _get_conn()
    try:
        console.print(f"Starting lead qualification batch (limit={limit}, force={force})...")
        summary = run_qualification_batch(conn, limit=limit, force=force)

        console.print(
            Panel(
                f"[bold]Total processed:[/bold] {summary.total}\n"
                f"[bold green]Qualified (score >= 45):[/bold green] {summary.qualified}\n"
                f"[bold yellow]Unqualified (score < 45):[/bold yellow] {summary.unqualified}\n"
                f"[bold red]Suppressed / Opt-out:[/bold red] {summary.suppressed}\n"
                f"[bold]Skipped on error:[/bold] {summary.skipped}\n"
                f"[bold]Duration:[/bold] {summary.duration_seconds}s",
                title="Lead Qualification Summary",
                border_style="green" if summary.qualified > 0 else "yellow",
            )
        )
    finally:
        conn.close()


@qualify_group.command("show")
@click.argument("lead_id", type=int)
def qualify_show_cmd(lead_id: int):
    """Show detailed qualification breakdown and explanation for a lead."""
    conn = _get_conn()
    try:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if not lead:
            console.print(f"[red]Lead ID #{lead_id} not found.[/red]")
            return

        # Calculate or fetch qualification
        res = qualify_lead(lead, conn)

        score_color = "green" if res.score >= 70 else "yellow" if res.score >= 45 else "red"
        console.print(
            Panel(
                f"[bold]Lead:[/bold] #{res.lead_id} — {res.business_name} ({res.email})\n"
                f"[bold]Score:[/bold] [{score_color}]{res.score:.1f} / 100[/{score_color}] "
                f"([bold {score_color}]{res.priority.upper()}[/bold {score_color}])\n"
                f"[bold]Eligible for Outreach:[/bold] {'[green]Yes[/green]' if res.eligible else '[red]No[/red]'}\n"
                f"[bold]Confidence:[/bold] {res.confidence * 100:.0f}%\n"
                f"[bold]Recommended Action:[/bold] {res.recommended_action}\n"
                f"[bold]Scoring Version:[/bold] {res.scoring_version}",
                title=f"Qualification: {res.business_name}",
                border_style=score_color,
            )
        )

        if res.explanation:
            table = Table(title="Scoring Breakdown & Audit Trail", box=box.SIMPLE)
            table.add_column("Explanation / Point Allocation", style="cyan")
            for item in res.explanation:
                style = "green" if item.startswith("+") and not item.startswith("+0") else "red" if item.startswith("-") else "dim"
                table.add_row(f"[{style}]{item}[/{style}]")
            console.print(table)
    finally:
        conn.close()


# ── Personalization subcommands ──────────────────────────────────────

@cli.group("personalize")
def personalize_group():
    """Personalized email generation commands."""


@personalize_group.command("run")
@click.option("--limit", default=20, help="Max leads to draft emails for")
@click.option("--force", is_flag=True, help="Re-generate drafts for leads with existing messages")
@click.option("--use-ai/--no-ai", default=True, help="Enable/disable hosted AI provider for drafting")
def personalize_run_cmd(limit: int, force: bool, use_ai: bool):
    """Generate personalized outreach drafts for qualified leads."""
    conn = _get_conn()
    try:
        provider_info = get_provider_status() if use_ai else None
        provider_label = f"{provider_info.provider} ({provider_info.model})" if (provider_info and provider_info.available) else "template engine"
        console.print(f"Starting email personalization (limit={limit}, force={force}, engine={provider_label})...")

        summary = run_personalization_batch(conn, limit=limit, use_ai=use_ai, force=force)

        console.print(
            Panel(
                f"[bold]Total processed:[/bold] {summary.total}\n"
                f"[bold green]Approved by Quality Gate:[/bold green] {summary.approved}\n"
                f"[bold red]Rejected by Quality Gate:[/bold red] {summary.rejected}\n"
                f"[bold]Skipped / Existed:[/bold] {summary.skipped}\n"
                f"[bold]Duration:[/bold] {summary.duration_seconds}s",
                title="Personalization Run Summary",
                border_style="green" if summary.approved > 0 else "yellow",
            )
        )
    finally:
        conn.close()


# ── Drafts subcommands ───────────────────────────────────────────────

@cli.group("drafts")
def drafts_group():
    """Draft message inspection and export commands."""


@drafts_group.command("list")
@click.option("--status", "status_filter", default="all", type=click.Choice(["all", "approved", "rejected", "pending"]), help="Filter by quality status")
@click.option("--limit", default=50, help="Max drafts to show")
def drafts_list_cmd(status_filter: str, limit: int):
    """List generated email drafts."""
    conn = _get_conn()
    try:
        drafts = get_drafts(conn, status_filter=status_filter, limit=limit)
        if not drafts:
            console.print("[yellow]No drafts found matching criteria.[/yellow]")
            return

        table = Table(title=f"Email Drafts ({len(drafts)} shown)", box=box.ROUNDED)
        table.add_column("ID", style="dim", width=4)
        table.add_column("Lead", style="bold")
        table.add_column("Recipient", style="cyan")
        table.add_column("Subject")
        table.add_column("Service", style="green")
        table.add_column("Engine")
        table.add_column("Quality", justify="center")

        for d in drafts:
            q_style = "green" if d.get("quality_status") == "approved" else "red"
            table.add_row(
                str(d["id"]),
                d.get("business_name", ""),
                d.get("recipient", ""),
                (d.get("subject", "") or "")[:40] + ("..." if len(d.get("subject", "") or "") > 40 else ""),
                d.get("selected_service", ""),
                d.get("provider", "template"),
                f"[{q_style}]{d.get('quality_status', 'draft')}[/{q_style}]",
            )
        console.print(table)
    finally:
        conn.close()


@drafts_group.command("show")
@click.argument("draft_id", type=int)
def drafts_show_cmd(draft_id: int):
    """Display full email draft contents and quality inspection report."""
    conn = _get_conn()
    try:
        # Check by draft ID or lead ID
        row = conn.execute(
            """SELECT m.*, l.business_name, l.contact_name, l.website, l.location
               FROM messages m
               JOIN leads l ON l.id = m.lead_id
               WHERE m.id = ? OR m.lead_id = ?
               ORDER BY m.id DESC LIMIT 1""",
            (draft_id, draft_id),
        ).fetchone()

        if not row:
            console.print(f"[red]No draft found with ID or Lead ID #{draft_id}.[/red]")
            return

        d = dict(row)
        q_status = d.get("quality_status", "pending")
        border = "green" if q_status == "approved" else "red"

        console.print(
            Panel(
                f"[bold]Draft ID:[/bold] {d['id']}  |  [bold]Lead ID:[/bold] {d['lead_id']}\n"
                f"[bold]Business:[/bold] {d['business_name']} ({d['website'] or 'No website'})\n"
                f"[bold]To:[/bold] {d.get('recipient', '')}\n"
                f"[bold]Service Focus:[/bold] {d.get('selected_service', '')} (Score: {d.get('qualification_score', 0):.1f})\n"
                f"[bold]Generator Engine:[/bold] {d.get('provider', 'template')} ({d.get('model') or 'rules'})\n"
                f"[bold]Quality Status:[/bold] [{border}]{q_status.upper()}[/{border}]\n"
                f"[dim]Created: {d.get('created_at', '')}[/dim]",
                title="Email Draft Metadata",
                border_style=border,
            )
        )

        console.print(
            Panel(
                f"[bold]Subject:[/bold] {d.get('subject', '')}\n\n"
                f"{d.get('body', '')}",
                title="Email Message Preview",
                border_style="cyan",
            )
        )

        errors = json.loads(d["validation_errors"]) if d.get("validation_errors") else []
        if errors:
            console.print("[red]Quality Gate Rejection Reasons:[/red]")
            for err in errors:
                console.print(f"  • [red]{err}[/red]")
        elif q_status == "approved":
            console.print("[green]✓ Draft passed all Quality Gate checks and is ready for outreach.[/green]")
    finally:
        conn.close()


@drafts_group.command("export")
@click.argument("output", type=click.Path())
@click.option("--format", "fmt", default="csv", type=click.Choice(["csv", "json"]), help="Output format")
@click.option("--status", "status_filter", default="all", type=click.Choice(["all", "approved", "rejected"]), help="Filter by quality status")
def drafts_export_cmd(output: str, fmt: str, status_filter: str):
    """Export generated drafts to a CSV or JSON file."""
    conn = _get_conn()
    try:
        count = export_drafts(conn, output_path=output, fmt=fmt, status_filter=status_filter)
        if count == 0:
            console.print("[yellow]No drafts found matching the export filter.[/yellow]")
        else:
            console.print(f"[green]✓ Exported {count} drafts to {output}[/green]")
    finally:
        conn.close()


# ── Gmail Group ──────────────────────────────────────────────────────

@cli.group("gmail")
def gmail_group():
    """Gmail OAuth & account configuration management."""


@gmail_group.command("status")
def gmail_status_cmd():
    """Check Gmail OAuth credentials, token status, and sender identity."""
    status = get_gmail_status()

    table = Table(title="Gmail Integration Status", box=box.ROUNDED)
    table.add_column("Property", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")

    table.add_row(
        "Credentials File",
        f"[green]✓ Found[/green] ({status.credentials_path})"
        if status.credentials_found
        else f"[yellow]✗ Missing[/yellow] ({status.credentials_path})",
    )
    table.add_row(
        "Token File",
        f"[green]✓ Found[/green] ({status.token_path})"
        if status.token_found
        else f"[yellow]✗ Not Generated[/yellow] ({status.token_path})",
    )
    table.add_row(
        "Token State",
        "[green]Valid & Active[/green]"
        if (status.token_valid and not status.token_expired)
        else ("[yellow]Expired (Refreshable)[/yellow]" if status.can_refresh else "[red]Missing or Invalid[/red]"),
    )
    table.add_row("Configured Agency Sender", f"[bold]{status.configured_sender}[/bold]")
    table.add_row(
        "Authenticated Account",
        f"[green]{status.authenticated_email}[/green]"
        if status.authenticated_email
        else "[dim]Not Authenticated[/dim]",
    )
    table.add_row(
        "Sender Identity Check",
        "[green]✓ Verified (Match)[/green]"
        if status.identity_matched
        else ("[red]✗ Mismatch![/red]" if status.authenticated_email else "[yellow]Unverified[/yellow]"),
    )
    table.add_row(
        "Granted Scopes",
        "\n".join(status.granted_scopes) if status.granted_scopes else "[dim]None[/dim]",
    )
    table.add_row(
        "Outreach Dispatch Mode",
        "[yellow]DRY RUN (Safe - No Real Emails)[/yellow]"
        if status.dry_run
        else "[bold red]LIVE DISPATCH (Real Emails)[/bold red]",
    )
    table.add_row(
        "Campaign Kill Switch",
        "[bold red]ACTIVE (Halted)[/bold red]"
        if status.kill_switch_active
        else "[green]Inactive (Normal Operation)[/green]",
    )

    console.print(table)
    if status.error:
        console.print(f"\n[yellow]Notice:[/yellow] {status.error}")


@gmail_group.command("auth")
@click.option("--code", help="Authorization code returned from Google consent screen")
@click.option("--credentials", "creds_file", type=click.Path(exists=True), help="Path to credentials.json")
def gmail_auth_cmd(code: str | None, creds_file: str | None):
    """Authenticate with Google OAuth 2.0 for Gmail access."""
    from pathlib import Path
    creds_p = Path(creds_file).resolve() if creds_file else get_credentials_path()

    if not creds_p.exists():
        console.print(
            Panel(
                "[bold yellow]Google OAuth credentials.json Not Found[/bold yellow]\n\n"
                "To authenticate Gmail for NexAgentLab SalesOS:\n"
                "1. Go to Google Cloud Console: [cyan]https://console.cloud.google.com[/cyan]\n"
                "2. Create or select a project for NexAgentLab.\n"
                "3. Enable the [bold]Gmail API[/bold] under APIs & Services.\n"
                "4. Go to [bold]APIs & Services > OAuth consent screen[/bold], set User Type to External,\n"
                "   and add test user: [bold]nexagentlab.tech@gmail.com[/bold].\n"
                "5. Go to [bold]Credentials > Create Credentials > OAuth client ID[/bold].\n"
                "6. Select Application type: [bold]Desktop app[/bold].\n"
                "7. Download the JSON file and save it as [bold]credentials.json[/bold] in the project root.\n\n"
                f"[dim]Expected location: {creds_p}[/dim]",
                title="Gmail OAuth Setup Guide",
                border_style="yellow",
            )
        )
        return

    try:
        client_info = load_credentials_file(creds_p)
    except Exception as e:
        console.print(f"[red]Error loading credentials file: {e}[/red]")
        return

    if not code:
        auth_url = generate_auth_url(client_info)
        console.print(
            Panel(
                "[bold green]Google OAuth Authorization Required[/bold green]\n\n"
                "1. Open this URL in your web browser:\n\n"
                f"[cyan underline]{auth_url}[/cyan underline]\n\n"
                "2. Sign in with [bold]nexagentlab.tech@gmail.com[/bold] and grant permissions.\n"
                "3. Copy the authorization code provided after sign-in.\n"
                "4. Run:\n\n"
                "   [bold]salesos gmail auth --code YOUR_CODE_HERE[/bold]",
                title="Google OAuth Step 1",
                border_style="green",
            )
        )
        return

    # Exchange code for token
    console.print("[dim]Exchanging authorization code for OAuth tokens...[/dim]")
    try:
        token_data = exchange_code_for_token(client_info, code)
        token_path = save_token_file(token_data)

        # Check sender profile identity
        auth_email = verify_sender_identity(token_data.get("access_token"))
        redacted_access = redact_secret(token_data.get("access_token"))

        console.print(
            Panel(
                f"[green]✓ Successfully authenticated with Gmail![/green]\n\n"
                f"[bold]Account:[/bold] {auth_email}\n"
                f"[bold]Token Path:[/bold] {token_path}\n"
                f"[bold]Access Token:[/bold] {redacted_access}\n"
                f"[bold]Refresh Token:[/bold] [green]Stored securely[/green]",
                title="Authentication Successful",
                border_style="green",
            )
        )
    except Exception as e:
        console.print(f"[red]Authentication failed: {e}[/red]")


# ── Outreach Group ───────────────────────────────────────────────────

@cli.group("outreach")
def outreach_group():
    """Controlled outreach dispatch, approval, and suppression."""


@outreach_group.command("preview")
@click.argument("message_id", type=int)
def outreach_preview_cmd(message_id: int):
    """Preview an outreach message and run pre-flight safety checklist."""
    conn = _get_conn()
    try:
        p = preview_message(conn, message_id)

        border = "green" if p["status"] in ("approved", "queued") else "yellow"
        console.print(
            Panel(
                f"[bold]Message ID:[/bold] {p['message_id']}  |  [bold]Lead ID:[/bold] {p['lead_id']}\n"
                f"[bold]Business:[/bold] {p['business_name']} ({p['website'] or 'No website'})\n"
                f"[bold]To:[/bold] {p['recipient']}\n"
                f"[bold]Service Focus:[/bold] {p['selected_service']} (Score: {p['qualification_score'] or 0:.1f})\n"
                f"[bold]Status:[/bold] [{border}]{p['status'].upper()}[/{border}]  |  "
                f"[bold]Quality Gate:[/bold] {p['quality_status']}\n"
                f"[dim]Gmail Draft ID: {p.get('gmail_draft_id') or 'Not synced'} | Sent: {p.get('sent_at') or 'No'}[/dim]",
                title="Outreach Draft Metadata",
                border_style=border,
            )
        )

        console.print(
            Panel(
                f"[bold]Subject:[/bold] {p['subject']}\n\n"
                f"{p['body']}",
                title="Email Message Preview",
                border_style="cyan",
            )
        )

        # Pre-flight checklist table
        chk = p["checks"]
        tbl = Table(title="Pre-Flight Safety Checklist", box=box.ROUNDED)
        tbl.add_column("Safety Gate Check", style="cyan")
        tbl.add_column("Status", style="white")

        tbl.add_row("Approved by User", "[green]✓ Ready[/green]" if chk["is_approved"] else "[yellow]✗ Needs Approval[/yellow]")
        tbl.add_row("Quality Gate Audit", "[green]✓ Passed[/green]" if chk["quality_gate_passed"] else "[red]✗ Failed[/red]")
        tbl.add_row(
            "Suppression List",
            "[green]✓ Clean[/green]"
            if chk["recipient_not_suppressed"]
            else f"[red]✗ Suppressed ({p['suppression_reason']})[/red]",
        )
        tbl.add_row("Duplicate Send Prevention", "[green]✓ Safe[/green]" if chk["no_duplicate_sent"] else "[red]✗ Recipient Already Emailed[/red]")
        tbl.add_row(
            "Daily Quota Allocation",
            f"[green]✓ Available[/green] ({p['daily_sends_today']} / {p['daily_cap']} used)",
        )
        tbl.add_row("Emergency Kill Switch", "[green]✓ Inactive[/green]" if chk["kill_switch_inactive"] else "[red]✗ ACTIVE[/red]")
        tbl.add_row(
            "Dispatch Mode",
            "[yellow]DRY RUN (Simulated)[/yellow]" if chk["dry_run_mode"] else "[red]LIVE GMAIL[/red]",
        )

        console.print(tbl)
        if p["ready_to_send"]:
            console.print("\n[green]✓ Message passes all safety checks and is ready to dispatch.[/green]")
        else:
            console.print("\n[yellow]Notice: Message is NOT yet ready for dispatch. Resolve outstanding checks first.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error previewing message: {e}[/red]")
    finally:
        conn.close()


@outreach_group.command("approve")
@click.argument("message_id", type=int)
@click.option("--force", is_flag=True, help="Force approval even if Quality Gate noted review warnings")
def outreach_approve_cmd(message_id: int, force: bool):
    """Approve a draft message for outreach sending."""
    conn = _get_conn()
    try:
        ok, msg = approve_message(conn, message_id, force=force)
        if ok:
            console.print(f"[green]✓ {msg}[/green]")
        else:
            console.print(f"[red]✗ {msg}[/red]")
    finally:
        conn.close()


@outreach_group.command("queue")
@click.option("--limit", type=int, default=None, help="Maximum approved messages to queue")
@click.option("--service", type=str, default=None, help="Filter by service category")
def outreach_queue_cmd(limit: int | None, service: str | None):
    """Move approved messages into the outbound dispatch queue."""
    conn = _get_conn()
    try:
        count = queue_messages(conn, limit=limit, service=service)
        if count == 0:
            console.print("[yellow]No approved messages found eligible for queueing.[/yellow]")
            console.print("[dim]Run 'salesos outreach approve <ID>' first.[/dim]")
        else:
            console.print(f"[green]✓ Successfully moved {count} approved messages into the outreach queue.[/green]")
    finally:
        conn.close()


@outreach_group.command("sync-drafts")
@click.option("--message-id", type=int, default=None, help="Specific message ID to sync")
@click.option("--live", is_flag=True, help="Sync directly to Gmail via Gmail API (default is dry-run)")
def outreach_sync_drafts_cmd(message_id: int | None, live: bool):
    """Synchronize queued drafts into Gmail Drafts for web review."""
    conn = _get_conn()
    try:
        mids = [message_id] if message_id else None
        res = sync_drafts_to_gmail(conn, message_ids=mids, dry_run=not live)

        mode_str = "[red]LIVE GMAIL[/red]" if live else "[yellow]DRY RUN[/yellow]"
        console.print(
            Panel(
                f"[bold]Mode:[/bold] {mode_str}\n"
                f"[bold]Evaluated:[/bold] {res['total_evaluated']}\n"
                f"[bold]Successfully Synced:[/bold] [green]{res['synced']}[/green]\n"
                f"[bold]Skipped (Suppressed):[/bold] {res['skipped']}",
                title="Draft Synchronization Results",
                border_style="green" if not res["errors"] else "yellow",
            )
        )
        if res["errors"]:
            console.print("[red]Errors encountered:[/red]")
            for err in res["errors"]:
                console.print(f"  • {err}")
    finally:
        conn.close()


@outreach_group.command("send")
@click.argument("message_id", type=int, required=False)
@click.option("--all-queued", is_flag=True, help="Send all currently queued messages in batch")
@click.option("--limit", type=int, default=None, help="Batch size limit when sending queued")
@click.option("--live", is_flag=True, help="Execute live send via Gmail API (default is dry-run)")
@click.option("--force", is_flag=True, help="Bypass non-critical warnings")
def outreach_send_cmd(
    message_id: int | None,
    all_queued: bool,
    limit: int | None,
    live: bool,
    force: bool,
):
    """Send approved outreach messages with full safety and compliance checks."""
    if not message_id and not all_queued:
        console.print("[yellow]Please specify a MESSAGE_ID to send or use --all-queued to send the queue.[/yellow]")
        return

    conn = _get_conn()
    try:
        # Batch send
        if all_queued:
            res = send_batch(conn, limit=limit, dry_run=not live)
            mode_str = "[red]LIVE GMAIL[/red]" if live else "[yellow]DRY RUN[/yellow]"
            console.print(
                Panel(
                    f"[bold]Mode:[/bold] {mode_str}\n"
                    f"[bold]Queued Found:[/bold] {res['total_queued']}\n"
                    f"[bold]Sent Successfully:[/bold] [green]{res['sent']}[/green]\n"
                    f"[bold]Skipped/Failed:[/bold] {res['skipped']}",
                    title="Outreach Batch Send Results",
                    border_style="green" if not res["errors"] else "yellow",
                )
            )
            if res["errors"]:
                for err in res["errors"]:
                    console.print(f"  • [yellow]{err}[/yellow]")
            return

        # Single message send
        res = send_outreach_message(conn, message_id=message_id, dry_run=not live, force=force)
        mode_str = "[red]LIVE GMAIL[/red]" if live else "[yellow]DRY RUN[/yellow]"
        console.print(
            Panel(
                f"[green]✓ Outreach message dispatched successfully[/green]\n\n"
                f"[bold]Message ID:[/bold] {res['message_id']}\n"
                f"[bold]Recipient:[/bold] {res['recipient']}\n"
                f"[bold]Mode:[/bold] {mode_str}\n"
                f"[bold]Gmail Message ID:[/bold] {res.get('gmail_message_id')}\n"
                f"[bold]Daily Sends Used:[/bold] {res['daily_sends_today']}",
                title="Dispatch Confirmation",
                border_style="green",
            )
        )
    except Exception as e:
        console.print(f"[red]Dispatch blocked: {e}[/red]")
    finally:
        conn.close()


@outreach_group.command("suppress")
@click.argument("email", type=str)
@click.option(
    "--reason",
    default="manual",
    type=click.Choice(["opt_out", "unsubscribe", "bounce", "complaint", "manual", "invalid"]),
    help="Reason for suppression",
)
@click.option("--source", default="cli", help="Source recording the suppression")
def outreach_suppress_cmd(email: str, reason: str, source: str):
    """Add an email to the suppression list and cancel any pending messages."""
    conn = _get_conn()
    try:
        res = record_suppression(conn, email=email, reason=reason, source=source)
        console.print(
            Panel(
                f"[green]✓ Email successfully added to suppression list[/green]\n\n"
                f"[bold]Normalized Email:[/bold] {res['email']}\n"
                f"[bold]Reason:[/bold] {res['reason']}\n"
                f"[bold]Source:[/bold] {res['source']}\n"
                f"[bold]Cancelled Pending Messages:[/bold] [yellow]{res['cancelled_messages']}[/yellow]",
                title="Suppression Recorded",
                border_style="green",
            )
        )
    except Exception as e:
        console.print(f"[red]Failed to suppress email: {e}[/red]")
    finally:
        conn.close()


@outreach_group.command("status")
def outreach_status_cmd():
    """Display outreach campaign metrics, queue status, and safety limits."""
    conn = _get_conn()
    try:
        s = get_outreach_status(conn)

        table = Table(title="NexAgentLab Outreach Campaign Overview", box=box.ROUNDED)
        table.add_column("Metric / Status", style="cyan")
        table.add_column("Count / Setting", style="white")

        msg_counts = s["messages_by_status"]
        table.add_row("Drafts in Review", str(msg_counts.get("draft", 0)))
        table.add_row("Approved Messages", f"[green]{msg_counts.get('approved', 0)}[/green]")
        table.add_row("Messages in Outbox Queue", f"[bold cyan]{msg_counts.get('queued', 0)}[/bold cyan]")
        table.add_row("Successfully Sent", f"[bold green]{msg_counts.get('sent', 0)}[/bold green]")
        table.add_row("Cancelled / Suppressed", str(msg_counts.get("cancelled", 0)))
        table.add_row("Failed Dispatches", f"[red]{msg_counts.get('failed', 0)}[/red]" if msg_counts.get("failed") else "0")

        table.add_section()
        table.add_row(
            "Today's Sent Volume",
            f"{s['daily_sends_today']} / {s['daily_cap']} (Quota remaining: {s['quota_remaining']})",
        )
        table.add_row(
            "Dispatch Mode",
            "[yellow]DRY RUN (Safe - Simulation Only)[/yellow]"
            if s["dry_run"]
            else "[bold red]LIVE GMAIL[/bold red]",
        )
        table.add_row(
            "Kill Switch",
            "[bold red]ACTIVE (All outreach blocked)[/bold red]"
            if s["kill_switch_active"]
            else "[green]Inactive (Normal)[/green]",
        )
        table.add_row("Suppression List Size", f"{s['total_suppressed']} addresses")
        table.add_row("Configured Agency Sender", s["configured_sender"])

        console.print(table)
    finally:
        conn.close()


# ── Replies Group ────────────────────────────────────────────────────

@cli.group("replies")
def replies_group():
    """Inbound reply monitoring, classification & response management."""


@replies_group.command("sync")
@click.option("--dry-run", is_flag=True, help="Preview messages that would be synced without saving")
@click.option("--limit", type=int, default=50, help="Maximum messages to evaluate")
@click.option("--force-recovery", is_flag=True, help="Ignore history checkpoint and perform full recovery sync")
def replies_sync_cmd(dry_run: bool, limit: int, force_recovery: bool):
    """Synchronize inbound email replies from Gmail."""
    conn = _get_conn()
    try:
        console.print(f"Connecting to Gmail (Mode: {'[yellow]DRY RUN[/yellow]' if dry_run else '[green]LIVE SYNC[/green]'})...")
        res = sync_inbound_replies(
            conn,
            dry_run=dry_run,
            max_messages=limit,
            force_recovery=force_recovery,
        )

        table = Table(title="Gmail Inbound Reply Sync Results", box=box.ROUNDED)
        table.add_column("Metric", style="cyan")
        table.add_column("Count / Value", style="white")

        table.add_row("Sync Strategy", res.sync_mode.upper())
        table.add_row("Messages Evaluated", str(res.messages_evaluated))
        table.add_row("New Replies Synced", f"[green]{res.new_replies_saved}[/green]")
        table.add_row("Matched to Outreach Leads", f"[bold green]{res.matched_count}[/bold green]")
        table.add_row("Unmatched Inbound", f"[yellow]{res.unmatched_count}[/yellow]")
        table.add_row("Duplicates Skipped", str(res.duplicates_skipped))
        table.add_row("Agency Outbound Skipped", str(res.agency_sent_skipped))
        if res.new_history_id:
            table.add_row("Updated History Cursor", res.new_history_id)

        console.print(table)

        if res.preview_items:
            console.print("\n[bold]Recent Inbound Messages:[/bold]")
            p_table = Table(box=box.SIMPLE)
            p_table.add_column("From", style="cyan")
            p_table.add_column("Subject", style="white")
            p_table.add_column("Matched Lead ID", style="magenta")
            p_table.add_column("Snippet", style="dim")
            for item in res.preview_items[:10]:
                p_table.add_row(
                    item["from_email"],
                    item["subject"][:35],
                    str(item["lead_id"] or "Unmatched"),
                    item["snippet"][:40],
                )
            console.print(p_table)

        if res.errors:
            console.print("\n[yellow]Warnings / Errors:[/yellow]")
            for err in res.errors:
                console.print(f"  • {err}")

    except Exception as e:
        console.print(f"[red]Sync failed: {e}[/red]")
    finally:
        conn.close()


@replies_group.command("list")
@click.option("--classification", "cls_filter", default=None, help="Filter by classification category")
@click.option("--needs-review", is_flag=True, help="Show only replies flagged for human review")
@click.option("--limit", type=int, default=20, help="Maximum replies to show")
def replies_list_cmd(cls_filter: str | None, needs_review: bool, limit: int):
    """List synced inbound replies and their classification states."""
    conn = _get_conn()
    try:
        query = """SELECT r.*, l.business_name
                   FROM replies r
                   LEFT JOIN leads l ON r.lead_id = l.id
                   WHERE 1=1"""
        params: list = []

        if cls_filter:
            query += " AND r.classification = ?"
            params.append(cls_filter)
        if needs_review:
            query += " AND r.needs_review = 1"

        query += " ORDER BY r.id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()

        if not rows:
            console.print("[yellow]No inbound replies found matching criteria.[/yellow]")
            return

        table = Table(title="Inbound Customer Replies", box=box.ROUNDED)
        table.add_column("ID", style="dim")
        table.add_column("From", style="cyan")
        table.add_column("Business", style="magenta")
        table.add_column("Classification", style="bold", no_wrap=True)
        table.add_column("Conf.", style="white")
        table.add_column("Review?", style="yellow")
        table.add_column("Received", style="dim")

        for r in rows:
            cls = r["classification"] or "pending"
            color = "green" if cls in ("positive_interest", "meeting_request") else ("red" if cls in ("unsubscribe", "bounce_or_delivery_failure") else "white")
            table.add_row(
                str(r["id"]),
                r["from_email"] or "",
                r["business_name"] or "Unmatched",
                f"[{color}]{cls}[/{color}]",
                f"{r['confidence']:.2f}" if r["confidence"] else "-",
                "[bold yellow]YES[/bold yellow]" if r["needs_review"] else "no",
                (r["received_at"] or r["created_at"])[:16],
            )

        console.print(table)
    finally:
        conn.close()


@replies_group.command("show")
@click.argument("reply_id", type=int)
def replies_show_cmd(reply_id: int):
    """Inspect full details of an inbound reply, classification & suggested response."""
    conn = _get_conn()
    try:
        r = conn.execute(
            """SELECT r.*, l.business_name, l.contact_name, l.email as lead_email,
                      m.subject as outbound_subject, m.body as outbound_body
               FROM replies r
               LEFT JOIN leads l ON r.lead_id = l.id
               LEFT JOIN messages m ON r.message_id = m.id
               WHERE r.id = ?""",
            (reply_id,),
        ).fetchone()

        if not r:
            console.print(f"[red]Reply ID {reply_id} not found.[/red]")
            return

        cls = r["classification"] or "unclassified"
        cls_color = "green" if cls in ("positive_interest", "meeting_request") else ("red" if cls in ("unsubscribe", "bounce_or_delivery_failure") else "yellow")

        console.print(
            Panel(
                f"[bold]Reply ID:[/bold] {r['id']}  |  [bold]Lead:[/bold] {r['business_name'] or 'Unmatched'} (ID: {r['lead_id'] or '-'})\n"
                f"[bold]From:[/bold] {r['from_email']}\n"
                f"[bold]To:[/bold] {r['to_email']}\n"
                f"[bold]Subject:[/bold] {r['subject']}\n"
                f"[bold]Received:[/bold] {r['received_at'] or r['created_at']}\n"
                f"[bold]Classification:[/bold] [{cls_color}]{cls.upper()}[/{cls_color}]  |  "
                f"[bold]Confidence:[/bold] {r['confidence'] or 0:.2f}  |  "
                f"[bold]Review Required:[/bold] {'[bold red]YES[/bold red]' if r['needs_review'] else 'No'}\n"
                f"[bold]Explanation:[/bold] {r['explanation'] or 'Not classified yet'}",
                title="Inbound Reply Metadata",
                border_style=cls_color,
            )
        )

        console.print(
            Panel(
                r["body"] or "[No body content]",
                title="Customer Reply Body",
                border_style="cyan",
            )
        )

        if r["suggested_reply"]:
            console.print(
                Panel(
                    r["suggested_reply"],
                    title=f"Suggested Response Draft (Status: {r['suggested_reply_status']})",
                    border_style="green",
                )
            )

        if r["outbound_subject"]:
            console.print(
                Panel(
                    f"[bold]Subject:[/bold] {r['outbound_subject']}\n\n"
                    f"{r['outbound_body'][:300]}...",
                    title="Original Outbound Context",
                    border_style="dim",
                )
            )

    finally:
        conn.close()


@replies_group.command("classify")
@click.option("--pending", is_flag=True, help="Classify all pending unclassified replies")
@click.option("--reply-id", type=int, default=None, help="Classify a specific reply ID")
@click.option("--no-ai", is_flag=True, help="Use deterministic rules only without LLM")
def replies_classify_cmd(pending: bool, reply_id: int | None, no_ai: bool):
    """Run reply classification agent on pending or specific inbound messages."""
    conn = _get_conn()
    try:
        if reply_id:
            res = classify_reply(conn, reply_id, use_ai=not no_ai)
            color = "green" if res.category in ("positive_interest", "meeting_request") else "white"
            console.print(
                Panel(
                    f"[bold]Category:[/bold] [{color}]{res.category.upper()}[/{color}]\n"
                    f"[bold]Confidence:[/bold] {res.confidence:.2f}\n"
                    f"[bold]Explanation:[/bold] {res.explanation}\n"
                    f"[bold]Review Required:[/bold] {'YES' if res.needs_review else 'No'}\n"
                    f"[bold]Suggested Reply:[/bold] {'Generated' if res.suggested_reply else 'None'}",
                    title=f"Classification Result for Reply #{reply_id}",
                    border_style="green",
                )
            )
            return

        # Batch classify pending
        res_batch = classify_pending_replies(conn, use_ai=not no_ai)
        console.print(
            Panel(
                f"[bold]Evaluated:[/bold] {res_batch['total_evaluated']}\n"
                f"[bold]Successfully Classified:[/bold] [green]{res_batch['processed']}[/green]\n"
                f"[bold]Flagged for Review:[/bold] [yellow]{res_batch['needs_review_count']}[/yellow]\n\n"
                + "\n".join(f"  • {cat}: {cnt}" for cat, cnt in res_batch["by_category"].items()),
                title="Batch Classification Results",
                border_style="green",
            )
        )
    finally:
        conn.close()


@replies_group.command("review")
@click.argument("reply_id", type=int)
@click.option("--override-class", type=click.Choice(CLASSIFICATION_CATEGORIES), help="Manually override category")
@click.option("--approve-reply", is_flag=True, help="Approve the suggested reply draft for outbound sending")
@click.option("--reply-text", help="Provide edited response body text")
def replies_review_cmd(reply_id: int, override_class: str | None, approve_reply: bool, reply_text: str | None):
    """Review, override classification, and approve suggested reply drafts."""
    conn = _get_conn()
    try:
        row = conn.execute("SELECT * FROM replies WHERE id = ?", (reply_id,)).fetchone()
        if not row:
            console.print(f"[red]Reply ID {reply_id} not found.[/red]")
            return

        with transaction(conn):
            if override_class:
                conn.execute(
                    """UPDATE replies SET
                       classification = ?,
                       needs_review = 0,
                       explanation = 'Manually reviewed and updated via CLI',
                       processed_at = datetime('now')
                       WHERE id = ?""",
                    (override_class, reply_id),
                )
                console.print(f"[green]✓ Updated classification to {override_class}[/green]")

            if reply_text:
                conn.execute(
                    "UPDATE replies SET suggested_reply = ? WHERE id = ?",
                    (reply_text, reply_id),
                )
                console.print("[green]✓ Updated suggested reply text.[/green]")

            if approve_reply:
                current_text = reply_text or row["suggested_reply"]
                if not current_text:
                    console.print("[red]Cannot approve: No suggested reply text exists for this reply.[/red]")
                    return

                # Create outreach message draft in messages table
                lead_id = row["lead_id"]
                if not lead_id:
                    console.print("[red]Cannot create outbound draft: Reply is not matched to a lead.[/red]")
                    return

                cur = conn.execute(
                    """INSERT INTO messages (
                       lead_id, recipient, subject, body, message_type,
                       status, quality_status, provider
                    ) VALUES (?, ?, ?, ?, 'reply', 'approved', 'approved', 'human_approved')""",
                    (
                        lead_id,
                        row["from_email"],
                        f"Re: {row['subject'] or 'Your inquiry'}",
                        current_text,
                    ),
                )
                new_msg_id = cur.lastrowid
                conn.execute(
                    "UPDATE replies SET suggested_reply_status = 'approved', needs_review = 0 WHERE id = ?",
                    (reply_id,),
                )
                console.print(
                    Panel(
                        f"[green]✓ Reply approved![/green]\n"
                        f"Created approved outbound message [bold]#{new_msg_id}[/bold].\n"
                        f"Dispatch via: [cyan]salesos outreach send {new_msg_id}[/cyan]",
                        title="Reply Draft Approved",
                        border_style="green",
                    )
                )

    finally:
        conn.close()


@replies_group.command("status")
def replies_status_cmd():
    """Display inbound reply intelligence metrics and review queue."""
    conn = _get_conn()
    try:
        by_class = {}
        for r in conn.execute("SELECT classification, count(*) as cnt FROM replies GROUP BY classification"):
            by_class[r["classification"] or "unclassified"] = r["cnt"]

        review_count = conn.execute("SELECT count(*) FROM replies WHERE needs_review = 1").fetchone()[0]
        total_replies = conn.execute("SELECT count(*) FROM replies").fetchone()[0]
        checkpoint = get_last_checkpoint(conn)

        table_cats = Table(box=box.ROUNDED)
        table_cats.add_column("Category", style="cyan")
        table_cats.add_column("Count", style="white")

        for cat, cnt in sorted(by_class.items()):
            color = "green" if cat in ("positive_interest", "meeting_request") else ("red" if cat in ("unsubscribe", "bounce_or_delivery_failure") else "white")
            table_cats.add_row(f"[{color}]{cat}[/{color}]", str(cnt))

        console.print("[bold]Inbound Reply Intelligence Overview[/bold]")
        console.print(
            Panel(
                f"[bold]Total Replies Processed:[/bold] {total_replies}\n"
                f"[bold]Pending Human Review:[/bold] [bold yellow]{review_count}[/bold yellow]\n"
                f"[bold]Last Sync Time:[/bold] {checkpoint['last_sync_at'] if checkpoint else 'Never'}\n"
                f"[bold]Gmail History Cursor:[/bold] {checkpoint['last_history_id'] if checkpoint else 'None'}",
                title="Reply Synchronization Health",
                border_style="green",
            )
        )
        console.print(table_cats)
    finally:
        conn.close()


# ── Follow-Ups Group ─────────────────────────────────────────────────

@cli.group("followups")
def followups_group():
    """Human-controlled follow-up planning and scheduling engine."""


@followups_group.command("plan")
@click.option("--limit", type=int, default=50, help="Maximum leads to evaluate for follow-up")
def followups_plan_cmd(limit: int):
    """Scan contacted non-responsive leads and schedule follow-up steps."""
    conn = _get_conn()
    try:
        res = plan_followups(conn, max_leads=limit)
        console.print(
            Panel(
                f"[bold]Leads Evaluated:[/bold] {res['leads_evaluated']}\n"
                f"[bold]New Follow-Ups Scheduled:[/bold] [green]{res['planned_count']}[/green]\n"
                f"[bold]Skipped (Replied/Suppressed):[/bold] [yellow]{res['skipped_count']}[/yellow]",
                title="Follow-Up Sequence Planner",
                border_style="green",
            )
        )
    finally:
        conn.close()


@followups_group.command("due")
def followups_due_cmd():
    """List scheduled follow-ups that have reached their due date."""
    conn = _get_conn()
    try:
        due_list = get_due_followups(conn)
        if not due_list:
            console.print("[green]✓ No follow-ups currently due for review.[/green]")
            return

        table = Table(title="Follow-Ups Due for Human Review", box=box.ROUNDED)
        table.add_column("Follow-Up ID", style="dim")
        table.add_column("Business", style="cyan")
        table.add_column("Recipient", style="white")
        table.add_column("Step", style="magenta")
        table.add_column("Due Date", style="yellow")
        table.add_column("Draft Msg ID", style="bold")

        for r in due_list:
            table.add_row(
                str(r["id"]),
                r["business_name"] or "Unknown",
                r["recipient"] or "",
                f"Step {r['followup_number']}",
                r["due_date"][:16],
                f"#{r['generated_message_id']}" if r["generated_message_id"] else "None",
            )

        console.print(table)
        console.print("\n[dim]To preview draft: salesos followups preview <ID>[/dim]")
        console.print("[dim]To authorize dispatch: salesos followups approve <ID>[/dim]")
    finally:
        conn.close()


@followups_group.command("preview")
@click.argument("followup_id", type=int)
def followups_preview_cmd(followup_id: int):
    """Preview follow-up schedule details and generated draft message."""
    conn = _get_conn()
    try:
        r = conn.execute(
            """SELECT f.*, m.subject, m.body, m.status as msg_status,
                      l.business_name, l.contact_name, l.email as lead_email
               FROM followups f
               LEFT JOIN messages m ON f.generated_message_id = m.id
               LEFT JOIN leads l ON f.lead_id = l.id
               WHERE f.id = ?""",
            (followup_id,),
        ).fetchone()

        if not r:
            console.print(f"[red]Follow-up ID {followup_id} not found.[/red]")
            return

        status_color = "green" if r["status"] == "approved" else ("red" if r["status"] == "cancelled" else "yellow")

        console.print(
            Panel(
                f"[bold]Follow-Up ID:[/bold] {r['id']}  |  [bold]Lead:[/bold] {r['business_name']} (ID: {r['lead_id']})\n"
                f"[bold]Step Number:[/bold] {r['followup_number']}  |  [bold]Status:[/bold] [{status_color}]{r['status'].upper()}[/{status_color}]\n"
                f"[bold]Due Date:[/bold] {r['due_date']}  |  [bold]Original Msg ID:[/bold] #{r['original_message_id']}\n"
                f"[bold]Reason:[/bold] {r['scheduled_reason']}",
                title="Follow-Up Schedule Details",
                border_style=status_color,
            )
        )

        if r["body"]:
            console.print(
                Panel(
                    f"[bold]Subject:[/bold] {r['subject']}\n\n"
                    f"{r['body']}",
                    title=f"Generated Follow-Up Draft (Message #{r['generated_message_id']})",
                    border_style="cyan",
                )
            )
        else:
            console.print("[yellow]No draft message generated yet for this follow-up.[/yellow]")

    finally:
        conn.close()


@followups_group.command("approve")
@click.argument("followup_id", type=int)
def followups_approve_cmd(followup_id: int):
    """Approve a follow-up draft for outreach dispatch."""
    conn = _get_conn()
    try:
        ok, msg = approve_followup(conn, followup_id)
        if ok:
            console.print(f"[green]✓ {msg}[/green]")
        else:
            console.print(f"[red]✗ {msg}[/red]")
    finally:
        conn.close()


@followups_group.command("cancel")
@click.argument("followup_id", type=int)
@click.option("--reason", default="manual_cancel", help="Reason for cancellation")
def followups_cancel_cmd(followup_id: int, reason: str):
    """Cancel a scheduled follow-up sequence."""
    conn = _get_conn()
    try:
        ok = cancel_followup(conn, followup_id, reason=reason)
        if ok:
            console.print(f"[green]✓ Follow-up {followup_id} cancelled.[/green]")
        else:
            console.print(f"[red]Follow-up {followup_id} not found.[/red]")
    finally:
        conn.close()


@followups_group.command("status")
def followups_status_cmd():
    """Display follow-up automation metrics and cadence settings."""
    conn = _get_conn()
    try:
        s = get_followups_status(conn)
        tbl = Table(title="Follow-Up Automation Pipeline", box=box.ROUNDED)
        tbl.add_column("Metric / Status", style="cyan")
        tbl.add_column("Value", style="white")

        by_st = s["followups_by_status"]
        tbl.add_row("Scheduled Ahead", str(by_st.get("scheduled", 0) + by_st.get("planned", 0)))
        tbl.add_row("Due for Review Today", f"[bold yellow]{s['due_for_review']}[/bold yellow]")
        tbl.add_row("Approved for Sending", f"[green]{by_st.get('approved', 0)}[/green]")
        tbl.add_row("Successfully Sent", f"[bold green]{by_st.get('sent', 0)}[/bold green]")
        tbl.add_row("Cancelled (Replied/Opt-Out)", str(by_st.get("cancelled", 0)))

        tbl.add_section()
        tbl.add_row("Step 1 Delay", f"{s['step1_delay_days']} business days")
        tbl.add_row("Step 2 Delay", f"{s['step2_delay_days']} business days")
        tbl.add_row("Max Steps per Campaign", f"{s['max_steps']} follow-ups")

        console.print(tbl)
    finally:
        conn.close()


# ── Leads Group & CRM Timeline ───────────────────────────────────────

@cli.group("leads")
def leads_group():
    """Lead records and CRM timeline commands."""


@leads_group.command("timeline")
@click.argument("lead_id", type=int)
def leads_timeline_cmd(lead_id: int):
    """Display comprehensive chronological CRM conversation timeline for a lead."""
    conn = _get_conn()
    try:
        data = get_lead_timeline(conn, lead_id)

        console.print(
            Panel(
                f"[bold]Lead ID:[/bold] {data['lead_id']}  |  [bold]Business:[/bold] {data['business_name']}\n"
                f"[bold]Contact:[/bold] {data['contact_name'] or 'Unknown'}  |  [bold]Email:[/bold] {data['email']}\n"
                f"[bold]Website:[/bold] {data['website'] or 'None'}  |  [bold]Status:[/bold] {data['status'].upper()}  |  "
                f"[bold]Qualification Score:[/bold] {data['score'] or 0:.1f}",
                title=f"CRM Timeline: {data['business_name']}",
                border_style="cyan",
            )
        )

        table = Table(box=box.ROUNDED)
        table.add_column("Timestamp", style="dim", no_wrap=True)
        table.add_column("Event Type", style="cyan", no_wrap=True)
        table.add_column("Title & Details", style="white")

        badge_colors = {
            "info": "blue",
            "success": "green",
            "warning": "yellow",
            "error": "red",
            "draft": "magenta",
            "reply": "bold green",
        }

        for ev in data["events"]:
            b_color = badge_colors.get(ev.get("badge", "info"), "white")
            table.add_row(
                ev["timestamp"][:19],
                f"[{b_color}]{ev['type']}[/{b_color}]",
                f"[bold]{ev['title']}[/bold]\n[dim]{ev['description']}[/dim]",
            )

        console.print(table)
    except Exception as e:
        console.print(f"[red]Error displaying timeline: {e}[/red]")
    finally:
        conn.close()


# ── Meetings Group (Milestone 6) ─────────────────────────────────────

@cli.group("meetings")
def meetings_group():
    """Meeting lifecycle management, booking synchronization, and prep briefs."""


@meetings_group.command("list")
@click.option("--status", help="Filter by status (scheduled, completed, cancelled, no_show)")
@click.option("--lead-id", type=int, help="Filter by lead ID")
@click.option("--limit", type=int, default=20, help="Maximum meetings to list")
def meetings_list_cmd(status: str | None, lead_id: int | None, limit: int):
    """List scheduled and completed client discovery meetings."""
    conn = _get_conn()
    try:
        meetings = list_meetings(conn, status=status, lead_id=lead_id, limit=limit)
        if not meetings:
            console.print("[yellow]No meetings found matching criteria.[/yellow]")
            return

        table = Table(title="Client Meetings", box=box.ROUNDED)
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Title", style="bold", no_wrap=True)
        table.add_column("Business", style="cyan")
        table.add_column("Scheduled Time (UTC)", style="white", no_wrap=True)
        table.add_column("Status", style="magenta", no_wrap=True)
        table.add_column("Source", style="dim")
        table.add_column("Brief?", style="green")

        status_colors = {
            "scheduled": "bold blue",
            "completed": "bold green",
            "cancelled": "bold red",
            "no_show": "bold yellow",
        }

        for m in meetings:
            st = m["status"]
            c = status_colors.get(st, "white")
            has_brief = "YES" if m["brief_content"] else "no"
            table.add_row(
                str(m["id"]),
                m["title"],
                m["business_name"] or f"Lead #{m['lead_id']}",
                m["scheduled_time"][:16].replace("T", " "),
                f"[{c}]{st}[/{c}]",
                m["booking_source"],
                has_brief,
            )

        console.print(table)
    finally:
        conn.close()


@meetings_group.command("show")
@click.argument("meeting_id", type=int)
def meetings_show_cmd(meeting_id: int):
    """Inspect meeting details, attendee info, and recorded outcomes."""
    conn = _get_conn()
    try:
        m = get_meeting(conn, meeting_id)
        has_brief = "[green]Generated[/green]" if m["brief_content"] else "[dim]Not generated yet[/dim]"

        outcome_panel = ""
        if m["outcome_notes"]:
            outcome_panel = (
                f"\n\n[bold]Outcome Notes:[/bold] {m['outcome_notes']}\n"
                f"[bold]Customer Needs:[/bold] {m['customer_needs'] or 'None recorded'}\n"
                f"[bold]Confirmed Requirements:[/bold] {m['confirmed_requirements'] or 'None'}\n"
                f"[bold]Agreed Actions:[/bold] {m['agreed_actions'] or 'None'} (Owner: {m['action_owner'] or 'Unassigned'})\n"
                f"[bold]Next Step:[/bold] {m['next_step'] or 'None'}\n"
                f"[bold]Proposal Needed:[/bold] {m['proposal_needed'].upper()}"
            )

        console.print(
            Panel(
                f"[bold]Title:[/bold] {m['title']}\n"
                f"[bold]Business:[/bold] {m['business_name']} (Lead #{m['lead_id']})\n"
                f"[bold]Contact Email:[/bold] {m['lead_email']}\n"
                f"[bold]Scheduled Time:[/bold] {m['scheduled_time']} ({m['timezone']})\n"
                f"[bold]Duration:[/bold] {m['duration_minutes']} minutes\n"
                f"[bold]Location / Link:[/bold] {m['location_or_url'] or 'Not specified'}\n"
                f"[bold]Status:[/bold] {m['status'].upper()}\n"
                f"[bold]Booking Source:[/bold] {m['booking_source']}\n"
                f"[bold]Preparation Brief:[/bold] {has_brief}\n"
                f"[bold]Notes:[/bold] {m['notes'] or 'None'}"
                f"{outcome_panel}",
                title=f"Meeting #{m['id']}: {m['title']}",
                border_style="cyan",
            )
        )
    except Exception as e:
        console.print(f"[red]Error displaying meeting: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("create")
@click.option("--lead-id", type=int, prompt="Lead ID", help="Target lead ID")
@click.option("--title", prompt="Meeting title", help="Meeting title")
@click.option("--time", "scheduled_time", prompt="Scheduled time (e.g. 2026-11-05 14:00)", help="Scheduled time in ISO-8601")
@click.option("--duration", type=int, default=30, help="Duration in minutes (default: 30)")
@click.option("--timezone", "timezone_name", default="UTC", help="Timezone name (default: UTC)")
@click.option("--url", "location_or_url", default=None, help="Meeting location or URL")
@click.option("--notes", default=None, help="Pre-meeting notes")
@click.option("--source", default="manual", help="Booking source (default: manual)")
def meetings_create_cmd(
    lead_id: int,
    title: str,
    scheduled_time: str,
    duration: int,
    timezone_name: str,
    location_or_url: str | None,
    notes: str | None,
    source: str,
):
    """Schedule a new client discovery meeting."""
    conn = _get_conn()
    try:
        meeting = create_meeting(
            conn,
            lead_id=lead_id,
            title=title,
            scheduled_time=scheduled_time,
            duration_minutes=duration,
            timezone_name=timezone_name,
            location_or_url=location_or_url,
            booking_source=source,
            notes=notes,
        )
        console.print(
            Panel(
                f"[bold]Meeting ID:[/bold] {meeting['id']}\n"
                f"[bold]Title:[/bold] {meeting['title']}\n"
                f"[bold]Business:[/bold] {meeting['business_name']}\n"
                f"[bold]Scheduled Time:[/bold] {meeting['scheduled_time']} ({meeting['timezone']})\n"
                f"[bold]Status:[/bold] [green]{meeting['status'].upper()}[/green]",
                title="Meeting Scheduled Successfully",
                border_style="green",
            )
        )
    except DuplicateBookingError as e:
        console.print(f"[bold red]Duplicate Booking Error:[/bold red] {e}")
    except Exception as e:
        console.print(f"[red]Failed to schedule meeting: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("update")
@click.argument("meeting_id", type=int)
@click.option("--title", help="New title")
@click.option("--time", "scheduled_time", help="New scheduled time")
@click.option("--duration", type=int, help="New duration in minutes")
@click.option("--timezone", "timezone_name", help="New timezone")
@click.option("--url", "location_or_url", help="New location or URL")
@click.option("--notes", help="New notes")
def meetings_update_cmd(
    meeting_id: int,
    title: str | None,
    scheduled_time: str | None,
    duration: int | None,
    timezone_name: str | None,
    location_or_url: str | None,
    notes: str | None,
):
    """Update details or reschedule an existing meeting."""
    conn = _get_conn()
    try:
        kwargs: dict[str, Any] = {}
        if title:
            kwargs["title"] = title
        if scheduled_time:
            kwargs["scheduled_time"] = scheduled_time
        if duration:
            kwargs["duration_minutes"] = duration
        if timezone_name:
            kwargs["timezone"] = timezone_name
        if location_or_url:
            kwargs["location_or_url"] = location_or_url
        if notes:
            kwargs["notes"] = notes

        updated = update_meeting(conn, meeting_id, **kwargs)
        console.print(f"[green]Meeting #{updated['id']} updated successfully.[/green]")
    except Exception as e:
        console.print(f"[red]Error updating meeting: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("cancel")
@click.argument("meeting_id", type=int)
@click.option("--reason", required=True, prompt="Cancellation reason", help="Cancellation reason")
def meetings_cancel_cmd(meeting_id: int, reason: str):
    """Cancel a scheduled meeting and record the cancellation reason."""
    conn = _get_conn()
    try:
        cancelled = cancel_meeting(conn, meeting_id, reason=reason)
        console.print(f"[yellow]Meeting #{cancelled['id']} cancelled. Reason: {reason}[/yellow]")
    except Exception as e:
        console.print(f"[red]Error cancelling meeting: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("complete")
@click.argument("meeting_id", type=int)
def meetings_complete_cmd(meeting_id: int):
    """Mark a meeting as completed and trigger pipeline progression."""
    conn = _get_conn()
    try:
        m = complete_meeting(conn, meeting_id)
        console.print(f"[green]Meeting #{m['id']} marked as completed. Associated deal moved to 'meeting_completed'.[/green]")
    except Exception as e:
        console.print(f"[red]Error completing meeting: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("brief")
@click.argument("meeting_id", type=int)
@click.option("--force-ai", is_flag=True, help="Force LLM assistant generation")
@click.option("--no-ai", is_flag=True, help="Use deterministic evidence-based brief")
def meetings_brief_cmd(meeting_id: int, force_ai: bool, no_ai: bool):
    """Generate or view evidence-backed meeting preparation brief."""
    conn = _get_conn()
    try:
        brief_text = generate_meeting_brief(conn, meeting_id, force_ai=force_ai, no_ai=no_ai)
        console.print(Markdown(brief_text))
    except Exception as e:
        console.print(f"[red]Error generating meeting brief: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("outcome")
@click.argument("meeting_id", type=int)
@click.option("--notes", required=True, prompt="Outcome summary notes", help="Summary notes of the call")
@click.option("--needs", default=None, help="Customer needs identified")
@click.option("--requirements", default=None, help="Confirmed technical or business requirements")
@click.option("--objections", default=None, help="Customer objections or concerns")
@click.option("--actions", default=None, help="Agreed actions")
@click.option("--owner", default=None, help="Action assignee")
@click.option("--due-date", default=None, help="Action due date (YYYY-MM-DD)")
@click.option("--proposal", type=click.Choice(["yes", "no", "unknown"]), default="unknown", help="Proposal needed?")
@click.option("--followup", is_flag=True, help="Generate follow-up draft message for review")
@click.option("--next-step", default=None, help="Next scheduled step")
def meetings_outcome_cmd(
    meeting_id: int,
    notes: str,
    needs: str | None,
    requirements: str | None,
    objections: str | None,
    actions: str | None,
    owner: str | None,
    due_date: str | None,
    proposal: str,
    followup: bool,
    next_step: str | None,
):
    """Record post-meeting structured review and outcomes."""
    conn = _get_conn()
    try:
        m = record_meeting_outcome(
            conn,
            meeting_id,
            outcome_notes=notes,
            customer_needs=needs,
            confirmed_requirements=requirements,
            objections=objections,
            agreed_actions=actions,
            action_owner=owner,
            action_due_date=due_date,
            proposal_needed=proposal,
            followup_required=followup,
            next_step=next_step,
        )
        msg_note = "\n[bold yellow]Draft follow-up email created for human review.[/bold yellow]" if followup else ""
        console.print(
            Panel(
                f"[bold]Meeting #{m['id']} Completed[/bold]\n"
                f"[bold]Business:[/bold] {m['business_name']}\n"
                f"[bold]Outcome Notes:[/bold] {notes}\n"
                f"[bold]Proposal Needed:[/bold] {proposal.upper()}\n"
                f"[bold]Next Step:[/bold] {next_step or 'None'}"
                f"{msg_note}",
                title="Post-Meeting Outcome Recorded",
                border_style="green",
            )
        )
    except Exception as e:
        console.print(f"[red]Error recording outcome: {e}[/red]")
    finally:
        conn.close()


@meetings_group.command("sync")
@click.option("--token", default=None, help="Calendly personal access token")
@click.option("--user-uri", default=None, help="Calendly user URI")
def meetings_sync_cmd(token: str | None, user_uri: str | None):
    """Synchronize scheduled events from Calendly API (optional)."""
    conn = _get_conn()
    try:
        res = sync_calendly_events(conn, access_token=token, user_uri=user_uri)
        if res["status"] == "unconfigured":
            console.print(
                Panel(
                    f"[bold yellow]Calendly Integration Optional / Unconfigured[/bold yellow]\n\n"
                    f"{res['message']}\n"
                    f"To enable Calendly synchronization:\n"
                    f"1. Generate a Personal Access Token in Calendly > Integrations.\n"
                    f"2. Add `CALENDLY_ACCESS_TOKEN=your-token` to your `.env` file.\n"
                    f"Offline meeting management and manual booking remain 100% operational.",
                    title="Calendly Sync Status",
                    border_style="yellow",
                )
            )
        elif res["status"] == "success":
            console.print(f"[green]Successfully synced {res['synced_count']} event(s) from Calendly.[/green]")
        else:
            console.print(f"[red]Calendly sync failed: {res.get('message')}[/red]")
    finally:
        conn.close()


# ── Pipeline Group (Milestone 6) ─────────────────────────────────────

@cli.group("pipeline")
def pipeline_group():
    """Sales pipeline, deal intelligence, and stage transition engine."""


@pipeline_group.command("board")
def pipeline_board_cmd():
    """Display interactive visual sales pipeline board across stages."""
    conn = _get_conn()
    try:
        deals = list_deals(conn, limit=200)
        report = get_pipeline_report(conn)

        console.print(
            Panel(
                f"[bold]Active Deals in Pipeline:[/bold] [green]{report['total_active_deals']}[/green]   |   "
                f"[bold]Total Active Value:[/bold] [bold green]${report['total_pipeline_value']:,.2f}[/bold green]   |   "
                f"[bold]Win Rate:[/bold] [cyan]{report['performance']['win_rate_percent']}%[/cyan] "
                f"({report['performance']['won_count']} Won / {report['performance']['lost_count']} Lost)",
                title="Sales Pipeline Overview",
                border_style="green",
            )
        )

        table = Table(title="Sales Deals by Pipeline Stage", box=box.ROUNDED)
        table.add_column("ID", style="dim", no_wrap=True)
        table.add_column("Deal Name", style="bold", no_wrap=True)
        table.add_column("Business", style="cyan")
        table.add_column("Stage", style="magenta", no_wrap=True)
        table.add_column("Est. Value", style="green", no_wrap=True)
        table.add_column("Owner", style="white")
        table.add_column("Next Action", style="yellow")
        table.add_column("Due Date", style="dim", no_wrap=True)

        stage_styles = {
            "lead_captured": "dim",
            "outreach_ready": "cyan",
            "contacted": "blue",
            "engaged": "bold blue",
            "meeting_booked": "bold magenta",
            "meeting_completed": "bold cyan",
            "proposal_sent": "bold yellow",
            "negotiation": "bold yellow",
            "won": "bold green",
            "lost": "bold red",
        }

        for d in deals:
            st = d["stage"]
            st_color = stage_styles.get(st, "white")
            val_str = f"${d['estimated_value']:,.2f}" if d["estimated_value"] is not None else "[dim]Unspecified[/dim]"
            table.add_row(
                str(d["id"]),
                d["name"],
                d["business_name"],
                f"[{st_color}]{st}[/{st_color}]",
                val_str,
                d["owner"] or "[dim]Unassigned[/dim]",
                d["next_action"] or "[dim]None[/dim]",
                d["next_action_due"] or "-",
            )

        console.print(table)
    finally:
        conn.close()


@pipeline_group.command("show")
@click.argument("deal_id", type=int)
def pipeline_show_cmd(deal_id: int):
    """Deep inspection of a sales deal and its evidentiary stage history."""
    conn = _get_conn()
    try:
        deal = get_deal(conn, deal_id)
        val_str = f"${deal['estimated_value']:,.2f} {deal['currency']}" if deal["estimated_value"] is not None else "Unspecified"
        conf_str = f"{deal['confidence'] * 100:.0f}%" if deal["confidence"] is not None else "Unspecified"

        console.print(
            Panel(
                f"[bold]Deal Name:[/bold] {deal['name']}\n"
                f"[bold]Lead Business:[/bold] {deal['business_name']} (Lead #{deal['lead_id']})\n"
                f"[bold]Service Interest:[/bold] {deal['service_interest'] or 'General'}\n"
                f"[bold]Current Stage:[/bold] [bold magenta]{deal['stage'].upper()}[/bold magenta]\n"
                f"[bold]Estimated Value:[/bold] {val_str}\n"
                f"[bold]Win Probability:[/bold] {conf_str} ({deal['confidence_explanation'] or 'None'})\n"
                f"[bold]Owner / Assignee:[/bold] {deal['owner'] or 'Unassigned'}\n"
                f"[bold]Expected Close Date:[/bold] {deal['expected_close_date'] or 'Unspecified'}\n"
                f"[bold]Next Action:[/bold] {deal['next_action'] or 'None'} (Due: {deal['next_action_due'] or 'None'})\n"
                f"[bold]Last Activity:[/bold] {deal['last_activity_at']}",
                title=f"Deal #{deal['id']}: {deal['name']}",
                border_style="cyan",
            )
        )

        ev_list = deal["stage_evidence"]
        if ev_list:
            table_ev = Table(title="Evidentiary Stage Transition Log", box=box.SIMPLE)
            table_ev.add_column("Timestamp", style="dim")
            table_ev.add_column("Transition", style="bold")
            table_ev.add_column("Actor / Trigger", style="cyan")
            table_ev.add_column("Notes / Evidence", style="white")

            for ev in ev_list:
                tr = f"{ev.get('from_stage', 'start')} ➔ {ev.get('to_stage', deal['stage'])}"
                actor = ev.get("actor", "system")
                notes = ev.get("override_reason") or ev.get("trigger_event") or "Verified"
                table_ev.add_row(ev.get("timestamp", "")[:19], tr, actor, str(notes))

            console.print(table_ev)
    except Exception as e:
        console.print(f"[red]Error displaying deal: {e}[/red]")
    finally:
        conn.close()


@pipeline_group.command("move")
@click.argument("deal_id", type=int)
@click.argument("stage")
@click.option("--force", is_flag=True, help="Force override transition constraints")
@click.option("--reason", default=None, help="Auditable justification if force=True")
@click.option("--win-notes", default=None, help="Confirmation notes if marking won")
@click.option("--loss-reason", default=None, help="Explicit reason if marking lost")
def pipeline_move_cmd(
    deal_id: int,
    stage: str,
    force: bool,
    reason: str | None,
    win_notes: str | None,
    loss_reason: str | None,
):
    """Transition a deal to a new pipeline stage with validation."""
    conn = _get_conn()
    try:
        evidence: dict[str, Any] = {}
        if win_notes:
            evidence["win_notes"] = win_notes
        if loss_reason:
            evidence["loss_reason"] = loss_reason

        updated = move_deal_stage(
            conn,
            deal_id,
            target_stage=stage,
            evidence=evidence,
            force=force,
            reason=reason,
            actor="cli_user",
        )
        console.print(f"[green]Deal #{deal_id} successfully moved to stage '{updated['stage']}'.[/green]")
    except (InvalidTransitionError, SuppressedLeadError, ValueError) as e:
        console.print(f"[bold red]Transition Rejected:[/bold red] {e}")
    except Exception as e:
        console.print(f"[red]Error moving deal: {e}[/red]")
    finally:
        conn.close()


@pipeline_group.command("update")
@click.argument("deal_id", type=int)
@click.option("--name", help="New deal name")
@click.option("--value", type=float, help="Estimated deal value")
@click.option("--currency", default="USD", help="Currency")
@click.option("--owner", help="Assignee / deal owner")
@click.option("--next-action", help="Next action description")
@click.option("--due", "next_action_due", help="Next action due date (YYYY-MM-DD)")
@click.option("--confidence", type=float, help="Win probability (0.0 - 1.0)")
def pipeline_update_cmd(
    deal_id: int,
    name: str | None,
    value: float | None,
    currency: str,
    owner: str | None,
    next_action: str | None,
    next_action_due: str | None,
    confidence: float | None,
):
    """Update deal value, owner, next action, or probability."""
    conn = _get_conn()
    try:
        kwargs: dict[str, Any] = {}
        if name:
            kwargs["name"] = name
        if value is not None:
            kwargs["estimated_value"] = value
            kwargs["currency"] = currency
        if owner:
            kwargs["owner"] = owner
        if next_action:
            kwargs["next_action"] = next_action
        if next_action_due:
            kwargs["next_action_due"] = next_action_due
        if confidence is not None:
            kwargs["confidence"] = confidence

        updated = update_deal(conn, deal_id, **kwargs)
        console.print(f"[green]Deal #{updated['id']} updated successfully.[/green]")
    except Exception as e:
        console.print(f"[red]Error updating deal: {e}[/red]")
    finally:
        conn.close()


@pipeline_group.command("report")
@click.option("--stalled-days", type=int, default=14, help="Inactivity days for stalled deals")
def pipeline_report_cmd(stalled_days: int):
    """Display comprehensive executive sales pipeline & deal intelligence report."""
    conn = _get_conn()
    try:
        rep = get_pipeline_report(conn, inactive_days=stalled_days)

        console.print(
            Panel(
                f"[bold]Active Pipeline Deals:[/bold] [green]{rep['total_active_deals']}[/green]   |   "
                f"[bold]Total Active Value:[/bold] [bold green]${rep['total_pipeline_value']:,.2f}[/bold green]   |   "
                f"[bold]Win Rate:[/bold] [bold cyan]{rep['performance']['win_rate_percent']}%[/bold cyan] "
                f"({rep['performance']['won_count']} Won / {rep['performance']['lost_count']} Lost)",
                title="Executive Pipeline Intelligence Report",
                border_style="green",
            )
        )

        # Stage table
        table_stages = Table(title="Pipeline Stages & Valuations", box=box.ROUNDED)
        table_stages.add_column("Stage", style="bold")
        table_stages.add_column("Deals Count", style="white")
        table_stages.add_column("Deals With Value", style="cyan")
        table_stages.add_column("Total Value (USD)", style="green")

        for stage, data in rep["by_stage"].items():
            table_stages.add_row(
                stage,
                str(data["count"]),
                str(data["valued_count"]),
                f"${data['total_value']:,.2f}",
            )
        console.print(table_stages)

        # Action Cues
        cues = rep["action_cues"]
        console.print(
            Panel(
                f"[bold]Leads Awaiting Contact:[/bold] {cues['leads_awaiting_contact']}\n"
                f"[bold]Positive Replies Needing Review:[/bold] [bold yellow]{cues['positive_replies_review']}[/bold yellow]\n"
                f"[bold]Meetings Needing Preparation Brief:[/bold] [bold yellow]{cues['meetings_needing_prep']}[/bold yellow]\n"
                f"[bold]Deals With Overdue Next Actions:[/bold] [bold red]{cues['overdue_actions']}[/bold red]\n"
                f"[bold]Stalled Deals (> {stalled_days} days inactive):[/bold] [bold red]{cues['stalled_deals']}[/bold red]",
                title="Immediate Action Cues & Health",
                border_style="yellow",
            )
        )

        # Data quality warnings
        if rep["data_quality_warnings"]:
            warn_text = "\n".join(f"[bold yellow]![/bold yellow] {w}" for w in rep["data_quality_warnings"])
            console.print(Panel(warn_text, title="Data Quality Warnings", border_style="red"))
    finally:
        conn.close()


@pipeline_group.command("export")
@click.argument("filepath")
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="csv", help="Export format")
def pipeline_export_cmd(filepath: str, fmt: str):
    """Export complete sales pipeline and deals to CSV or JSON."""
    conn = _get_conn()
    try:
        count = export_pipeline(conn, filepath=filepath, format=fmt)
        console.print(f"[green]Successfully exported {count} deal(s) to '{filepath}' ({fmt.upper()}).[/green]")
    except Exception as e:
        console.print(f"[red]Export failed: {e}[/red]")
    finally:
        conn.close()



# ── Integrations & Health Check CLI (Acceptance Audit) ───────────────────


@cli.group("integrations")
def integrations_group():
    """Integration audit, health checks, and acceptance testing."""
    pass


@integrations_group.command("status")
def integrations_status_cmd():
    """Display quick status of all external and internal integrations."""
    report = run_all_health_checks(live=False)

    badge_styles = {
        CheckStatus.PASS: "green",
        CheckStatus.FAIL: "red",
        CheckStatus.NOT_CONFIGURED: "yellow",
        CheckStatus.SKIPPED: "dim",
        CheckStatus.BLOCKED: "bold red",
        CheckStatus.NOT_IMPLEMENTED: "magenta",
    }

    table = Table(title="NexAgentLab SalesOS — Integration Status", box=box.ROUNDED)
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Summary")

    for check in report.checks:
        color = badge_styles.get(check.status, "white")
        table.add_row(
            check.component,
            f"[{color}][bold]{check.status.value}[/bold][/{color}]",
            check.summary,
        )

    console.print(table)
    readiness_color = "green" if report.readiness == ReadinessLevel.READY_FOR_CONTROLLED_PILOT else "yellow" if report.readiness == ReadinessLevel.READY_FOR_LOCAL_TESTING_ONLY else "red"
    console.print(Panel(f"[bold]System Readiness Level:[/bold] [{readiness_color}][bold]{report.readiness.value}[/bold][/{readiness_color}]", border_style=readiness_color))


@integrations_group.command("check")
@click.option("--live", is_flag=True, default=False, help="Perform safe, read-only live network connectivity probes.")
@click.option("--read-only", is_flag=True, default=True, help="Enforce strictly read-only checks without side effects.")
@click.option("--json", "json_path", type=click.Path(), default=None, help="Save detailed JSON health report to file.")
@click.option("--md", "md_path", type=click.Path(), default=None, help="Save formatted Markdown acceptance report to file.")
def integrations_check_cmd(live: bool, read_only: bool, json_path: str | None, md_path: str | None):
    """Run comprehensive integration health checks and acceptance audit."""
    mode_str = "Live Probe Mode" if live else "Local Safe Mode"
    console.print(f"[cyan]Running integration health checks in [bold]{mode_str}[/bold] (read-only={read_only})...[/cyan]")

    report = run_all_health_checks(live=live)

    badge_styles = {
        CheckStatus.PASS: "green",
        CheckStatus.FAIL: "red",
        CheckStatus.NOT_CONFIGURED: "yellow",
        CheckStatus.SKIPPED: "dim",
        CheckStatus.BLOCKED: "bold red",
        CheckStatus.NOT_IMPLEMENTED: "magenta",
    }

    table = Table(title=f"Integration Health Check Results ({mode_str})", box=box.ROUNDED)
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Summary")
    table.add_column("Remediation / Note")

    for check in report.checks:
        color = badge_styles.get(check.status, "white")
        table.add_row(
            check.component,
            f"[{color}][bold]{check.status.value}[/bold][/{color}]",
            check.summary,
            check.remediation or "[dim]None[/dim]",
        )

    console.print(table)

    readiness_color = "green" if report.readiness == ReadinessLevel.READY_FOR_CONTROLLED_PILOT else "yellow" if report.readiness == ReadinessLevel.READY_FOR_LOCAL_TESTING_ONLY else "red"
    console.print(
        Panel(
            f"[bold]Readiness Classification:[/bold] [{readiness_color}][bold]{report.readiness.value}[/bold][/{readiness_color}]\n"
            f"[bold]Evaluated At:[/bold] {report.timestamp}\n"
            f"[bold]Mode:[/bold] {'Live Network Enabled' if live else 'Safe Offline / Local'}",
            title="Acceptance Verdict",
            border_style=readiness_color,
        )
    )

    if json_path:
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                f.write(report.to_json())
            console.print(f"[green]Saved JSON report to '{json_path}'.[/green]")
        except Exception as e:
            console.print(f"[yellow]Could not write JSON report to '{json_path}': {e}[/yellow]")

    if md_path:
        try:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(report.to_markdown())
            console.print(f"[green]Saved Markdown report to '{md_path}'.[/green]")
        except Exception as e:
            console.print(f"[yellow]Could not write Markdown report to '{md_path}': {e}[/yellow]")


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
