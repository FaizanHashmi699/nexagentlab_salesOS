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


# ── Entry point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
