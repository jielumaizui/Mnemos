"""SQLite/WAL/temp/snapshot disk budget health for Mnemos."""

from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.ops.durable_io import read_native_bytes


SCHEMA_VERSION = "mnemos.sqlite_disk_budget.v1"
BYTES_PER_MIB = 1024 * 1024
SECONDS_PER_DAY = 24 * 60 * 60
DEFAULT_TEMP_PATTERNS = (
    "mnemos_*",
    "mnemos-*",
    "mnemos*.tmp",
    "mnemos*.sqlite",
    "mnemos*.db",
)


@dataclass(frozen=True)
class BudgetFinding:
    area: str
    metric: str
    severity: str
    observed_bytes: int
    budget_bytes: int
    message: str
    handling: str
    user_action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "area": self.area,
            "metric": self.metric,
            "severity": self.severity,
            "observed_bytes": self.observed_bytes,
            "budget_bytes": self.budget_bytes,
            "message": self.message,
            "handling": self.handling,
            "user_action": self.user_action,
        }


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        return getter(key, default)
    data = getattr(config, "_data", {})
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _cfg_float(config: Any, key: str, default: float) -> float:
    try:
        raw = _cfg_get(config, key, default)
        return default if raw is None else float(raw)
    except (TypeError, ValueError):
        return default


def _budget_bytes(config: Any, key: str, default_mb: float) -> int:
    return int(max(0.0, _cfg_float(config, key, default_mb)) * BYTES_PER_MIB)


def _database_dir(config: Any) -> Path:
    value = getattr(config, "database_dir", None)
    return Path(value).expanduser() if value is not None else Path.home() / ".mnemos"


def _mnemos_dir(config: Any) -> Path:
    value = getattr(config, "mnemos_dir", None) or getattr(config, "data_dir", None)
    return Path(value).expanduser() if value is not None else _database_dir(config)


def _state_path(config: Any) -> Path:
    return _database_dir(config) / "sqlite_disk_budget_state.json"


def _safe_stat_size(path: Path) -> int:
    try:
        metadata = path.lstat()
        return int(metadata.st_size) if stat.S_ISREG(metadata.st_mode) else 0
    except OSError:
        return 0


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return _safe_stat_size(path)
    total = 0
    for item in path.rglob("*"):
        total += _safe_stat_size(item)
    return total


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_native_bytes(path).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
    except OSError:
        return


def _temp_files(patterns: Iterable[str] = DEFAULT_TEMP_PATTERNS) -> list[Path]:
    root = Path(tempfile.gettempdir())
    files: dict[Path, Path] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if path.is_file():
                files[path] = path
    return sorted(files)


def _mtime_age_seconds(path: Path, now: float) -> float:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return 0.0


def _rate_bytes_per_day(
    *,
    previous: Mapping[str, Any],
    current_size: int,
    now: float,
    min_interval_seconds: float,
) -> tuple[float | None, bool]:
    try:
        previous_size = int(previous.get("size_bytes", 0) or 0)
        previous_ts = float(previous.get("sampled_at", 0) or 0)
    except (TypeError, ValueError):
        return None, True
    if previous_ts <= 0:
        return None, True
    delta_seconds = now - previous_ts
    if delta_seconds < min_interval_seconds:
        return None, False
    delta_bytes = max(0, current_size - previous_size)
    return (delta_bytes / delta_seconds) * SECONDS_PER_DAY, True


def _finding(
    *,
    area: str,
    metric: str,
    observed_bytes: int,
    budget_bytes: int,
    message: str,
    handling: str,
    user_action: str,
) -> BudgetFinding:
    return BudgetFinding(
        area=area,
        metric=metric,
        severity="degraded",
        observed_bytes=observed_bytes,
        budget_bytes=budget_bytes,
        message=message,
        handling=handling,
        user_action=user_action,
    )


def build_sqlite_disk_budget_report(
    config: Any,
    *,
    update_state: bool = True,
) -> dict[str, Any]:
    """Build a health report for SQLite WAL, temp, snapshot, and raw artifacts."""

    now = time.time()
    database_dir = _database_dir(config)
    mnemos_dir = _mnemos_dir(config)
    snapshot_dir = mnemos_dir / "backups" / "snapshots"
    raw_projection_backup_dirs = sorted(
        path
        for path in (mnemos_dir / "backups").glob("raw-vault-projection-*")
        if path.is_dir()
    ) if (mnemos_dir / "backups").is_dir() else []
    state_path = _state_path(config)
    state = _load_state(state_path)

    wal_files = sorted(database_dir.glob("*.db-wal")) if database_dir.exists() else []
    wal_entries: list[dict[str, Any]] = [
        {"path": str(path), "size_bytes": _safe_stat_size(path)} for path in wal_files
    ]
    wal_total = sum(item["size_bytes"] for item in wal_entries)

    temp_entries: list[dict[str, Any]] = [
        {
            "path": str(path),
            "size_bytes": _safe_stat_size(path),
            "age_seconds": int(_mtime_age_seconds(path, now)),
        }
        for path in _temp_files()
    ]
    temp_total = sum(item["size_bytes"] for item in temp_entries)
    stale_seconds = _cfg_float(config, "storage.disk_budget.temp_stale_minutes", 60) * 60
    stale_temp_total = sum(
        item["size_bytes"] for item in temp_entries if item["age_seconds"] >= stale_seconds
    )

    raw_events_path = database_dir / "raw_events.db"
    raw_events_size = _safe_stat_size(raw_events_path)
    snapshot_total = _tree_size(snapshot_dir)
    raw_projection_backup_total = sum(_tree_size(path) for path in raw_projection_backup_dirs)
    min_interval = _cfg_float(
        config, "storage.disk_budget.growth_sample_min_seconds", 300
    )

    raw_growth_per_day, raw_should_update = _rate_bytes_per_day(
        previous=(
            state.get("raw_events", {})
            if isinstance(state.get("raw_events"), Mapping)
            else {}
        ),
        current_size=raw_events_size,
        now=now,
        min_interval_seconds=min_interval,
    )
    snapshot_growth_per_day, snapshot_should_update = _rate_bytes_per_day(
        previous=(
            state.get("snapshots", {})
            if isinstance(state.get("snapshots"), Mapping)
            else {}
        ),
        current_size=snapshot_total,
        now=now,
        min_interval_seconds=min_interval,
    )

    budgets: dict[str, int] = {
        "sqlite_wal_file_max_bytes": _budget_bytes(
            config, "storage.disk_budget.sqlite_wal_file_max_mb", 512
        ),
        "sqlite_wal_total_max_bytes": _budget_bytes(
            config, "storage.disk_budget.sqlite_wal_total_max_mb", 1024
        ),
        "temp_total_max_bytes": _budget_bytes(
            config, "storage.disk_budget.temp_total_max_mb", 2048
        ),
        "snapshot_total_max_bytes": _budget_bytes(
            config, "storage.disk_budget.snapshot_total_max_mb", 20480
        ),
        "snapshot_growth_max_bytes_per_day": _budget_bytes(
            config, "storage.disk_budget.snapshot_growth_max_mb_per_day", 8192
        ),
        "raw_projection_backup_total_max_bytes": _budget_bytes(
            config, "storage.disk_budget.raw_projection_backup_total_max_mb", 1024
        ),
        "raw_events_max_bytes": _budget_bytes(
            config, "storage.disk_budget.raw_events_max_mb", 4096
        ),
        "raw_events_growth_max_bytes_per_day": _budget_bytes(
            config, "storage.disk_budget.raw_events_growth_max_mb_per_day", 2048
        ),
    }

    findings: list[BudgetFinding] = []
    for entry in wal_entries:
        if entry["size_bytes"] > budgets["sqlite_wal_file_max_bytes"]:
            findings.append(
                _finding(
                    area="wal",
                    metric="sqlite_wal_file",
                    observed_bytes=int(entry["size_bytes"]),
                    budget_bytes=budgets["sqlite_wal_file_max_bytes"],
                    message="SQLite WAL file exceeds per-file disk budget.",
                    handling="auto_heal_safe",
                    user_action=(
                        "Run python3 scripts/repair_sqlite_disk_budget.py --apply --wal, "
                        "then rerun python3 mnemos_cli.py health --json."
                    ),
                )
            )
    if wal_total > budgets["sqlite_wal_total_max_bytes"]:
        findings.append(
            _finding(
                area="wal",
                metric="sqlite_wal_total",
                observed_bytes=wal_total,
                budget_bytes=budgets["sqlite_wal_total_max_bytes"],
                message="SQLite WAL total exceeds disk budget.",
                handling="auto_heal_safe",
                user_action=(
                    "Run python3 scripts/repair_sqlite_disk_budget.py --apply --wal, "
                    "then rerun python3 mnemos_cli.py health --json."
                ),
            )
        )
    if temp_total > budgets["temp_total_max_bytes"]:
        handling = "auto_heal_safe" if stale_temp_total else "manual_required"
        action = (
            "Run python3 scripts/repair_sqlite_disk_budget.py --apply --temp; "
            "if young temp remains large, stop Mnemos and inspect the temp files."
            if stale_temp_total
            else "Stop Mnemos, inspect current temp writers, then rerun python3 mnemos_cli.py health --json."
        )
        findings.append(
            _finding(
                area="temp",
                metric="temp_total",
                observed_bytes=temp_total,
                budget_bytes=budgets["temp_total_max_bytes"],
                message="Mnemos temp files exceed disk budget.",
                handling=handling,
                user_action=action,
            )
        )
    if snapshot_total > budgets["snapshot_total_max_bytes"]:
        findings.append(
            _finding(
                area="snapshot",
                metric="snapshot_total",
                observed_bytes=snapshot_total,
                budget_bytes=budgets["snapshot_total_max_bytes"],
                message="Snapshot storage exceeds disk budget.",
                handling="manual_required",
                user_action=(
                    "Run python3 mnemos_cli.py backup list --json, decide which "
                    "snapshots to keep, then remove only confirmed obsolete snapshot "
                    "directories."
                ),
            )
        )
    if (
        snapshot_growth_per_day is not None
        and snapshot_growth_per_day > budgets["snapshot_growth_max_bytes_per_day"]
    ):
        findings.append(
            _finding(
                area="snapshot",
                metric="snapshot_growth_per_day",
                observed_bytes=int(snapshot_growth_per_day),
                budget_bytes=budgets["snapshot_growth_max_bytes_per_day"],
                message="Snapshot growth rate exceeds disk budget.",
                handling="manual_required",
                user_action=(
                    "Review snapshot triggers with python3 mnemos_cli.py backup list "
                    "--json before deleting any snapshot data."
                ),
            )
        )
    if raw_projection_backup_total > budgets["raw_projection_backup_total_max_bytes"]:
        findings.append(
            _finding(
                area="raw_projection_backup",
                metric="raw_projection_backup_total",
                observed_bytes=raw_projection_backup_total,
                budget_bytes=budgets["raw_projection_backup_total_max_bytes"],
                message="Legacy raw-vault projection backups exceed the retained disk budget.",
                handling="manual_required",
                user_action=(
                    "Run python3 scripts/audit_raw_projection_backups.py --json, review "
                    "the manifest/inventory/recovery evidence, then request explicit deletion approval."
                ),
            )
        )
    if raw_events_size > budgets["raw_events_max_bytes"]:
        findings.append(
            _finding(
                area="raw_events",
                metric="raw_events_size",
                observed_bytes=raw_events_size,
                budget_bytes=budgets["raw_events_max_bytes"],
                message="raw_events.db exceeds disk budget.",
                handling="manual_required",
                user_action=(
                    "Review raw retention and use mnemos data delete --dry-run "
                    "--scope <scope> before any confirmed deletion."
                ),
            )
        )
    if (
        raw_growth_per_day is not None
        and raw_growth_per_day > budgets["raw_events_growth_max_bytes_per_day"]
    ):
        findings.append(
            _finding(
                area="raw_events",
                metric="raw_events_growth_per_day",
                observed_bytes=int(raw_growth_per_day),
                budget_bytes=budgets["raw_events_growth_max_bytes_per_day"],
                message="raw_events.db growth rate exceeds disk budget.",
                handling="manual_required",
                user_action=(
                    "Pause high-volume ingestion, inspect raw source scope, and run "
                    "mnemos data delete --dry-run --scope <scope> only after "
                    "confirming the retention decision."
                ),
            )
        )

    if update_state:
        state = dict(state)
        if raw_should_update:
            state["raw_events"] = {"size_bytes": raw_events_size, "sampled_at": now}
        if snapshot_should_update:
            state["snapshots"] = {"size_bytes": snapshot_total, "sampled_at": now}
        state["schema_version"] = SCHEMA_VERSION
        _write_state(state_path, state)

    auto_heal_available = sum(
        1 for finding in findings if finding.handling == "auto_heal_safe"
    )
    manual_required = sum(1 for finding in findings if finding.handling == "manual_required")
    status = "degraded" if findings else "ok"
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "database_dir": str(database_dir),
        "snapshot_dir": str(snapshot_dir),
        "temp_dir": tempfile.gettempdir(),
        "state_path": str(state_path),
        "budgets": budgets,
        "wal": {
            "file_count": len(wal_entries),
            "total_bytes": wal_total,
            "largest": max(wal_entries, key=lambda item: item["size_bytes"], default={}),
            "files": wal_entries[:20],
        },
        "temp": {
            "file_count": len(temp_entries),
            "total_bytes": temp_total,
            "stale_bytes": stale_temp_total,
            "stale_after_seconds": int(stale_seconds),
            "files": temp_entries[:20],
        },
        "snapshots": {
            "total_bytes": snapshot_total,
            "growth_bytes_per_day": (
                int(snapshot_growth_per_day)
                if snapshot_growth_per_day is not None
                else None
            ),
            "growth_sample_state": (
                "ready" if snapshot_growth_per_day is not None else "warming_up"
            ),
        },
        "raw_projection_backups": {
            "directory_count": len(raw_projection_backup_dirs),
            "total_bytes": raw_projection_backup_total,
            "directories": [str(path) for path in raw_projection_backup_dirs[:20]],
        },
        "raw_events": {
            "path": str(raw_events_path),
            "exists": raw_events_path.exists(),
            "size_bytes": raw_events_size,
            "growth_bytes_per_day": (
                int(raw_growth_per_day) if raw_growth_per_day is not None else None
            ),
            "growth_sample_state": (
                "ready" if raw_growth_per_day is not None else "warming_up"
            ),
        },
        "findings": [finding.as_dict() for finding in findings],
        "auto_heal_available": auto_heal_available,
        "manual_required": manual_required,
        "user_notification": {
            "surface": "python3 mnemos_cli.py health --json",
            "auto_heal": "WAL checkpoint and stale Mnemos temp cleanup are safe repair actions.",
            "manual": (
                "Snapshot pruning and raw_events deletion require user confirmation "
                "because they can remove history or raw evidence."
            ),
        },
        "repair_actions": [
            "python3 scripts/repair_sqlite_disk_budget.py --dry-run",
            "python3 scripts/repair_sqlite_disk_budget.py --apply --wal --temp",
            "python3 mnemos_cli.py health --json",
        ],
    }
    if findings:
        report["error"] = "sqlite disk budget exceeded"
    return report


def repair_sqlite_disk_budget(
    config: Any,
    *,
    apply: bool = False,
    repair_wal: bool = True,
    repair_temp: bool = True,
) -> dict[str, Any]:
    """Checkpoint WAL files and delete stale Mnemos temp files when requested."""

    now = time.time()
    database_dir = _database_dir(config)
    stale_seconds = _cfg_float(config, "storage.disk_budget.temp_stale_minutes", 60) * 60
    actions: list[dict[str, Any]] = []

    if repair_wal and database_dir.exists():
        for wal_path in sorted(database_dir.glob("*.db-wal")):
            db_path = Path(str(wal_path)[:-4])
            action = {
                "type": "wal_checkpoint_truncate",
                "path": str(db_path),
                "wal_path": str(wal_path),
                "before_bytes": _safe_stat_size(wal_path),
                "applied": False,
                "status": "planned",
            }
            if apply:
                try:
                    with sqlite3.connect(str(db_path), timeout=10) as conn:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    action["after_bytes"] = _safe_stat_size(wal_path)
                    action["applied"] = True
                    action["status"] = "ok"
                except sqlite3.Error as exc:
                    action["status"] = "failed"
                    action["error"] = str(exc)
            actions.append(action)

    if repair_temp:
        for path in _temp_files():
            age_seconds = _mtime_age_seconds(path, now)
            if age_seconds < stale_seconds:
                continue
            action = {
                "type": "delete_stale_temp",
                "path": str(path),
                "before_bytes": _safe_stat_size(path),
                "age_seconds": int(age_seconds),
                "applied": False,
                "status": "planned",
            }
            if apply:
                try:
                    path.unlink()
                    action["applied"] = True
                    action["status"] = "ok"
                except OSError as exc:
                    action["status"] = "failed"
                    action["error"] = str(exc)
            actions.append(action)

    failed = [action for action in actions if action.get("status") == "failed"]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "apply" if apply else "dry_run",
        "ok": not failed,
        "actions": actions,
        "applied_count": sum(1 for action in actions if action.get("applied")),
        "failed_count": len(failed),
        "manual_scope": {
            "snapshots": "manual_required",
            "raw_events": "manual_required",
        },
    }
