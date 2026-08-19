"""Redacted secret-like configuration inventory."""

from __future__ import annotations

import re
from typing import Any, Mapping

SCHEMA_VERSION = "mnemos.secret_inventory.v1"
REFERENCE_PREFIXES = ("env:", "keyring:", "keyref:")
_SECRET_LEAF_RE = re.compile(
    r"(api[_-]?key|(^|[_-])token$|secret|password|credential|bearer|key_source)",
    re.IGNORECASE,
)
_SAFE_SECRET_NAME_RE = re.compile(
    r"(token_budget|(^|[_-])tokens$|token_limit|max_tokens|response_tokens|tokenizer|api_key_env)",
    re.IGNORECASE,
)


def _is_secret_leaf(name: str) -> bool:
    leaf = name.lower()
    if _SAFE_SECRET_NAME_RE.search(leaf):
        return False
    return bool(_SECRET_LEAF_RE.search(leaf))


def _iter_secret_fields(
    value: Any,
    prefix: str = "",
    *,
    secret_context: bool = False,
) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            child_is_secret = _is_secret_leaf(str(key))
            if isinstance(child, str) and child and (secret_context or child_is_secret):
                findings.append((child_prefix, child))
            else:
                findings.extend(
                    _iter_secret_fields(
                        child,
                        child_prefix,
                        secret_context=secret_context or child_is_secret,
                    )
                )
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            child_prefix = f"{prefix}[{idx}]"
            if isinstance(child, str) and child and secret_context:
                findings.append((child_prefix, child))
            else:
                findings.extend(
                    _iter_secret_fields(
                        child,
                        child_prefix,
                        secret_context=secret_context,
                    )
                )
    elif isinstance(value, str) and value and secret_context:
        findings.append((prefix, value))
    return findings


def _classify_secret_value(value: str) -> tuple[str, dict[str, Any]]:
    if value.startswith(REFERENCE_PREFIXES):
        return "reference", {"source": value.split(":", 1)[0]}
    return "plaintext-risk", {"value_length": len(value)}


def build_secret_inventory(data: Mapping[str, Any]) -> dict[str, Any]:
    findings = []
    plaintext_count = 0
    reference_count = 0
    for path, value in _iter_secret_fields(data):
        status, evidence = _classify_secret_value(value)
        if status == "plaintext-risk":
            plaintext_count += 1
        elif status == "reference":
            reference_count += 1
        findings.append(
            {
                "path": path,
                "status": status,
                "value_present": bool(value),
                **evidence,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "findings": findings,
        "plaintext_count": plaintext_count,
        "reference_count": reference_count,
    }
