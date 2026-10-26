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
from rich.panel import Panel
from rich.table import Table

from .db import get_connection, get_db_path, init_db
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


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
