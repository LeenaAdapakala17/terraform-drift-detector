"""
driftctl/report/table_renderer.py

Renders a ScanReport as a colour-coded terminal table using Rich.

Status colours:
  MISSING    → red
  UNMANAGED  → yellow
  MODIFIED   → cyan
  TAG_DRIFT  → blue
  IN_SYNC    → green  (shown only with verbose=True)

Includes a Remediation column showing a truncated hint.
Full remediation text is visible in --output json or the dashboard.
"""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

from driftctl.models import DriftResult, ScanReport

# Max characters for the remediation column before truncating
REMEDIATION_TRUNCATE = 55

STATUS_COLOURS: dict[str, str] = {
    "MISSING":   "red",
    "UNMANAGED": "yellow",
    "MODIFIED":  "cyan",
    "TAG_DRIFT": "blue",
    "IN_SYNC":   "green",
}


def render_table(
    report: ScanReport,
    verbose: bool = False,
) -> None:
    """
    Print the drift report as a Rich table to stdout.

    Args:
        report:  The scan report to render.
        verbose: When True, include IN_SYNC resources in the output.
    """
    console = Console()
    results = report.results if verbose else report.drifted

    if not results:
        console.print(
            "\n[bold green]✓ No drift detected.[/bold green] "
            f"All {report.total_resources} resources are in sync.\n"
        )
        return

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold white",
        border_style="grey50",
        expand=False,
    )

    table.add_column("Resource Type",  style="white",      min_width=22)
    table.add_column("ID",             style="dim white",  min_width=20)
    table.add_column("Name",           style="dim white",  min_width=12)
    table.add_column("Status",         min_width=10)
    table.add_column("Changed Fields", style="dim white",  min_width=18)
    table.add_column("Remediation",    style="dim white",  min_width=35)

    for result in results:
        colour = STATUS_COLOURS.get(result.status, "white")

        # Changed fields: list attribute diff keys + "tags" if tag drift
        changed = list(result.attribute_diffs.keys())
        if result.tag_diffs:
            changed.append("tags")
        changed_str = ", ".join(changed) if changed else "—"

        # Remediation: first line only, truncated
        remediation_str = _truncate_remediation(result.remediation)

        table.add_row(
            result.type,
            result.id,
            result.name or "—",
            Text(result.status, style=f"bold {colour}"),
            changed_str,
            remediation_str,
        )

    console.print()
    console.print(table)
    _print_summary(console, report)


def render_table_string(
    report: ScanReport,
    verbose: bool = False,
) -> str:
    """
    Return the table as a plain string (for REST API format=table response).
    Strips Rich markup.
    """
    from io import StringIO
    buffer = StringIO()
    console = Console(file=buffer, highlight=False, markup=False)
    results = report.results if verbose else report.drifted

    if not results:
        console.print(
            f"No drift detected. All {report.total_resources} resources are in sync."
        )
        return buffer.getvalue()

    console.print(f"Scan ID  : {report.scan_id}")
    console.print(f"Time     : {report.created_at}")
    console.print(f"State    : {report.state_path}")
    console.print(f"Region   : {report.region}")
    console.print("")
    console.print(
        f"{'RESOURCE TYPE':<24} {'ID':<22} {'STATUS':<12} {'FIELDS':<20} REMEDIATION"
    )
    console.print("-" * 100)

    for result in results:
        changed = list(result.attribute_diffs.keys())
        if result.tag_diffs:
            changed.append("tags")
        changed_str = ", ".join(changed) if changed else "—"
        remediation_str = _truncate_remediation(result.remediation)
        console.print(
            f"{result.type:<24} {result.id:<22} {result.status:<12} "
            f"{changed_str:<20} {remediation_str}"
        )

    console.print("")
    summary = report.summary()
    console.print(
        f"Total: {summary['total_resources']} resources  |  "
        f"Drifted: {summary['drifted']}  |  "
        f"Missing: {summary['missing']}  |  "
        f"Unmanaged: {summary['unmanaged']}  |  "
        f"Modified: {summary['modified']}  |  "
        f"Tag drift: {summary['tag_drift']}"
    )

    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _print_summary(console: Console, report: ScanReport) -> None:
    """Print the coloured summary line below the table."""
    s = report.summary()
    parts = []

    if s["missing"] > 0:
        parts.append(f"[red]{s['missing']} MISSING[/red]")
    if s["unmanaged"] > 0:
        parts.append(f"[yellow]{s['unmanaged']} UNMANAGED[/yellow]")
    if s["modified"] > 0:
        parts.append(f"[cyan]{s['modified']} MODIFIED[/cyan]")
    if s["tag_drift"] > 0:
        parts.append(f"[blue]{s['tag_drift']} TAG_DRIFT[/blue]")

    summary_str = "  ·  ".join(parts) if parts else "[green]all in sync[/green]"

    console.print(
        f"\n  [bold]{s['drifted']} of {s['total_resources']} resources drifted[/bold]"
        f"  —  {summary_str}\n"
    )
    if s["drifted"] > 0:
        console.print(
            "  [dim]Run [bold]driftctl report <scan-id>[/bold] to view full "
            "remediation commands.[/dim]\n"
        )


def _truncate_remediation(remediation: str | None) -> str:
    """
    Return the first meaningful line of a remediation hint, truncated.
    Skips comment lines (starting with #) to show the actual command.
    """
    if not remediation:
        return "—"

    # Find the first non-comment, non-empty line
    for line in remediation.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if len(stripped) > REMEDIATION_TRUNCATE:
                return stripped[:REMEDIATION_TRUNCATE] + "…"
            return stripped

    # All lines are comments — return the first comment line trimmed
    first = remediation.splitlines()[0].strip().lstrip("# ")
    if len(first) > REMEDIATION_TRUNCATE:
        return first[:REMEDIATION_TRUNCATE] + "…"
    return first
