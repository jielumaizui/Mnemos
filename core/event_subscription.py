"""Stable EventBus subscription identities and restart checkpoints."""

from __future__ import annotations

from typing import Any, Callable


def handler_qualname(handler: Callable[..., Any]) -> str:
    """Return the code identity used when no explicit consumer id is supplied."""

    module = str(getattr(handler, "__module__", "") or "")
    qualname = str(
        getattr(handler, "__qualname__", "")
        or getattr(handler, "__name__", "")
        or repr(handler)
    )
    display_name = str(getattr(handler, "__name__", "") or "")
    if display_name and display_name != qualname.rsplit(".", 1)[-1]:
        qualname = f"{qualname}[{display_name}]"
    return f"{module}.{qualname}"


def subscribe_handler(
    bus: Any,
    event_type: str,
    handler: Callable[..., Any],
    consumer_id: str | None,
) -> None:
    """Register one handler under a stable, unique per-event consumer id."""

    stable_id = str(consumer_id or handler_qualname(handler)).strip()
    if not stable_id:
        raise ValueError("EventBus consumer_id cannot be empty")
    with bus._handlers_lock:
        duplicate = any(
            kind == event_type and existing == stable_id
            for (kind, _handler_key), existing in bus._handler_consumer_ids.items()
        )
        if duplicate and consumer_id is not None:
            raise ValueError(
                f"duplicate EventBus consumer_id for {event_type}: {stable_id}"
            )
        if duplicate:
            suffix = 2
            candidate = f"{stable_id}#{suffix}"
            existing_ids = set(bus._handler_consumer_ids.values())
            while candidate in existing_ids:
                suffix += 1
                candidate = f"{stable_id}#{suffix}"
            stable_id = candidate
        bus._handlers.setdefault(event_type, []).append(handler)
        bus._handler_consumer_ids[(event_type, id(handler))] = stable_id


def subscription_identity(bus: Any, event_type: str, handler: Callable[..., Any]) -> str:
    """Return a checkpoint identity unaffected by subscriber ordering."""

    consumer_id = subscription_consumer_id(bus, event_type, handler)
    return f"{event_type}:{consumer_id}"


def subscription_consumer_id(
    bus: Any, event_type: str, handler: Callable[..., Any]
) -> str:
    """Return the projection consumer id independently of its event route."""

    return str(
        bus._handler_consumer_ids.get(
            (event_type, id(handler)), handler_qualname(handler)
        )
    )


def was_handler_processed(
    bus: Any,
    processed: set[str],
    event_type: str,
    handler: Callable[..., Any],
    display_name: str,
) -> bool:
    """Recognize current stable ids and pre-migration index-based checkpoints."""

    current = subscription_identity(bus, event_type, handler)
    if current in processed or display_name in processed:
        return True
    legacy_suffix = f":{handler_qualname(handler)}"
    legacy_prefix = f"{event_type}:"
    return any(
        value.startswith(legacy_prefix) and value.endswith(legacy_suffix)
        for value in processed
    )
