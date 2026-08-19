"""Secrets/keyring diagnostics command."""

from __future__ import annotations

import json
from typing import Any

from core.cli.helpers import _get_config
from core.ops.keyring_doctor import (
    build_keyring_doctor_report,
    format_keyring_doctor_text,
)


def _accept_env_fallback(config: Any) -> None:
    setter = getattr(config, "set", None)
    saver = getattr(config, "save", None)
    if not callable(setter) or not callable(saver):
        raise RuntimeError("config object does not support persistent updates")
    setter("security.accept_env_secret_fallback", True)
    saver()


def cmd_secrets(args: Any) -> int:
    """Run secret storage diagnostics."""
    if getattr(args, "secrets_cmd", "") != "doctor":
        print("Usage: mnemos secrets doctor [--json] [--accept-env-fallback]")
        return 2

    config = _get_config()
    applied_actions: list[str] = []
    if getattr(args, "accept_env_fallback", False):
        pre_accept_report = build_keyring_doctor_report(config)
        plaintext_count = int(
            pre_accept_report.get("secret_inventory_plaintext_count", 0) or 0
        )
        inventory_error = pre_accept_report.get("secret_inventory_error")
        if plaintext_count > 0 or inventory_error:
            pre_accept_report["applied_actions"] = applied_actions
            pre_accept_report.setdefault("warnings", []).append(
                "env fallback was not accepted because plaintext secret safety was not proven"
            )
            if getattr(args, "json", False):
                print(json.dumps(pre_accept_report, ensure_ascii=False, indent=2))
            else:
                print(format_keyring_doctor_text(pre_accept_report), end="")
            return 1
        _accept_env_fallback(config)
        applied_actions.append("security.accept_env_secret_fallback=true")

    report = build_keyring_doctor_report(config)
    if applied_actions:
        report["applied_actions"] = applied_actions

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_keyring_doctor_text(report), end="")

    return 0 if int(report.get("secret_inventory_plaintext_count", 0) or 0) == 0 else 1
