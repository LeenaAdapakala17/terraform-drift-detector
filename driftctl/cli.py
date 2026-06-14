"""
driftctl/cli.py

Command-line interface built with Typer.

Commands:
  scan              Compare a tfstate file against live AWS
  report            View a saved scan report from SQLite
  scans list        List recent scans
  workspace list    List configured workspaces
  workspace create  Create a new workspace
  schedule create   Set a cron schedule for a workspace
  serve             Start the REST API server + web dashboard

Exit codes:
  0  no drift detected
  1  drift detected
  2  error (parse failure, AWS error, etc.)
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from driftctl.engine.drift import detect_drift
from driftctl.engine.remediate import enrich_results
from driftctl.models import (
    ScanReport,
    StateReadError,
    UnsupportedStateVersionError,
)
from driftctl.providers.registry import DefaultRegistry
from driftctl.report.json_renderer import render_json_string
from driftctl.report.table_renderer import render_table, render_table_string
from driftctl.state.extractor import extract_from_state
from driftctl.state.reader import read_state

console = Console(stderr=True)  # status messages to stderr, stdout stays clean for JSON

# ---------------------------------------------------------------------------
# App and sub-apps
# ---------------------------------------------------------------------------

app          = typer.Typer(help="Terraform drift detector", add_completion=False)
workspace_app = typer.Typer(help="Manage workspaces")
scans_app     = typer.Typer(help="List and view saved scans")
schedule_app  = typer.Typer(help="Manage cron schedules")

app.add_typer(workspace_app, name="workspace")
app.add_typer(scans_app,     name="scans")
app.add_typer(schedule_app,  name="schedule")


# ---------------------------------------------------------------------------
# driftctl scan
# ---------------------------------------------------------------------------

@app.command()
def scan(
    state: Optional[str] = typer.Option(
        None,
        "--state",
        help="Path to .tfstate file or s3://bucket/key",
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        help="Path to driftctl.yaml config file",
        exists=False,
    ),
    workspace: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="Workspace name from config file",
    ),
    provider: str = typer.Option(
        "aws",
        "--provider",
        help="Cloud provider (default: aws)",
    ),
    region: str = typer.Option(
        "us-east-1",
        "--region",
        help="AWS region to scan",
    ),
    profile: Optional[str] = typer.Option(
        None,
        "--profile",
        help="AWS credential profile name",
    ),
    output: str = typer.Option(
        "table",
        "--output",
        help="Output format: table or json",
    ),
    skip_cloud: bool = typer.Option(
        False,
        "--skip-cloud",
        help="Parse state only, skip AWS API calls (offline mode)",
    ),
    unmanaged: bool = typer.Option(
        False,
        "--unmanaged/--no-unmanaged",
        help="Detect resources in cloud not in state",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Include IN_SYNC resources in output",
    ),
    output_file: Optional[Path] = typer.Option(
        None,
        "--output-file",
        help="Write JSON output to file",
    ),
) -> None:
    """
    Compare a Terraform state file against live AWS infrastructure.

    Examples:\n
      driftctl scan --state terraform.tfstate --region us-east-1\n
      driftctl scan --state s3://my-bucket/prod/terraform.tfstate\n
      driftctl scan --state terraform.tfstate --skip-cloud\n
      driftctl scan --state terraform.tfstate --output json\n
    """
    # ------------------------------------------------------------------
    # Resolve state source
    # ------------------------------------------------------------------
    state_source = _resolve_state_source(state, config, workspace)
    if not state_source:
        console.print(
            "[red]Error:[/red] No state source specified. "
            "Use --state, or --config with --workspace."
        )
        raise typer.Exit(code=2)

    # ------------------------------------------------------------------
    # Read and extract state (expected model)
    # ------------------------------------------------------------------
    console.print(f"[dim]Reading state from:[/dim] {state_source}")
    try:
        raw_records = read_state(state_source, region=region)
    except (StateReadError, UnsupportedStateVersionError) as exc:
        console.print(f"[red]Error reading state:[/red] {exc}")
        raise typer.Exit(code=2)

    expected = []
    for record in raw_records:
        resource = extract_from_state(
            record["type"],
            record["name"],
            record["attributes"],
        )
        if resource is not None:
            expected.append(resource)

    console.print(
        f"[dim]State:[/dim] {len(expected)} managed resources extracted"
    )

    # ------------------------------------------------------------------
    # Fetch actual state from cloud (unless --skip-cloud)
    # ------------------------------------------------------------------
    actual = []
    if skip_cloud:
        console.print("[yellow]--skip-cloud:[/yellow] skipping AWS API calls")
    else:
        console.print(f"[dim]Fetching live resources from AWS ({region})...[/dim]")
        try:
            cloud_provider = DefaultRegistry(region=region, profile=profile).get(provider)
            if cloud_provider is None:
                console.print(f"[red]Error:[/red] Unknown provider: {provider}")
                raise typer.Exit(code=2)

            resource_types = list({r.type for r in expected})
            for resource_type in resource_types:
                fetched = cloud_provider.fetch(resource_type)
                actual.extend(fetched)

            console.print(
                f"[dim]Cloud:[/dim] {len(actual)} live resources fetched"
            )
        except Exception as exc:
            console.print(f"[red]AWS error:[/red] {exc}")
            raise typer.Exit(code=2)

    # ------------------------------------------------------------------
    # Run drift engine + remediation
    # ------------------------------------------------------------------
    drift_results = detect_drift(
        expected,
        actual,
        detect_unmanaged=unmanaged,
    )
    enrich_results(drift_results)

    # ------------------------------------------------------------------
    # Build ScanReport
    # ------------------------------------------------------------------
    report = ScanReport(
        scan_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        state_path=state_source,
        region=region,
        workspace=workspace,
        results=drift_results,
    )

    # ------------------------------------------------------------------
    # Save to SQLite if available
    # ------------------------------------------------------------------
    _try_save_scan(report)

    # ------------------------------------------------------------------
    # Render output
    # ------------------------------------------------------------------
    if output == "json":
        json_out = render_json_string(report, verbose=verbose)
        typer.echo(json_out)
        if output_file:
            output_file.write_text(json_out, encoding="utf-8")
            console.print(f"[dim]JSON written to:[/dim] {output_file}")
    else:
        render_table(report, verbose=verbose)
        if output_file:
            table_str = render_table_string(report, verbose=verbose)
            output_file.write_text(table_str, encoding="utf-8")
            console.print(f"[dim]Output written to:[/dim] {output_file}")

    # ------------------------------------------------------------------
    # Exit code
    # ------------------------------------------------------------------
    raise typer.Exit(code=report.exit_code)


# ---------------------------------------------------------------------------
# driftctl report <scan-id>
# ---------------------------------------------------------------------------

@app.command()
def report(
    scan_id: str = typer.Argument(..., help="Scan ID to display"),
    output: str = typer.Option(
        "table",
        "--output",
        help="Output format: table or json",
    ),
    output_file: Optional[Path] = typer.Option(
        None,
        "--output-file",
        help="Write output to file",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Include IN_SYNC resources",
    ),
) -> None:
    """Display a saved scan report from the database."""
    scan_report = _load_scan_from_db(scan_id)
    if scan_report is None:
        console.print(f"[red]Error:[/red] Scan '{scan_id}' not found.")
        raise typer.Exit(code=2)

    if output == "json":
        out = render_json_string(scan_report, verbose=verbose)
        typer.echo(out)
        if output_file:
            output_file.write_text(out, encoding="utf-8")
    else:
        render_table(scan_report, verbose=verbose)
        if output_file:
            output_file.write_text(
                render_table_string(scan_report, verbose=verbose),
                encoding="utf-8",
            )


# ---------------------------------------------------------------------------
# driftctl scans list
# ---------------------------------------------------------------------------

@scans_app.command("list")
def scans_list(
    workspace_name: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="Filter by workspace name",
    ),
    limit: int = typer.Option(20, "--limit", help="Max results"),
    output: str = typer.Option("table", "--output", help="table or json"),
) -> None:
    """List recent scans stored in the database."""
    scans = _list_scans_from_db(workspace=workspace_name, limit=limit)
    if not scans:
        console.print("[dim]No scans found.[/dim]")
        return

    if output == "json":
        typer.echo(json.dumps(scans, indent=2))
        return

    from rich.table import Table
    from rich import box as rbox

    tbl = Table(box=rbox.ROUNDED, header_style="bold white")
    tbl.add_column("Scan ID",    min_width=36)
    tbl.add_column("Created At", min_width=20)
    tbl.add_column("Workspace",  min_width=12)
    tbl.add_column("State",      min_width=30)
    tbl.add_column("Drifted",    min_width=8)
    tbl.add_column("Exit Code",  min_width=8)

    for s in scans:
        tbl.add_row(
            s.get("scan_id", ""),
            s.get("created_at", ""),
            s.get("workspace") or "—",
            s.get("state_path", ""),
            str(s.get("drifted_count", 0)),
            str(s.get("exit_code", "?")),
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
# driftctl workspace list
# ---------------------------------------------------------------------------

@workspace_app.command("list")
def workspace_list() -> None:
    """List all workspaces in the database."""
    workspaces = _list_workspaces_from_db()
    if not workspaces:
        console.print(
            "[dim]No workspaces found. Use [bold]driftctl workspace create[/bold] to add one.[/dim]"
        )
        return

    from rich.table import Table
    from rich import box as rbox

    tbl = Table(box=rbox.ROUNDED, header_style="bold white")
    tbl.add_column("Name",     min_width=14)
    tbl.add_column("Provider", min_width=8)
    tbl.add_column("State",    min_width=35)
    tbl.add_column("Region",   min_width=12)
    tbl.add_column("Schedule", min_width=16)

    for ws in workspaces:
        tbl.add_row(
            ws.get("name", ""),
            ws.get("provider", "aws"),
            ws.get("state_path", ""),
            ws.get("region", ""),
            ws.get("schedule_cron") or "—",
        )
    console.print(tbl)


# ---------------------------------------------------------------------------
# driftctl workspace create
# ---------------------------------------------------------------------------

@workspace_app.command("create")
def workspace_create(
    name: str = typer.Option(..., "--name", help="Workspace name"),
    state: str = typer.Option(..., "--state", help="State source (local path or s3://…)"),
    region: str = typer.Option(..., "--region", help="AWS region"),
    backend: str = typer.Option("local", "--backend", help="local or s3"),
    cron: Optional[str] = typer.Option(None, "--cron", help="Cron schedule expression"),
    unmanaged: bool = typer.Option(False, "--unmanaged/--no-unmanaged"),
) -> None:
    """Create a new workspace."""
    ws_id = _save_workspace_to_db(
        name=name,
        state_path=state,
        state_backend=backend,
        region=region,
        schedule_cron=cron,
        detect_unmanaged=unmanaged,
    )
    if ws_id:
        console.print(f"[green]✓[/green] Workspace '[bold]{name}[/bold]' created (id: {ws_id})")
    else:
        console.print(
            f"[yellow]Note:[/yellow] Database not available. "
            f"Workspace '{name}' not persisted."
        )


# ---------------------------------------------------------------------------
# driftctl schedule create
# ---------------------------------------------------------------------------

@schedule_app.command("create")
def schedule_create(
    workspace_name: str = typer.Option(..., "--workspace", help="Workspace name"),
    cron: str = typer.Option(..., "--cron", help='Cron expression e.g. "0 6 * * *"'),
) -> None:
    """Set or update a cron schedule for a workspace."""
    ok = _update_schedule_in_db(workspace_name, cron)
    if ok:
        console.print(
            f"[green]✓[/green] Schedule set for '[bold]{workspace_name}[/bold]': "
            f"[cyan]{cron}[/cyan]"
        )
    else:
        console.print(
            f"[red]Error:[/red] Workspace '{workspace_name}' not found "
            f"or database not available."
        )


# ---------------------------------------------------------------------------
# driftctl serve
# ---------------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host", help="Host to bind"),
    port: int = typer.Option(8080, "--port", help="Port to listen on"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload (dev mode)"),
) -> None:
    """Start the REST API server and web dashboard."""
    try:
        import uvicorn
        from driftctl.api.server import create_app

        console.print(
            f"[green]Starting driftctl server on http://{host}:{port}[/green]"
        )
        uvicorn.run(
            "driftctl.api.server:app",
            host=host,
            port=port,
            reload=reload,
        )
    except ImportError:
        console.print(
            "[red]Error:[/red] uvicorn not installed. "
            "Run: pip install uvicorn"
        )
        raise typer.Exit(code=2)


# ---------------------------------------------------------------------------
# Private helpers — thin wrappers around storage layer
# These return gracefully if storage is not yet initialised (Phase 5).
# ---------------------------------------------------------------------------

def _resolve_state_source(
    state: str | None,
    config: Path | None,
    workspace: str | None,
) -> str | None:
    """
    Resolve the state file source from CLI flags or config file.
    Priority: --state flag > config file workspace > None.
    """
    if state:
        return state

    if config and workspace:
        return _state_from_config(config, workspace)

    # Try default config location
    default_config = Path("configs/driftctl.yaml")
    if workspace and default_config.exists():
        return _state_from_config(default_config, workspace)

    return None


def _state_from_config(config_path: Path, workspace_name: str) -> str | None:
    """Read the state source for a workspace from a YAML config file."""
    try:
        import yaml
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        for ws in cfg.get("workspaces", []):
            if ws.get("name") == workspace_name:
                state_cfg = ws.get("state", {})
                backend = state_cfg.get("backend", "local")
                if backend == "s3":
                    bucket = state_cfg.get("bucket", "")
                    key    = state_cfg.get("key", "")
                    return f"s3://{bucket}/{key}"
                return state_cfg.get("path", "")
    except Exception as exc:
        console.print(f"[yellow]Warning:[/yellow] Could not read config: {exc}")
    return None


def _try_save_scan(report: ScanReport) -> None:
    """Save a scan to SQLite if storage is available. Silently skip if not."""
    try:
        from driftctl.storage.db import save_scan
        save_scan(report)
    except Exception:
        pass  # Storage not yet initialised — Phase 5 adds it


def _load_scan_from_db(scan_id: str) -> ScanReport | None:
    """Load a scan from SQLite. Returns None if not available."""
    try:
        from driftctl.storage.db import get_scan
        return get_scan(scan_id)
    except Exception:
        return None


def _list_scans_from_db(
    workspace: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List recent scans from SQLite. Returns [] if not available."""
    try:
        from driftctl.storage.db import list_scans
        return list_scans(workspace=workspace, limit=limit)
    except Exception:
        return []


def _list_workspaces_from_db() -> list[dict]:
    """List workspaces from SQLite. Returns [] if not available."""
    try:
        from driftctl.storage.db import list_workspaces
        return list_workspaces()
    except Exception:
        return []


def _save_workspace_to_db(**kwargs) -> str | None:
    """Save a workspace to SQLite. Returns workspace id or None."""
    try:
        from driftctl.storage.db import save_workspace
        return save_workspace(**kwargs)
    except Exception:
        return None


def _update_schedule_in_db(workspace_name: str, cron: str) -> bool:
    """Update a workspace's cron schedule. Returns True on success."""
    try:
        from driftctl.storage.db import update_schedule
        return update_schedule(workspace_name, cron)
    except Exception:
        return False
