#!/usr/bin/env python3
"""Run an isolated temp-home install lifecycle probe."""

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

from core.setup.install_lifecycle import InstallLifecycleManager, build_install_lifecycle_health
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
            "vaults": {
                "mnemos": {"path": str(self.wiki_dir), "enabled": True},
                "raw": {"path": str(self.obsidian_vault_path), "enabled": True},
            }
        }

    def prepare(self) -> None:
        self.database_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        init_vaults(self.wiki_dir, self.obsidian_vault_path)

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


def _run_probe(home: Path) -> dict[str, Any]:
    cfg = ProbeConfig(home)
    cfg.prepare()
    manager = InstallLifecycleManager(cfg)
    dry_run = manager.run_setup(dry_run=True)
    applied = manager.run_setup(dry_run=False, auto_setup_args=None)
    health = build_install_lifecycle_health(cfg)
    return {
        "schema_version": "mnemos.install_probe.v1",
        "ok": (
            dry_run.status in {"configuring", "installed_partial", "installed_ready"}
            and applied.status == "installed_ready"
            and health["status"] == "ok"
            and Path(applied.state_path).exists()
        ),
        "home": str(home),
        "dry_run_status": dry_run.status,
        "applied_status": applied.status,
        "health_status": health["status"],
        "state_path": applied.state_path,
        "action_ledger_ref": applied.action_ledger_ref,
        "repair_actions": list(applied.repair_actions),
        "errors": list(applied.errors) + list(health.get("errors") or []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmp-home", action="store_true", help="run in an isolated temporary home")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args(argv)
    if not args.tmp_home:
        parser.error("--tmp-home is required to avoid touching the real user home")
    with tempfile.TemporaryDirectory(prefix="mnemos-install-probe-") as tmp:
        payload = _run_probe(Path(tmp))
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["ok"]:
        print("Install lifecycle probe passed")
    else:
        print("Install lifecycle probe failed")
        for error in payload["errors"]:
            print(f"- {error}")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
