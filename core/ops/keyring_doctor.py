"""Keyring and env secret fallback diagnostics."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

SCHEMA_VERSION = "mnemos.keyring_doctor.v1"
KEYRING_POLICY = (
    "keyring: references are recommended for local secret storage. env: references "
    "are safe when no plaintext secret is stored in config, but they are a fallback "
    "with shell, launchd, process, and CI log exposure risks."
)


def probe_keyring() -> dict[str, Any]:
    """Probe the active Python process for a usable keyring backend."""
    try:
        import keyring  # type: ignore

        backend = keyring.get_keyring()
        backend_type = f"{type(backend).__module__}.{type(backend).__name__}"
        available = bool(backend) and backend_type != "keyring.backends.fail.Keyring"
        return {
            "available": available,
            "backend": backend_type,
            "error": None if available else f"keyring backend is not usable: {backend_type}",
        }
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        ImportError,
        AttributeError,
        sqlite3.Error,
    ) as exc:
        return {
            "available": False,
            "backend": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _cfg_get(config: Any, key: str, default: Any = None) -> Any:
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except (TypeError, ValueError, AttributeError):
            return default
    data = getattr(config, "_data", {})
    current: Any = data
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def _secret_reference_counts(secret_inventory: Mapping[str, Any]) -> dict[str, int]:
    counts = {"env": 0, "keyring": 0, "keyref": 0, "plaintext": 0, "unknown": 0}
    for finding in secret_inventory.get("findings", []):
        if not isinstance(finding, Mapping):
            counts["unknown"] += 1
            continue
        status = finding.get("status")
        if status == "plaintext-risk":
            counts["plaintext"] += 1
            continue
        if status == "reference":
            source = str(finding.get("source", "unknown"))
            if source in {"env", "keyring", "keyref"}:
                counts[source] += 1
            else:
                counts["unknown"] += 1
    if not counts["plaintext"]:
        counts["plaintext"] = int(secret_inventory.get("plaintext_count", 0) or 0)
    return counts


def build_keyring_doctor_report(
    config: Any | None = None,
    *,
    keyring_info: Mapping[str, Any] | None = None,
    secret_inventory: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted keyring/env-fallback decision report."""
    if keyring_info is None:
        keyring_info = probe_keyring()
    if secret_inventory is None:
        data = getattr(config, "_data", {}) if config is not None else {}
        if isinstance(data, Mapping):
            from core.privacy.secret_inventory import build_secret_inventory

            secret_inventory = build_secret_inventory(data)
            secret_inventory = {**secret_inventory, "error": None}
        else:
            secret_inventory = {
                "findings": [],
                "plaintext_count": 0,
                "reference_count": 0,
                "error": "config object has no loaded mapping data",
            }

    reference_counts = _secret_reference_counts(secret_inventory)
    plaintext_count = int(secret_inventory.get("plaintext_count", 0) or 0)
    inventory_error = bool(secret_inventory.get("error"))
    keyring_available = bool(keyring_info.get("available"))
    env_fallback_accepted = bool(
        _cfg_get(config, "security.accept_env_secret_fallback", False)
    )
    no_plaintext = plaintext_count == 0 and not inventory_error
    uses_env = reference_counts["env"] > 0
    uses_keyring = reference_counts["keyring"] > 0 or reference_counts["keyref"] > 0

    if keyring_available:
        status = "ok"
        risk_level = "best"
        summary = "keyring backend is available for local secret references"
        safe_but_not_best = False
        requires_user_choice = False
    elif no_plaintext and env_fallback_accepted:
        status = "accepted"
        risk_level = "safe_but_not_best"
        summary = (
            "keyring is unavailable; env fallback is explicitly accepted and no "
            "plaintext secret is stored in config"
        )
        safe_but_not_best = True
        requires_user_choice = False
    elif no_plaintext:
        status = "warning"
        risk_level = "safe_but_not_best"
        summary = (
            "keyring is unavailable; current config is safe from plaintext secret "
            "storage, but env fallback is not yet explicitly accepted"
        )
        safe_but_not_best = True
        requires_user_choice = True
    else:
        status = "warning"
        risk_level = "plaintext_or_unknown_secret_inventory"
        summary = (
            "keyring is unavailable and plaintext secret safety could not be proven"
        )
        safe_but_not_best = False
        requires_user_choice = True

    warnings: list[str] = []
    if not keyring_available:
        if status == "accepted":
            warnings.append(
                "keyring unavailable; env fallback explicitly accepted; safe but not best"
            )
        elif risk_level == "safe_but_not_best":
            warnings.append(
                "keyring unavailable; env fallback is safe for this config but not best"
            )
        else:
            warnings.append(
                "keyring unavailable; prove plaintext_count=0 before accepting env fallback"
            )

    repair_actions = [
        "Install keyring in the active Python environment: python3 -m pip install 'keyring>=25,<26'",
        "macOS: grant Keychain access to the active Python interpreter when prompted",
        "Prefer keyring references such as: python3 mnemos_cli.py config --set llm.api_key_source=keyring:mnemos/llm",
    ]
    if not keyring_available and not env_fallback_accepted:
        repair_actions.extend(
            [
                "If this deployment intentionally uses env vars, run: "
                "python3 mnemos_cli.py secrets doctor --accept-env-fallback",
                "Equivalent config flag: python3 mnemos_cli.py config --set security.accept_env_secret_fallback=true",
            ]
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "ok": bool(no_plaintext and (keyring_available or env_fallback_accepted)),
        "summary": summary,
        "policy": KEYRING_POLICY,
        "risk_level": risk_level,
        "safe_but_not_best": safe_but_not_best,
        "requires_user_choice": requires_user_choice,
        "env_fallback_accepted": env_fallback_accepted,
        "uses_env_references": uses_env,
        "uses_keyring_references": uses_keyring,
        "secret_reference_counts": reference_counts,
        "secret_inventory_plaintext_count": plaintext_count,
        "secret_inventory_error": secret_inventory.get("error"),
        "keyring": {
            "available": keyring_available,
            "backend": keyring_info.get("backend"),
            "error": keyring_info.get("error"),
        },
        "warnings": warnings,
        "repair_actions": repair_actions,
        "recommended_source_order": ["keyring:", "keyref:", "env:"],
    }


def format_keyring_doctor_text(report: Mapping[str, Any]) -> str:
    """Render a compact human report for `mnemos secrets doctor`."""
    keyring = report.get("keyring", {})
    if not isinstance(keyring, Mapping):
        keyring = {}
    counts = report.get("secret_reference_counts", {})
    if not isinstance(counts, Mapping):
        counts = {}
    lines = [
        "Mnemos Secrets Doctor",
        "=" * 40,
        f"schema: {report.get('schema_version', SCHEMA_VERSION)}",
        f"status: {report.get('status', 'unknown')}",
        f"summary: {report.get('summary', '')}",
        f"keyring_available: {bool(keyring.get('available'))}",
    ]
    if keyring.get("backend"):
        lines.append(f"keyring_backend: {keyring['backend']}")
    if keyring.get("error"):
        lines.append(f"keyring_error: {keyring['error']}")
    lines.extend(
        [
            f"env_fallback_accepted: {bool(report.get('env_fallback_accepted'))}",
            f"safe_but_not_best: {bool(report.get('safe_but_not_best'))}",
            (
                "secret_reference_counts: "
                f"env={int(counts.get('env', 0) or 0)}, "
                f"keyring={int(counts.get('keyring', 0) or 0)}, "
                f"keyref={int(counts.get('keyref', 0) or 0)}, "
                f"plaintext={int(counts.get('plaintext', 0) or 0)}"
            ),
        ]
    )
    warnings = report.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.append("warnings:")
        lines.extend(f"  - {item}" for item in warnings)
    actions = report.get("repair_actions", [])
    if isinstance(actions, list) and actions:
        lines.append("repair_actions:")
        lines.extend(f"  - {item}" for item in actions)
    return "\n".join(lines) + "\n"
