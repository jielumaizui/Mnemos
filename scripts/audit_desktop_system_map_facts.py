#!/usr/bin/env python3
"""Audit Desktop system-map facts for a current repo-state contract."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FACTS_PATH = Path.home() / "Desktop" / "mnemos系统图谱" / "99-代码扫描-facts.json"
SCHEMA_VERSION = "mnemos.system_map_current_state.v1"
QUICK_COMMAND = "python3 run_tests.py quick"
LOCAL_GATES_COMMAND = "python3 scripts/run_local_gates.py"
REQUIRED_SUCCESS_COMMANDS = (QUICK_COMMAND,)


@dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    path: str


def current_git_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("facts payload must be a JSON object")
    return payload


def _command_entries(current_state: dict[str, Any]) -> Iterable[dict[str, Any]]:
    commands = current_state.get("commands")
    if not isinstance(commands, list):
        return ()
    return (entry for entry in commands if isinstance(entry, dict))


def _success_command_map(current_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for entry in _command_entries(current_state):
        command = entry.get("command")
        if not isinstance(command, str):
            continue
        exit_code = entry.get("exit_code")
        summary = entry.get("summary")
        if exit_code == 0 and isinstance(summary, str) and summary.strip():
            entries[command] = entry
    return entries


def audit_payload(payload: dict[str, Any], *, current_commit: str, facts_path: Path) -> list[Finding]:
    findings: list[Finding] = []
    path = str(facts_path)
    current_state = payload.get("current_state")
    if not isinstance(current_state, dict):
        return [
            Finding(
                "missing_current_state",
                "99-代码扫描-facts.json must include top-level current_state metadata.",
                path,
            )
        ]

    schema_version = current_state.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        findings.append(
            Finding(
                "invalid_current_state_schema",
                f"current_state.schema_version must be {SCHEMA_VERSION}.",
                path,
            )
        )

    repo_git_commit = current_state.get("repo_git_commit")
    if repo_git_commit != current_commit:
        findings.append(
            Finding(
                "stale_current_state_commit",
                "current_state.repo_git_commit must match the current repository HEAD.",
                path,
            )
        )

    generated_at = current_state.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        findings.append(
            Finding(
                "missing_current_state_timestamp",
                "current_state.generated_at must be a non-empty timestamp.",
                path,
            )
        )

    notice = current_state.get("historical_snapshot_notice")
    if not isinstance(notice, str) or "current_state" not in notice:
        findings.append(
            Finding(
                "missing_historical_snapshot_notice",
                "current_state must explain that historical scan claims are not current-state evidence.",
                path,
            )
        )

    success_commands = _success_command_map(current_state)
    for command in REQUIRED_SUCCESS_COMMANDS:
        if command not in success_commands:
            findings.append(
                Finding(
                    "missing_successful_validation_command",
                    f"current_state.commands must include a successful `{command}` result.",
                    path,
                )
            )
    quick_entry = success_commands.get(QUICK_COMMAND)
    if (
        repo_git_commit == current_commit
        and quick_entry is not None
        and quick_entry.get("tested_code_commit") != current_commit
    ):
        findings.append(
            Finding(
                "stale_quick_validation_commit",
                "successful quick evidence must name the current repository commit.",
                path,
            )
        )

    return findings


def build_payload(
    *,
    facts_path: Path,
    repo_root: Path,
    current_commit: str | None,
    findings: list[Finding],
    skipped: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "mnemos.desktop_system_map_facts_audit.v1",
        "ok": not findings,
        "skipped": skipped,
        "facts_path": str(facts_path),
        "repo_root": str(repo_root),
        "current_commit": current_commit,
        "findings": [asdict(finding) for finding in findings],
        "by_rule": {
            rule: sum(1 for finding in findings if finding.rule == rule)
            for rule in sorted({finding.rule for finding in findings})
        },
    }


def run_audit(*, facts_path: Path, repo_root: Path, require_present: bool) -> dict[str, Any]:
    if not facts_path.exists():
        findings = []
        if require_present:
            findings.append(
                Finding(
                    "missing_facts_path",
                    "Desktop system-map facts file was not found.",
                    str(facts_path),
                )
            )
        return build_payload(
            facts_path=facts_path,
            repo_root=repo_root,
            current_commit=None,
            findings=findings,
            skipped=not require_present,
        )

    current_commit = current_git_commit(repo_root)
    payload = _load_json(facts_path)
    findings = audit_payload(payload, current_commit=current_commit, facts_path=facts_path)
    return build_payload(
        facts_path=facts_path,
        repo_root=repo_root,
        current_commit=current_commit,
        findings=findings,
    )


def _print_text(payload: dict[str, Any]) -> None:
    if payload["skipped"]:
        print(f"Desktop system-map facts audit skipped: {payload['facts_path']} not found.")
        return
    if payload["ok"]:
        print("Desktop system-map facts audit passed.")
        return
    print("Desktop system-map facts audit found issue(s):")
    for finding in payload["findings"]:
        print(f"- {finding['path']} [{finding['rule']}] {finding['message']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts-path", type=Path, default=DEFAULT_FACTS_PATH)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--require-present",
        action="store_true",
        help="Fail when the Desktop system-map facts file is missing.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    try:
        payload = run_audit(
            facts_path=args.facts_path,
            repo_root=args.repo_root,
            require_present=args.require_present,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        payload = build_payload(
            facts_path=args.facts_path,
            repo_root=args.repo_root,
            current_commit=None,
            findings=[
                Finding(
                    "audit_error",
                    str(exc),
                    str(args.facts_path),
                )
            ],
        )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_text(payload)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
