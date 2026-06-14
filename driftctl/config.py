"""
driftctl/config.py

Configuration loader.

Priority (highest to lowest):
  1. CLI flags (handled by Typer in cli.py)
  2. Environment variables
  3. YAML config file
  4. Built-in defaults

Usage:
  from driftctl.config import load_config, Config
  cfg = load_config("configs/driftctl.yaml")
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class StateConfig:
    backend: str = "local"       # "local" | "s3"
    path: str = ""               # local file path
    bucket: str = ""             # s3 bucket (s3 backend only)
    key: str = ""                # s3 key    (s3 backend only)
    region: str = ""             # s3 region (s3 backend only)

    def source_uri(self) -> str:
        """Return the canonical state source string."""
        if self.backend == "s3":
            return f"s3://{self.bucket}/{self.key}"
        return self.path


@dataclass
class WorkspaceConfig:
    name: str = ""
    provider: str = "aws"
    state: StateConfig = field(default_factory=StateConfig)
    regions: list[str] = field(default_factory=lambda: ["us-east-1"])
    detect_unmanaged: bool = False
    schedule_cron: str | None = None

    @property
    def region(self) -> str:
        """Primary region (first in the list)."""
        return self.regions[0] if self.regions else "us-east-1"


@dataclass
class ApiConfig:
    addr: str = ":8080"
    api_key: str = ""

    @property
    def host(self) -> str:
        parts = self.addr.split(":")
        return parts[0] if len(parts) == 2 and parts[0] else "0.0.0.0"

    @property
    def port(self) -> int:
        parts = self.addr.split(":")
        try:
            return int(parts[-1])
        except ValueError:
            return 8080


@dataclass
class Config:
    database: str = "driftctl.db"
    api: ApiConfig = field(default_factory=ApiConfig)
    default_region: str = "us-east-1"
    default_profile: str = ""
    detect_unmanaged: bool = False
    workspaces: list[WorkspaceConfig] = field(default_factory=list)

    def get_workspace(self, name: str) -> WorkspaceConfig | None:
        """Return a workspace config by name, or None."""
        for ws in self.workspaces:
            if ws.name == name:
                return ws
        return None


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

_DEFAULTS = Config()


def load_config(path: str | Path | None = None) -> Config:
    """
    Load configuration from a YAML file, then apply environment
    variable overrides.

    Args:
        path: Path to driftctl.yaml. If None or file not found,
              returns defaults + env var overrides.

    Returns:
        A populated Config object.
    """
    cfg = Config()

    # Load from YAML if available
    if path:
        yaml_data = _load_yaml(str(path))
        if yaml_data:
            cfg = _parse_yaml(yaml_data)

    # Apply environment variable overrides
    _apply_env(cfg)

    # Initialise DB with the configured path
    _init_db(cfg.database)

    return cfg


def _load_yaml(path: str) -> dict | None:
    """Load and parse a YAML file. Returns None on any error."""
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        logger.debug("Loaded config from %s", path)
        return data or {}
    except FileNotFoundError:
        logger.debug("Config file not found: %s", path)
        return None
    except Exception as exc:
        logger.warning("Could not load config %s: %s", path, exc)
        return None


def _parse_yaml(data: dict) -> Config:
    """Parse a raw YAML dict into a Config object."""
    cfg = Config()

    cfg.database       = data.get("database", "driftctl.db")
    cfg.default_region = data.get("default_region", "us-east-1")
    cfg.default_profile = data.get("default_profile", "")
    cfg.detect_unmanaged = bool(
        data.get("scan", {}).get("detect_unmanaged", False)
    )

    # API config
    api_data = data.get("api", {})
    cfg.api = ApiConfig(
        addr=api_data.get("addr", ":8080"),
        api_key=api_data.get("api_key", ""),
    )

    # Workspace configs
    for ws_data in data.get("workspaces", []):
        ws = _parse_workspace(ws_data, cfg.default_region)
        cfg.workspaces.append(ws)

    return cfg


def _parse_workspace(data: dict, default_region: str) -> WorkspaceConfig:
    """Parse a single workspace dict."""
    ws = WorkspaceConfig()
    ws.name     = data.get("name", "")
    ws.provider = data.get("provider", "aws")
    ws.regions  = data.get("regions", [default_region])
    ws.detect_unmanaged = bool(data.get("detect_unmanaged", False))

    # Schedule
    schedule = data.get("schedule", {})
    ws.schedule_cron = schedule.get("cron") if schedule else None

    # State backend
    state_data = data.get("state", {})
    backend = state_data.get("backend", "local")
    ws.state = StateConfig(
        backend=backend,
        path=state_data.get("path", ""),
        bucket=state_data.get("bucket", ""),
        key=state_data.get("key", ""),
        region=state_data.get("region", ws.regions[0] if ws.regions else default_region),
    )

    return ws


def _apply_env(cfg: Config) -> None:
    """Apply environment variable overrides to a Config object."""
    if val := os.environ.get("DRIFTCTL_DB"):
        cfg.database = val
    if val := os.environ.get("DRIFTCTL_REGION"):
        cfg.default_region = val
    if val := os.environ.get("AWS_DEFAULT_REGION"):
        cfg.default_region = cfg.default_region or val
    if val := os.environ.get("AWS_PROFILE"):
        cfg.default_profile = cfg.default_profile or val


def _init_db(db_path: str) -> None:
    """Tell the storage layer which database file to use."""
    try:
        from driftctl.storage.db import set_db_path
        set_db_path(db_path)
    except Exception as exc:
        logger.warning("Could not init database: %s", exc)


# ---------------------------------------------------------------------------
# Example config writer (for driftctl workspace create)
# ---------------------------------------------------------------------------

def write_example_config(path: str = "configs/driftctl.yaml") -> None:
    """Write an example driftctl.yaml if it doesn't exist."""
    target = Path(path)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        """\
# driftctl configuration
database: driftctl.db

api:
  addr: ":8080"
  api_key: ""

default_region: us-east-1
default_profile: ""

scan:
  detect_unmanaged: false

workspaces:
  - name: local-dev
    provider: aws
    state:
      backend: local
      path: ./terraform.tfstate
    regions: [us-east-1]

  - name: prod
    provider: aws
    state:
      backend: s3
      bucket: my-tf-state-bucket
      key: prod/terraform.tfstate
      region: us-east-1
    regions: [us-east-1]
    schedule:
      cron: "0 6 * * *"
""",
        encoding="utf-8",
    )
    logger.info("Example config written to %s", path)
