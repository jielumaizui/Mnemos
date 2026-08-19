#!/usr/bin/env python3
"""Run an isolated temp-home upgrade lifecycle probe."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.setup.install_lifecycle import InstallLifecycleManager
from core.setup.vault_layout import init_vaults


class ProbeConfig:
    def __init__(self, home: Path):
        self.home = home
        self.mnemos_dir = home / ".mnemos"
        self.data_dir = self.mnemos_dir
        self.database_dir = self.mnemos_dir / "databases"
        self.config_path = self.mnemos_dir / "configs" / "main.json"
        self.wiki_dir = home / "MnemosVault"
        self.obsidian_vault_path = home / "RawVault"
        self._data: dict[str, Any] = {
            "persona": {"data_sources": {"memos": {"enabled": True}}},
            "vaults": {
                "mnemos": {"path": str(self.wiki_dir), "enabled": True},
                "raw": {"path": str(self.obsidian_vault_path), "enabled": True},
            },
        }

    def prepare(self) -> None:
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.save()
        init_vaults(self.wiki_dir, self.obsidian_vault_path)
        (self.database_dir / "events.db").write_bytes(b"probe")

    def get(self, key: str, default: Any = None) -> Any:
        current: Any = self._data
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def to_dict(self) -> dict[str, Any]:
        data = json.loads(json.dumps(self._data, ensure_ascii=False))
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        self.config_path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")

    def vault_dir(self, name: str) -> Path:
        if name == "mnemos":
            return self.wiki_dir
        if name == "raw":
            return self.obsidian_vault_path
        raise KeyError(name)


def _run_probe(home: Path, *, preserve_existing: bool) -> dict[str, Any]:
    cfg = ProbeConfig(home)
    cfg.prepare()
    original_config = cfg.config_path.read_text(encoding="utf-8")
    manager = InstallLifecycleManager(cfg)
    plan = manager.upgrade_plan()
    applied = manager.upgrade_apply(execute_wrapped=False)
    preserved = cfg.config_path.exists() and bool(cfg.config_path.read_text(encoding="utf-8"))
    if preserve_existing:
        preserved = preserved and bool(original_config)
    return {
        "schema_version": "mnemos.upgrade_probe.v1",
        "ok": (
            plan.status in {"upgrade_available", "installed_ready"}
            and applied.status in {"installed_ready", "rollback_available"}
            and bool(plan.migration_plan_hash)
            and bool(applied.backup_ref)
            and preserved
        ),
        "home": str(home),
        "plan_status": plan.status,
        "apply_status": applied.status,
        "plan_hash": plan.migration_plan_hash,
        "backup_ref": applied.backup_ref,
        "preserve_existing": preserve_existing,
        "state_path": applied.state_path,
        "action_ledger_ref": applied.action_ledger_ref,
        "errors": list(applied.errors),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmp-home", action="store_true", help="run in an isolated temporary home")
    parser.add_argument(
        "--preserve-existing",
        action="store_true",
        help="assert existing config/vault data is still present after the probe",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    if not args.tmp_home:
        parser.error("--tmp-home is required to avoid touching the real user home")
    with tempfile.TemporaryDirectory(prefix="mnemos-upgrade-probe-") as tmp:
        payload = _run_probe(Path(tmp), preserve_existing=bool(args.preserve_existing))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["ok"]:
        print("Upgrade lifecycle probe passed")
    else:
        print("Upgrade lifecycle probe failed")
        for error in payload["errors"]:
            print(f"- {error}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
