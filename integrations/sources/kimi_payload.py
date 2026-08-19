"""Reversible decoding helpers for native Kimi JSONL artifacts."""

from __future__ import annotations

import base64
import json
import logging
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.sync_framework.agent_source import NativeSourceContractError
from core.ops.durable_io import read_native_bytes

logger = logging.getLogger(__name__)


class _DuplicateJsonKeyError(ValueError):
    pass


class _NativeJsonRecord(dict):
    """Runtime mapping carrying lossless equality and exact source-line evidence."""

    json_value_key: tuple
    source_line_ref: Dict[str, Any]


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


def _runtime_json_float(value: str) -> Any:
    parsed = float(value)
    if math.isfinite(parsed):
        return parsed
    return {
        "_mnemos_json_number": value,
        "decode_warning": "non_finite_runtime_float",
    }


def _strict_json_object(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKeyError(key)
        value[key] = item
    return value


def _json_structure_error(value: Any, *, max_depth: int = 256) -> str:
    """Validate decoded scalars iteratively so adversarial nesting cannot abort."""
    pending: List[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > max_depth:
            return "json_nesting_too_deep"
        if isinstance(item, str):
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                return "invalid_unicode_scalar"
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
    return ""


def _lossless_json_value_key(value: Any) -> tuple:
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, Decimal):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return (
            "array",
            tuple(_lossless_json_value_key(item) for item in value),
        )
    if isinstance(value, dict):
        return (
            "object",
            tuple(
                (key, _lossless_json_value_key(item))
                for key, item in sorted(value.items())
            ),
        )
    return ("non_json_value", type(value).__name__, repr(value))


def native_json_value_key(value: Any) -> Optional[tuple]:
    key = getattr(value, "json_value_key", None)
    return key if isinstance(key, tuple) else None


def native_json_line_ref(value: Any) -> Optional[Dict[str, Any]]:
    ref = getattr(value, "source_line_ref", None)
    return dict(ref) if isinstance(ref, dict) else None


def read_native_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Return one mapping per non-empty line without dropping undecodable bytes."""
    try:
        lines = read_native_bytes(path).splitlines()
    except OSError:
        raise NativeSourceContractError(
            "native_kimi_jsonl_read_failed"
        ) from None

    records: List[Dict[str, Any]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            records.append(
                {
                    "_mnemos_raw_event_ref": {
                        "source_file": str(path),
                        "line_number": line_number,
                        "raw_base64": base64.b64encode(raw_line).decode("ascii"),
                        "raw_encoding": "base64",
                        "decode_error": "invalid_utf8",
                    }
                }
            )
            continue
        try:
            value = json.loads(
                text,
                parse_constant=_reject_non_json_constant,
                parse_float=_runtime_json_float,
                object_pairs_hook=_strict_json_object,
            )
        except _DuplicateJsonKeyError:
            records.append(
                {
                    "_mnemos_raw_event_ref": {
                        "source_file": str(path),
                        "line_number": line_number,
                        "raw_text": text,
                        "decode_error": "duplicate_json_key",
                    }
                }
            )
            continue
        except (json.JSONDecodeError, RecursionError, ValueError):
            records.append(
                {
                    "_mnemos_raw_event_ref": {
                        "source_file": str(path),
                        "line_number": line_number,
                        "raw_text": text,
                        "decode_error": "invalid_json",
                    }
                }
            )
            continue
        structure_error = _json_structure_error(value)
        if structure_error:
            records.append(
                {
                    "_mnemos_raw_event_ref": {
                        "source_file": str(path),
                        "line_number": line_number,
                        "raw_text": text,
                        "decode_error": structure_error,
                    }
                }
            )
            continue
        if not isinstance(value, dict):
            records.append(
                {
                    "_mnemos_raw_event_ref": {
                        "source_file": str(path),
                        "line_number": line_number,
                        "raw_text": text,
                        "decode_error": "non_object_json",
                    }
                }
            )
            continue
        lossless_value = json.loads(
            text,
            parse_constant=_reject_non_json_constant,
            parse_float=Decimal,
            parse_int=Decimal,
            object_pairs_hook=_strict_json_object,
        )
        record = _NativeJsonRecord(value)
        record.json_value_key = _lossless_json_value_key(lossless_value)
        record.source_line_ref = {
            "source_file": str(path),
            "line_number": line_number,
            "raw_text": text,
            "raw_encoding": "utf-8",
        }
        records.append(record)
    return records
