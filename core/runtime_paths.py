"""Canonical runtime-owned paths shared by core services and the daemon.

The model-call ledger deliberately lives here instead of in a feature-specific
module: health, privacy, retention, reconciliation, and every provider boundary
must resolve exactly the same durable owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved durable paths for one Mnemos runtime configuration."""

    data_dir: Path
    database_dir: Path

    @classmethod
    def from_config(cls, config: Any | None = None) -> "RuntimePaths":
        if config is None:
            from core.config import get_config

            config = get_config()
        return cls(data_dir=Path(config.data_dir), database_dir=Path(config.database_dir))

    @classmethod
    def fallback(cls) -> "RuntimePaths":
        fallback = Path.home() / ".mnemos"
        return cls(data_dir=fallback, database_dir=fallback)

    @property
    def model_call_ledger_db(self) -> Path:
        """The single canonical durable owner for billable-model accounting."""
        return self.database_dir / "model_call_ledger.db"

    @property
    def pid_file(self) -> Path:
        return self.database_dir / "daemon.pid"

    @property
    def status_file(self) -> Path:
        return self.database_dir / "daemon.status"

    @property
    def daemon_log(self) -> Path:
        return self.database_dir / "logs" / "daemon.log"

    @property
    def heartbeat_file(self) -> Path:
        return self.database_dir / "daemon_heartbeat.json"
