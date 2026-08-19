"""Narrow persistence redaction for user-owned local cognition artifacts.

Mnemos is a local, single-user system.  This module therefore does not add
encryption or broad content suppression: it only replaces obvious personal
identifiers, payment-card numbers and credentials before derived cognition is
written to Wiki/SQLite surfaces.  Audit metadata contains types and counts,
never the matched literals.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Mapping, Sequence

REDACTION_POLICY = "pii_credentials_only_v1"

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?" r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)(?P<label>\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|"
    r"secret|password|passwd|pwd|private[_ -]?key|authorization)\b\s*[:=]\s*)"
    r"(?P<quote>['\"]?)(?P<value>[^\s,;'\"]{4,})(?P=quote)"
)
_BEARER_TOKEN = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+\-/=]{12,}")
_KNOWN_API_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{12,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{30,}"
    r")(?![A-Za-z0-9])"
)
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@" r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_PHONE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?86[- ]?)?1[3-9]\d{9}(?![A-Za-z0-9])"
)
_PRC_ID = re.compile(
    r"(?<![A-Za-z0-9])(?:\d{17}[0-9Xx]|\d{15})(?![A-Za-z0-9])"
)
# A valid card number must stand on its own.  Digit runs embedded in opaque
# hashes/revision IDs can satisfy Luhn by chance and are provenance, not PII.
_CARD_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])\d(?:[ -]?\d){12,18}(?![A-Za-z0-9])"
)
_COMPACT_TIMESTAMP_SUFFIX = re.compile(
    r"(?:^|[ -])(?P<date>\d{8})[- ]?(?P<time>\d{6})$"
)
_LABELED_CONTACT = re.compile(
    r"(?i)(?P<label>(?:\b(?:email|phone|mobile|id[_ -]?card|bank[_ -]?card)\b|"
    r"邮箱|电子邮箱|手机号?|联系电话|电话|身份证(?:号码|号)?|"
    r"银行卡(?:号)?)\s*[:=]\s*)(?P<value>[^\s,;，；\n]{2,})"
)
_LABELED_PERSONAL = re.compile(
    r"(?i)(?P<label>(?:\b(?:full[_ -]?name|home[_ -]?address)\b|"
    r"真实姓名|姓名|家庭住址|住址)\s*[:=]\s*)"
    r"(?P<value>[^;；\n]{2,})"
)

_CREDENTIAL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "credential",
        "credentials",
        "password",
        "passwd",
        "pwd",
        "secret",
        "private_key",
    }
)
_PERSONAL_KEYS = frozenset(
    {
        "bank_card",
        "bank_card_number",
        "card_number",
        "email",
        "full_name",
        "home_address",
        "id_card",
        "id_number",
        "mobile",
        "phone",
        "phone_number",
        "姓名",
        "住址",
        "身份证号",
        "手机号",
        "邮箱",
        "银行卡号",
    }
)
_OPAQUE_IDENTITY_KEYS = frozenset(
    {
        # A target reference is an authorization identity, not display text.
        # Redacting a credential-shaped substring changes the addressed effect
        # and invalidates the action/effect IDs bound to the original target.
        "target_ref",
    }
)


@dataclass(frozen=True)
class RedactedContent:
    """A redacted value plus count-only audit evidence."""

    value: Any
    counts: tuple[tuple[str, int], ...] = ()
    policy: str = REDACTION_POLICY

    @property
    def total(self) -> int:
        return sum(count for _, count in self.counts)


def redact_persistence_value(value: Any, *, key: str = "") -> RedactedContent:
    """Recursively redact only PII/payment/credential literals.

    The returned structure is detached from the input.  Source/session IDs,
    paths, ordinary code and domain content are intentionally preserved.
    """

    counts: Counter[str] = Counter()
    redacted = _redact_value(value, key=str(key or ""), counts=counts)
    return RedactedContent(
        value=redacted,
        counts=tuple(sorted((name, int(count)) for name, count in counts.items())),
    )


def redact_fragment_in_place(fragment: Any) -> RedactedContent:
    """Redact the content-bearing fields of one live fragment before sinks."""

    fields = (
        "form",
        "title",
        "frontmatter",
        "background",
        "core_content",
        "boundaries",
        "anti_patterns",
        "related_concepts",
        "relations",
        "self_check_issues",
        "cross_agent_links",
        "keywords",
        "ai_expansion",
    )
    combined: Counter[str] = Counter()
    for field_name in fields:
        if not hasattr(fragment, field_name):
            continue
        result = redact_persistence_value(
            getattr(fragment, field_name),
            key=field_name,
        )
        setattr(fragment, field_name, result.value)
        combined.update(dict(result.counts))
    return RedactedContent(
        value=fragment,
        counts=tuple(sorted((name, int(count)) for name, count in combined.items())),
    )


def redact_fragments_in_place(fragments: Sequence[Any]) -> RedactedContent:
    """Apply the narrow persistence policy to a fragment sequence."""

    combined: Counter[str] = Counter()
    for fragment in fragments:
        result = redact_fragment_in_place(fragment)
        combined.update(dict(result.counts))
    return RedactedContent(
        value=fragments,
        counts=tuple(sorted((name, int(count)) for name, count in combined.items())),
    )


def _redact_value(value: Any, *, key: str, counts: Counter[str]) -> Any:
    normalized_key = key.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized_key in _OPAQUE_IDENTITY_KEYS:
        if isinstance(value, str) and _opaque_identity_contains_sensitive(value):
            raise ValueError(
                "opaque identity contains sensitive content and cannot be rewritten"
            )
        return value
    if normalized_key in _CREDENTIAL_KEYS and value not in (None, ""):
        counts["credential"] += 1
        return "[REDACTED:CREDENTIAL]"
    if normalized_key in _PERSONAL_KEYS and value not in (None, ""):
        counts["personal_identifier"] += 1
        return "[REDACTED:PERSONAL]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_value(
                child_value,
                key=str(child_key),
                counts=counts,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, tuple):
        return tuple(_redact_value(item, key=key, counts=counts) for item in value)
    if isinstance(value, list):
        return [_redact_value(item, key=key, counts=counts) for item in value]
    if isinstance(value, str):
        return _redact_text(value, counts)
    return value


def _redact_text(text: str, counts: Counter[str]) -> str:
    redacted = _sub_counted(
        _PRIVATE_KEY_BLOCK,
        text,
        "credential",
        counts,
        lambda _match: "[REDACTED:PRIVATE_KEY]",
    )
    redacted = _sub_counted(
        _CREDENTIAL_ASSIGNMENT,
        redacted,
        "credential",
        counts,
        lambda match: f"{match.group('label')}[REDACTED:CREDENTIAL]",
    )
    redacted = _sub_counted(
        _BEARER_TOKEN,
        redacted,
        "credential",
        counts,
        lambda match: f"{match.group(1)}[REDACTED:CREDENTIAL]",
    )
    redacted = _sub_counted(
        _KNOWN_API_KEY,
        redacted,
        "api_key",
        counts,
        lambda _match: "[REDACTED:API_KEY]",
    )
    redacted = _CARD_CANDIDATE.sub(lambda match: _redact_card(match, counts), redacted)
    redacted = _sub_counted(
        _LABELED_CONTACT,
        redacted,
        "personal_identifier",
        counts,
        lambda match: f"{match.group('label')}[REDACTED:PERSONAL]",
    )
    redacted = _sub_counted(
        _LABELED_PERSONAL,
        redacted,
        "personal_identifier",
        counts,
        lambda match: f"{match.group('label')}[REDACTED:PERSONAL]",
    )
    redacted = _sub_counted(
        _EMAIL,
        redacted,
        "email",
        counts,
        lambda _match: "[REDACTED:EMAIL]",
    )
    redacted = _sub_counted(
        _PHONE,
        redacted,
        "phone",
        counts,
        lambda _match: "[REDACTED:PHONE]",
    )
    redacted = _sub_counted(
        _PRC_ID,
        redacted,
        "government_id",
        counts,
        lambda _match: "[REDACTED:ID]",
    )
    return redacted


def _opaque_identity_contains_sensitive(text: str) -> bool:
    """Detect only high-confidence secrets/PII that cannot be identity-redacted."""

    if any(
        pattern.search(text)
        for pattern in (
            _PRIVATE_KEY_BLOCK,
            _BEARER_TOKEN,
            _KNOWN_API_KEY,
            _EMAIL,
            _PHONE,
            _PRC_ID,
        )
    ):
        return True
    if any("=" in match.group("label") for match in _CREDENTIAL_ASSIGNMENT.finditer(text)):
        return True
    for match in _CARD_CANDIDATE.finditer(text):
        candidate = match.group(0)
        digits = "".join(character for character in candidate if character.isdigit())
        if (
            13 <= len(digits) <= 19
            and not _has_valid_compact_timestamp_suffix(candidate)
            and _luhn_valid(digits)
        ):
            return True
    return False


def _sub_counted(
    pattern: re.Pattern[str],
    text: str,
    category: str,
    counts: Counter[str],
    replacement: Any,
) -> str:
    def replace(match: re.Match[str]) -> str:
        counts[category] += 1
        return str(replacement(match))

    return pattern.sub(replace, text)


def _redact_card(match: re.Match[str], counts: Counter[str]) -> str:
    candidate = match.group(0)
    digits = "".join(character for character in candidate if character.isdigit())
    if (
        len(digits) < 13
        or len(digits) > 19
        or _has_valid_compact_timestamp_suffix(candidate)
        or not _luhn_valid(digits)
    ):
        return candidate
    counts["bank_card"] += 1
    return "[REDACTED:BANK_CARD]"


def _has_valid_compact_timestamp_suffix(candidate: str) -> bool:
    """Distinguish generated ``YYYYMMDD[-]HHMMSS`` IDs from payment cards."""

    match = _COMPACT_TIMESTAMP_SUFFIX.search(candidate)
    if match is None:
        return False
    try:
        datetime.strptime(
            f"{match.group('date')}{match.group('time')}",
            "%Y%m%d%H%M%S",
        )
    except ValueError:
        return False
    return True


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0
