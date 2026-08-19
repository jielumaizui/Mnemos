"""Canonical in-memory representations for provider-input accounting.

The model-call ledger stores only a digest of ``input_text``.  Provider
boundaries nevertheless need that text to represent the complete billable
input, rather than just a user prompt, so the ledger can reserve a conservative
UTF-8 upper bound before dispatch.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Mapping, Sequence


_SAFE_PROVIDER_ERROR_CATEGORIES = frozenset(
    {
        "provider_authentication_failed",
        "provider_error",
        "provider_network_error",
        "provider_rate_limited",
        "provider_request_rejected",
        "provider_response_invalid",
        "provider_server_error",
        "provider_timeout",
    }
)


class ProviderRequestError(RuntimeError):
    """A provider-boundary failure whose public message is safe to persist."""

    def __init__(self, category: str) -> None:
        safe_category = (
            category if category in _SAFE_PROVIDER_ERROR_CATEGORIES else "provider_error"
        )
        self.category = safe_category
        super().__init__(safe_category)


def _provider_status_code(error: object) -> int | None:
    """Read an HTTP status defensively without rendering provider data."""
    try:
        status = getattr(error, "status_code", None)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        status = None
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    try:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def safe_provider_error_category(error: object) -> str:
    """Return a reviewed provider-error category without exposing exception text.

    Provider exceptions routinely embed request bodies, response payloads, URLs,
    or credentials in ``str(error)`` and traceback rendering.  Call boundaries
    must persist/log one of these stable labels instead.  Attribute and class
    inspection is used only to choose the label; no provider-controlled value is
    included in the returned string.
    """
    if isinstance(error, ProviderRequestError):
        return error.category

    status = _provider_status_code(error)
    if status == 429:
        return "provider_rate_limited"
    if status in (401, 403):
        return "provider_authentication_failed"
    if status in (408, 504):
        return "provider_timeout"
    if isinstance(status, int) and 400 <= status < 500:
        return "provider_request_rejected"
    if isinstance(status, int) and status >= 500:
        return "provider_server_error"

    if isinstance(error, TimeoutError):
        return "provider_timeout"
    if isinstance(error, ConnectionError):
        return "provider_network_error"
    if isinstance(error, (ValueError, TypeError, KeyError, IndexError)):
        return "provider_response_invalid"

    # Avoid returning the class name: exception subclasses are not a trusted
    # output channel.  It is inspected only for well-known SDK naming patterns.
    try:
        class_name = type(error).__name__.casefold()
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return "provider_error"
    if "timeout" in class_name:
        return "provider_timeout"
    if "rate" in class_name or "limit" in class_name:
        return "provider_rate_limited"
    if any(token in class_name for token in ("auth", "permission", "forbidden")):
        return "provider_authentication_failed"
    if any(token in class_name for token in ("connect", "network", "transport")):
        return "provider_network_error"
    if any(token in class_name for token in ("json", "decode", "parse", "validation", "schema")):
        return "provider_response_invalid"
    return "provider_error"


def canonical_provider_input(payload: Mapping[str, Any]) -> str:
    """Return a stable, complete representation of a provider-visible input.

    This value is ephemeral at the boundary: :class:`ModelCallLedger` hashes it
    and never persists the visible content.  Sorting keys makes the digest and
    byte upper bound reproducible for equivalent request dictionaries.
    """
    try:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("provider input must be JSON-serializable") from exc


def canonical_chat_input(messages: Sequence[Mapping[str, Any]]) -> str:
    """Represent every chat message, including its role and visible content."""
    return canonical_provider_input({"messages": list(messages)})


def utf8_token_upper_bound(input_text: str) -> int:
    """A tokenizer cannot emit more input tokens than UTF-8 input bytes."""
    return max(1, len(input_text.encode("utf-8", "surrogatepass")))


@contextmanager
def non_redirecting_openai_client(factory: Any, /, **kwargs: Any):
    """Yield one OpenAI-compatible client that cannot turn one call into two.

    SDK retries are not enough: the default HTTP transport follows 307/308 and
    can issue a second billable POST after the ledger has made only one
    reservation.  The provider boundary owns this short-lived transport and
    closes both layers when the request finishes.
    """
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required for a non-redirecting OpenAI provider call") from exc
    if "http_client" in kwargs:
        raise ValueError("provider boundary owns the OpenAI HTTP client")
    supplied_retries = kwargs.pop("max_retries", 0)
    if supplied_retries not in (0, None):
        raise ValueError("provider boundary requires max_retries=0")
    transport = httpx.Client(follow_redirects=False)
    client = None
    try:
        client = factory(max_retries=0, http_client=transport, **kwargs)
        yield client
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
        transport.close()


def new_non_redirecting_openai_client(factory: Any, /, **kwargs: Any) -> tuple[Any, Any]:
    """Construct a long-lived SDK client plus its owned no-redirect transport."""
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required for a non-redirecting OpenAI provider call") from exc
    if "http_client" in kwargs:
        raise ValueError("provider boundary owns the OpenAI HTTP client")
    supplied_retries = kwargs.pop("max_retries", 0)
    if supplied_retries not in (0, None):
        raise ValueError("provider boundary requires max_retries=0")
    transport = httpx.Client(follow_redirects=False)
    try:
        return factory(max_retries=0, http_client=transport, **kwargs), transport
    except BaseException:
        transport.close()
        raise
