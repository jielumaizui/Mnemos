"""Pure value normalization and trusted provider-meter receipts for model calls."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

from .contracts import (
    _LEDGER_CACHE_STATUSES,
    _LEDGER_OPERATIONS,
    _LEGACY_PRICE_VERSIONS,
    _MODEL_LABEL_RE,
    _OPAQUE_METADATA_REFERENCE_KINDS,
    _OPAQUE_METADATA_REFERENCE_VERSION,
    _PROVIDER_LABEL_RE,
    _RUNTIME_LEDGER_CACHE_STATUSES,
    _RUNTIME_LEDGER_OPERATIONS,
    _SAFE_ERROR_CODES,
    ModelCallLedgerInvariantError,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_day() -> str:
    return datetime.now(timezone.utc).date().isoformat()


_EPOCH_TIMESTAMP = "1970-01-01T00:00:00+00:00"
_EPOCH_SPEND_DAY = "1970-01-01"


def _canonical_timestamp(value: object, *, fallback: str = _EPOCH_TIMESTAMP) -> str:
    """Render an external historical timestamp as one safe UTC ISO value.

    A timestamp is metadata, not a free-form notes field.  Retired-source rows can
    contain malformed caller text, so importing it verbatim would reintroduce
    a durable raw-data channel.  Unparseable values deliberately collapse to a
    fixed epoch rather than an error string or the original text.
    """
    raw = str(value or "").strip()
    if not raw:
        return fallback
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def _is_canonical_timestamp(value: object) -> bool:
    raw = str(value or "")
    return bool(raw) and raw == _canonical_timestamp(raw, fallback="")


def _canonical_spend_day(value: object, *, fallback: str = _EPOCH_SPEND_DAY) -> str:
    """Return a strict YYYY-MM-DD tombstone partition without raw text."""
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return fallback


def _is_canonical_spend_day(value: object) -> bool:
    raw = str(value or "")
    return bool(raw) and raw == _canonical_spend_day(raw, fallback="")


def _spend_day_from_timestamp(value: object) -> str:
    normalized = _canonical_timestamp(value, fallback="")
    if not normalized:
        raise ModelCallLedgerInvariantError("persisted entry timestamp is invalid")
    return normalized[:10]


@contextmanager
def _readonly_sqlite_connection(uri: str, *, timeout: float = 2) -> Iterator[sqlite3.Connection]:
    """Open a diagnostic SQLite connection without retaining its read lock."""
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    try:
        yield conn
    finally:
        conn.close()


def _hash_text(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _is_opaque_metadata_reference(value: object, kind: str) -> bool:
    """Return whether ``value`` is the ledger's one-way reference for ``kind``."""
    if kind not in _OPAQUE_METADATA_REFERENCE_KINDS:
        return False
    normalized = str(value or "")
    prefix = f"{kind}:{_OPAQUE_METADATA_REFERENCE_VERSION}:sha256:"
    return normalized.startswith(prefix) and _is_sha256_digest(normalized.removeprefix(prefix))


def _opaque_metadata_reference(
    kind: str,
    value: object,
    *,
    preserve_canonical: bool = False,
) -> str:
    """Replace external metadata with a versioned domain-separated digest.

    Runtime callers never get to choose an already-opaque-looking value: a
    preformatted prefix is itself input and is rehashed.  Reconciliation may
    preserve only this exact current on-disk format so it remains idempotent
    while migrating all earlier formats into the historical-data contract.
    """
    if kind not in _OPAQUE_METADATA_REFERENCE_KINDS:
        raise ModelCallLedgerInvariantError("unsupported model-call metadata reference kind")
    raw = str(value or "").strip()
    if not raw:
        return ""
    if preserve_canonical and _is_opaque_metadata_reference(raw, kind):
        return raw
    return (
        f"{kind}:{_OPAQUE_METADATA_REFERENCE_VERSION}:sha256:"
        f"{_hash_text(f'model-call-{kind}:{_OPAQUE_METADATA_REFERENCE_VERSION}:{raw}')}"
    )


def _is_safe_operation(value: object) -> bool:
    return str(value or "") in _LEDGER_OPERATIONS


def _normalize_operation(value: object, *, historical: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if not historical and normalized in _RUNTIME_LEDGER_OPERATIONS:
        return normalized
    if historical:
        return "legacy"
    raise ModelCallLedgerInvariantError("model-call operation is not an audited identifier")


def _is_safe_provider_label(value: object) -> bool:
    return _is_opaque_metadata_reference(value, "provider_label")


def _normalize_provider_label(value: object, *, historical: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if not historical and _PROVIDER_LABEL_RE.fullmatch(normalized):
        return normalized
    if historical:
        return _opaque_metadata_reference("provider_label", normalized, preserve_canonical=True)
    raise ModelCallLedgerInvariantError("model-call provider is not a safe identifier")


def _is_safe_model_label(value: object) -> bool:
    return _is_opaque_metadata_reference(value, "model_label")


def _normalize_model_label(value: object, *, historical: bool = False) -> str:
    normalized = str(value or "").strip()
    if not historical and _MODEL_LABEL_RE.fullmatch(normalized):
        return normalized
    if historical:
        return _opaque_metadata_reference("model_label", normalized, preserve_canonical=True)
    raise ModelCallLedgerInvariantError("model-call model is not a safe identifier")


def _is_safe_cache_status(value: object) -> bool:
    return str(value or "") in _LEDGER_CACHE_STATUSES


def _normalize_cache_status(value: object, *, historical: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if not historical and normalized in _RUNTIME_LEDGER_CACHE_STATUSES:
        return normalized
    if historical:
        return "legacy"
    raise ModelCallLedgerInvariantError("model-call cache status is not an audited identifier")


def _is_safe_metered_usage_receipt(value: object) -> bool:
    normalized = str(value or "")
    return not normalized or _is_opaque_metadata_reference(normalized, "metered_receipt")


def _normalize_metered_usage_receipt(
    value: object,
    *,
    preserve_canonical: bool = False,
) -> str:
    raw = str(value or "").strip()
    if not raw:
        return raw
    if preserve_canonical and _is_opaque_metadata_reference(raw, "metered_receipt"):
        return raw
    return _opaque_metadata_reference("metered_receipt", raw)


def _is_safe_price_version(value: object) -> bool:
    normalized = str(value or "")
    return _is_digest_reference(normalized) or normalized in _LEGACY_PRICE_VERSIONS


def _normalize_price_version(value: object, *, historical: bool = False) -> str:
    normalized = str(value or "").strip()
    if _is_safe_price_version(normalized):
        return normalized
    if historical:
        return "legacy-observation-unbillable-v1"
    raise ModelCallLedgerInvariantError("model-call price version is not a safe identifier")


def _is_sha256_digest(value: object) -> bool:
    normalized = str(value or "")
    return len(normalized) == 64 and all(character in "0123456789abcdef" for character in normalized)


def _is_digest_reference(value: object) -> bool:
    """Accept a current SHA reference or a retained prior-format SHA reference."""
    normalized = str(value or "")
    return _is_sha256_digest(normalized) or (
        normalized.startswith("sha256:") and _is_sha256_digest(normalized.removeprefix("sha256:"))
    )


# v2 denotes the current SHA-only identifier contract.  v1 remains a
# migration/tombstone lookup shape.
_CANONICAL_RUN_ID_PREFIX = "mclrun:v2:"
_LEGACY_CANONICAL_RUN_ID_PREFIX = "mclrun:"
# v2 is intentionally explicit.  The pre-v2 ``mclentry:<digest>`` spelling
# was not provenance-bound, so reconciliation must treat even a
# canonical-looking prior-version value as untrusted caller material and rekey it.
_CANONICAL_ENTRY_ID_PREFIX = "mclentry:v2:"


def _is_canonical_run_id(value: object) -> bool:
    normalized = str(value or "")
    return normalized.startswith(_CANONICAL_RUN_ID_PREFIX) and _is_sha256_digest(
        normalized.removeprefix(_CANONICAL_RUN_ID_PREFIX)
    )


def _is_prior_canonical_run_id(value: object) -> bool:
    normalized = str(value or "")
    return (
        normalized.startswith(_LEGACY_CANONICAL_RUN_ID_PREFIX)
        and not normalized.startswith(_CANONICAL_RUN_ID_PREFIX)
        and _is_sha256_digest(normalized.removeprefix(_LEGACY_CANONICAL_RUN_ID_PREFIX))
    )


def _canonical_run_id(value: object) -> str:
    """Map caller labels to a stable opaque identifier before durable storage."""
    raw = str(value or "").strip()
    if not raw:
        raw = uuid.uuid4().hex
    # A canonical-looking prefix is still public input until it is resolved
    # against an already-persisted run by ``start_run``.  Otherwise callers
    # could choose arbitrary 64-hex payloads and make them durable verbatim.
    # The durable canonical form is deliberately the same digest previously
    # used for raw-id tombstones.  That lets reconciliation rekey active rows
    # without making historic deletion-budget facts unreachable.
    return _CANONICAL_RUN_ID_PREFIX + _hash_text(f"model-call-run-id:{raw}")


def _is_canonical_entry_id(value: object) -> bool:
    normalized = str(value or "")
    digest = normalized.removeprefix(_CANONICAL_ENTRY_ID_PREFIX)
    return normalized.startswith(_CANONICAL_ENTRY_ID_PREFIX) and _is_sha256_digest(digest)


def _canonical_entry_id(value: object) -> str:
    """Map an old entry key to a stable non-reversible canonical identifier."""
    raw = str(value or "").strip()
    if _is_canonical_entry_id(raw):
        return raw
    return _CANONICAL_ENTRY_ID_PREFIX + _hash_text(f"model-call-entry-id:{raw}")


def _new_canonical_entry_id() -> str:
    return _CANONICAL_ENTRY_ID_PREFIX + _hash_text(uuid.uuid4().hex)


def _nonnegative_finite_float(value: object, *, label: str) -> float:
    """Parse a monetary/configuration value without creating a negative credit."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, bytes, bytearray)):
        raise ModelCallLedgerInvariantError(f"{label} must be a finite non-negative number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelCallLedgerInvariantError(f"{label} must be a finite non-negative number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ModelCallLedgerInvariantError(f"{label} must be a finite non-negative number")
    return parsed


def _money_equal(left: object, right: object) -> bool:
    """Compare persisted costs within a bounded number of IEEE-754 ULPs.

    A relative tolerance lets a large stored cost drift by real currency while
    still looking equal.  SQLite round-trips and the same arithmetic sequence
    can differ by a few representable units, so permit eight ULPs and nothing
    scale-relative.  Near zero the tolerance remains subnormal rather than
    becoming a fixed free-spend floor.
    """
    normalized_left = _nonnegative_finite_float(left, label="monetary value")
    normalized_right = _nonnegative_finite_float(right, label="monetary value")
    tolerance = max(math.ulp(normalized_left), math.ulp(normalized_right)) * 8
    return abs(normalized_left - normalized_right) <= tolerance


def _money_exceeds(left: object, right: object) -> bool:
    """Return true for a real positive overage at every representable scale."""
    normalized_left = _nonnegative_finite_float(left, label="monetary value")
    normalized_right = _nonnegative_finite_float(right, label="monetary value")
    # Invariants may use a relative float comparison to recognize the same
    # arithmetic after SQLite round-trips.  A budget gate may not: even a
    # sub-display-precision positive excess is still spend beyond the caller's
    # configured ceiling.
    return normalized_left > normalized_right


def _nonnegative_int(value: object, *, label: str) -> int:
    """Reject malformed token counts instead of silently turning them into credit."""
    if isinstance(value, bool):
        raise ModelCallLedgerInvariantError(f"{label} must be a non-negative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ModelCallLedgerInvariantError(f"{label} must be a non-negative integer")
        parsed = int(value)
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized.isascii() or not normalized.isdigit():
            raise ModelCallLedgerInvariantError(f"{label} must be a non-negative integer")
        parsed = int(normalized)
    else:
        raise ModelCallLedgerInvariantError(f"{label} must be a non-negative integer")
    if parsed < 0:
        raise ModelCallLedgerInvariantError(f"{label} must be a non-negative integer")
    return parsed


def _utf8_input_token_upper_bound(value: object) -> int:
    """Return a conservative upper bound for provider-visible input tokens.

    Every token consumes at least one UTF-8 byte, so a tokenizer cannot emit
    more input tokens than the complete payload's UTF-8 byte length.
    Requiring the reservation to cover that upper bound prevents a caller
    from declaring one token for a multi-megabyte payload.  Provider
    boundaries pass their canonical complete request envelope (system prompt,
    roles and user content included), not a selected prompt fragment.
    """
    if not isinstance(value, str):
        raise ModelCallLedgerInvariantError("reservation input_text must be canonical UTF-8 text")
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeError as exc:
        raise ModelCallLedgerInvariantError(
            "reservation input_text must be canonical UTF-8 text"
        ) from exc


def _safe_error_code(value: object, *, default: str) -> str:
    """Persist only an audited failure category, never caller/exception text."""
    candidate = str(value or default).strip()
    return candidate if candidate in _SAFE_ERROR_CODES else "error_redacted"


def _json_hash(value: Mapping[str, Any]) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(rendered)


def _config_get(config: Any | None, key: str, default: Any) -> Any:
    if config is not None and hasattr(config, "get"):
        return config.get(key, default)
    return default


def _case_insensitive_mapping_value(mapping: Mapping[str, Any], key: str) -> Any | None:
    normalized = str(key or "").lower()
    for candidate, value in mapping.items():
        if str(candidate).lower() == normalized:
            return value
    return None


def _explicit_provider_price(
    provider: str,
    model: str,
    config: Any | None,
) -> tuple[Mapping[str, Any] | None, str]:
    """Return a named price source; unknown prices are never silently free."""
    from core.llm_config import DEFAULT_PROVIDER_PRICES

    normalized_provider = str(provider or "").lower()
    normalized_model = str(model or "").lower()
    configured_prices = _config_get(config, "llm.provider_prices", {}) or {}
    if isinstance(configured_prices, Mapping):
        configured_provider = _case_insensitive_mapping_value(
            configured_prices, normalized_provider
        )
        if isinstance(configured_provider, Mapping):
            configured_model = _case_insensitive_mapping_value(
                configured_provider, normalized_model
            )
            if isinstance(configured_model, Mapping):
                return configured_model, "configured_exact"
            configured_default = _case_insensitive_mapping_value(configured_provider, "default")
            if isinstance(configured_default, Mapping):
                return configured_default, "configured_default"

    default_provider = _case_insensitive_mapping_value(
        DEFAULT_PROVIDER_PRICES, normalized_provider
    )
    if isinstance(default_provider, Mapping):
        default_model = _case_insensitive_mapping_value(default_provider, normalized_model)
        if isinstance(default_model, Mapping):
            return default_model, "built_in_exact"
        default_price = _case_insensitive_mapping_value(default_provider, "default")
        if isinstance(default_price, Mapping):
            return default_price, "built_in_default"
    return None, "missing"


def _price_snapshot(
    provider: str,
    model: str,
    config: Any | None,
) -> tuple[float, float, str]:
    """Return a priced immutable snapshot; unknown providers fail before dispatch."""
    price, price_source = _explicit_provider_price(provider, model, config)
    if price is None:
        raise ModelCallLedgerInvariantError(
            "model-call price is missing for provider/model; configure an explicit price before dispatch"
        )
    if "input" not in price or "output" not in price:
        raise ModelCallLedgerInvariantError(
            "model-call price must explicitly provide finite input and output rates"
        )
    input_price = _nonnegative_finite_float(
        price["input"],
        label="configured input price",
    )
    output_price = _nonnegative_finite_float(
        price["output"],
        label="configured output price",
    )
    if (input_price == 0.0 or output_price == 0.0) and not (
        price_source == "configured_exact"
        and bool(_config_get(config, "model_call_ledger.allow_explicit_zero_price", False))
    ):
        raise ModelCallLedgerInvariantError(
            "any zero model-call price requires configured provider/model pricing and explicit "
            "allow_explicit_zero_price"
        )
    version_payload = {
        "provider": str(provider or "").lower(),
        "model": str(model or "").lower(),
        "input_per_1k": input_price,
        "output_per_1k": output_price,
        "price_source": price_source,
    }
    version = "sha256:" + _json_hash(version_payload)
    return input_price, output_price, version


def _cost(input_tokens: int, output_tokens: int, input_price: float, output_price: float) -> float:
    normalized_input_tokens = _nonnegative_int(input_tokens, label="input token count")
    normalized_output_tokens = _nonnegative_int(output_tokens, label="output token count")
    normalized_input_price = _nonnegative_finite_float(input_price, label="input price")
    normalized_output_price = _nonnegative_finite_float(output_price, label="output price")
    # Do not round per request.  A fixed decimal floor lets an attacker split
    # a real cost into individually-zero entries and evade a daily/run cap.
    # SQLite REAL retains values far below the former 1e-12 display precision;
    # only presentation may choose its own rounding.
    amount = (normalized_input_tokens / 1000.0) * normalized_input_price + (
        normalized_output_tokens / 1000.0
    ) * normalized_output_price
    return _nonnegative_finite_float(amount, label="model-call cost")


def has_metered_provider_usage(usage: Any) -> bool:
    """True only when a provider response includes an explicit token meter.

    A response/request identifier alone proves transport acceptance, not actual
    provider metering.  Boundaries must preserve the reservation as incurred
    unknown whenever this predicate is false.
    """
    fields = ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens")
    if isinstance(usage, Mapping):
        return any(field in usage and usage[field] is not None for field in fields)
    return any(getattr(usage, field, None) is not None for field in fields)


class MeteredProviderUsageReceipt(Protocol):
    """Static contract implemented only by factory-issued runtime receipts."""

    @property
    def is_factory_issued(self) -> bool: ...

    @property
    def input_tokens(self) -> int: ...

    @property
    def output_tokens(self) -> int: ...

    @property
    def metered_usage_receipt(self) -> str: ...

    @property
    def provider_usage_id(self) -> str: ...

    @property
    def request_id(self) -> str: ...


def _build_metered_usage_factory() -> tuple[type[object], Any, Any]:
    """Build a trusted-process guard for settleable provider meter receipts.

    A module-global token can be imported and supplied to a public dataclass
    constructor. The receipt type and issuance closure instead share lexical
    state, preventing ordinary/public API callers from constructing a valid
    receipt. This is intentionally an anti-misuse boundary inside the trusted
    Mnemos process, not a cryptographic isolation claim against hostile code
    already executing in that interpreter.
    """
    # Facts live only in this lexical registry.  Do not put a capability token
    # or mutable metering fields on the public receipt object: a caller that
    # can inspect one valid receipt could otherwise clone its capability onto
    # an ``object.__new__`` instance.
    issued_receipts: weakref.WeakKeyDictionary[object, tuple[int, int, str, str, str]] = (
        weakref.WeakKeyDictionary()
    )

    class MeteredProviderUsage:
        """Trusted-process provider-meter receipt accepted by settlement only."""

        __slots__ = ("__weakref__",)

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise ModelCallLedgerInvariantError(
                "metered usage must be issued by the provider-meter receipt factory"
            )

        @property
        def is_factory_issued(self) -> bool:
            return self in issued_receipts

        def _facts(self) -> tuple[int, int, str, str, str]:
            facts = issued_receipts.get(self)
            if facts is None:
                raise ModelCallLedgerInvariantError(
                    "metered usage must be issued by the provider-meter receipt factory"
                )
            return facts

        @property
        def input_tokens(self) -> int:
            return self._facts()[0]

        @property
        def output_tokens(self) -> int:
            return self._facts()[1]

        @property
        def metered_usage_receipt(self) -> str:
            return self._facts()[2]

        @property
        def provider_usage_id(self) -> str:
            return self._facts()[3]

        @property
        def request_id(self) -> str:
            return self._facts()[4]

    def metered_provider_usage(
        usage: Any,
        *,
        request_id: str = "",
        output_required: bool,
    ) -> MeteredProviderUsage | None:
        """Build a receipt only from explicit provider token-metering fields."""
        if not has_metered_provider_usage(usage):
            return None

        def _value(*names: str) -> int | None:
            for name in names:
                value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
                if value is not None:
                    # Provider JSON may carry integer tokens as strings or
                    # integral floats, but truncating a fractional/malformed
                    # meter downward would turn a paid call into a lower
                    # settled charge.  Treat it as unverified instead.
                    if isinstance(value, bool):
                        return None
                    if isinstance(value, int):
                        parsed = value
                    elif isinstance(value, float):
                        if not math.isfinite(value) or not value.is_integer():
                            return None
                        parsed = int(value)
                    elif isinstance(value, str):
                        normalized = value.strip()
                        if not normalized.isascii() or not normalized.isdigit():
                            return None
                        parsed = int(normalized)
                    else:
                        return None
                    if parsed < 0:
                        raise ModelCallLedgerInvariantError("provider metered usage cannot be negative")
                    return parsed
            return None

        input_tokens = _value("prompt_tokens", "input_tokens")
        output_tokens = _value("completion_tokens", "output_tokens")
        total_tokens = _value("total_tokens")
        if input_tokens is None and not output_required and total_tokens is not None:
            input_tokens = total_tokens
        if input_tokens is None:
            return None
        if output_required and output_tokens is None:
            return None
        output_tokens = 0 if output_tokens is None else output_tokens
        if total_tokens is not None and total_tokens != input_tokens + output_tokens:
            # Explicit totals are provider evidence too.  A contradiction
            # cannot be safely rounded down; the caller preserves the full
            # reservation as incurred unknown.
            return None

        def _text_value(*names: str) -> str:
            for name in names:
                value = usage.get(name) if isinstance(usage, Mapping) else getattr(usage, name, None)
                if value is not None and str(value).strip():
                    return str(value).strip()
            return ""

        provider_usage_id = _text_value("usage_id", "usageId")
        provider_request_id = str(request_id or "").strip() or _text_value(
            "request_id", "requestId", "id"
        )
        if not provider_usage_id and not provider_request_id:
            # The locally generated meter digest proves only that this process
            # observed numbers; it cannot establish a 1:1 provider request.
            return None
        meter_receipt = _opaque_metadata_reference(
            "metered_receipt",
            json.dumps(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                    "provider_usage_id": provider_usage_id,
                    "request_id": provider_request_id,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        receipt = object.__new__(MeteredProviderUsage)
        issued_receipts[receipt] = (
            input_tokens,
            output_tokens,
            meter_receipt,
            provider_usage_id,
            provider_request_id,
        )
        return receipt

    def issued_metered_usage_facts(
        receipt: MeteredProviderUsage,
    ) -> tuple[int, int, str, str, str]:
        if not isinstance(receipt, MeteredProviderUsage):
            raise ModelCallLedgerInvariantError(
                "settlement requires a factory-issued metered receipt"
            )
        return receipt._facts()

    return MeteredProviderUsage, metered_provider_usage, issued_metered_usage_facts


MeteredProviderUsage, metered_provider_usage, _issued_metered_usage_facts = _build_metered_usage_factory()
