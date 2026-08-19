"""Pure normalization and trigger matching for policy patches."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping


VALID_SEVERITIES = {"critical", "high", "medium", "low"}
MAX_TRIGGER_TERM_LENGTH = 64
MAX_TRIGGER_WORDS = 6
MAX_TRIGGER_CJK_CHARS = 12
SENTENCE_PUNCTUATION = frozenset("，,。！？!?；;：:\n\r")


def _cfg_get(cfg: Any, key: str, default: Any) -> Any:
    try:
        return cfg.get(key, default)
    except (AttributeError, TypeError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_id(*parts: str) -> str:
    raw = ":".join(str(part or "") for part in parts).encode("utf-8")
    digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:16]
    return f"policy-{digest}"


def policy_patch_id(
    source_type: str,
    source_id: str,
    task_type: str,
    subtype: str,
    content: str,
) -> str:
    """Return the canonical identity used by ``PolicyPatchStore.propose``."""

    return _stable_id(source_type, source_id, task_type, subtype, content)


def _clean(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def _norm_severity(value: str) -> str:
    severity = _clean(value, "medium").lower()
    return severity if severity in VALID_SEVERITIES else "medium"


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _first_text(lesson: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = lesson.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_trigger(lesson: Mapping[str, Any]) -> str:
    for key in ("trigger", "trigger_keyword", "trigger_keywords"):
        value = lesson.get(key)
        values = _string_list(value)
        sanitized = _sanitize_trigger_terms(values)
        if sanitized:
            return _serialize_trigger_terms(sanitized)
    return ""


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                loaded = json.loads(text)
            except json.JSONDecodeError:
                loaded = None
            if isinstance(loaded, list):
                return [str(item) for item in loaded if str(item or "").strip()]
        return [text]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _sanitize_trigger_terms(values: list[str]) -> list[str]:
    """Keep unique bounded activation terms and discard generated explanations."""

    sanitized: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = str(value or "").strip()
        key = term.lower()
        if not _eligible_trigger_term(term) or key in seen:
            continue
        seen.add(key)
        sanitized.append(term)
    return sanitized


def _serialize_trigger_terms(values: list[str]) -> str:
    if not values:
        return ""
    return values[0] if len(values) == 1 else _json_dumps(values)


def _trigger_matches(trigger: str, content: str, context_text: str) -> bool:
    """Compatibility predicate; patch content is intentionally not match input."""

    del content
    return bool(_matched_trigger_terms(trigger, context_text))


def _matched_trigger_terms(trigger: str, context_text: str) -> list[str]:
    """Return only trigger terms proven to match the current task context."""

    normalized_context = context_text.lower()
    return [
        token.strip()
        for token in _string_list(trigger)
        if _eligible_trigger_term(token)
        and _term_matches(token, normalized_context, "")
    ]


def _eligible_trigger_term(token: str) -> bool:
    """Reject generated explanations that cannot be stable task triggers."""

    term = token.strip()
    if not term or len(term) > MAX_TRIGGER_TERM_LENGTH:
        return False
    if len(term.split()) > MAX_TRIGGER_WORDS:
        return False
    if any(char in SENTENCE_PUNCTUATION for char in term):
        return False
    cjk_count = sum(1 for char in term if "\u4e00" <= char <= "\u9fff")
    return cjk_count <= MAX_TRIGGER_CJK_CHARS


def _term_matches(token: str, normalized_context: str, normalized_content: str) -> bool:
    """Match context only; the content argument remains for call compatibility."""

    del normalized_content
    term = token.strip().lower()
    if not term:
        return False
    if any("\u4e00" <= char <= "\u9fff" for char in term):
        if term in normalized_context:
            return True
        cjk_segments = re.findall(r"[\u4e00-\u9fff]+", term)
        cjk_text = "".join(cjk_segments)
        if len(cjk_text) < 4:
            return False
        chunks = [cjk_text[i : i + 2] for i in range(0, len(cjk_text) - 1, 2)]
        matched = sum(1 for chunk in chunks if chunk in normalized_context)
        return matched >= min(2, len(chunks))

    left_boundary = r"(?<![a-z0-9_])" if term[0].isalnum() or term[0] == "_" else ""
    right_boundary = r"(?![a-z0-9_])" if term[-1].isalnum() or term[-1] == "_" else ""
    if re.search(left_boundary + re.escape(term) + right_boundary, normalized_context):
        return True
    return False
