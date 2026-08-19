# -*- coding: utf-8 -*-
"""Lightweight in-memory key pool for LLM API failover.

This module absorbs the good ideas from the deprecated ``core.credential_pool``
(multi-key rotation, failure cooldown, exponential backoff) while fitting the
existing ``core.llm_config`` architecture.

A ``KeyPool`` is attached to an ``LLMApiConfig`` when multiple keys are
configured for the same provider/model. Callers ask the pool for the currently
active key, report success/failure after the call, and the pool updates key
state accordingly.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable

#: Base cooldown minutes per error category.
COOLDOWN_MINUTES = {
    "rate_limit": 1,  # 429
    "server_error": 5,  # 5xx
    "auth_error": 60,  # 401/403
    "timeout": 2,
    "unknown": 5,
}

#: Mark a key expired after this many consecutive failures.
MAX_CONSECUTIVE_FAILURES = 5


@runtime_checkable
class KeyLike(Protocol):
    """Minimal read-only interface for a key entry stored in a pool."""

    @property
    def api_key(self) -> str: ...

    @property
    def provider(self) -> str: ...

    @property
    def base_url(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def source(self) -> str: ...


@dataclass
class _KeyState:
    """Internal mutable state for a single key."""

    status: str = "active"  # active | cooling | expired
    total_calls: int = 0
    success_calls: int = 0
    consecutive_failures: int = 0
    cooldown_until: Optional[datetime] = None
    cooldown_reason: str = ""


class KeyPool:
    """In-memory pool of interchangeable API keys for one provider/model.

    The pool selects an active key according to the configured strategy and
    tracks failures using short-term cooldowns. State is kept in memory, so
    it is reset when the process restarts; this is intentional to keep the
    implementation simple and avoid a separate SQLite dependency.
    """

    def __init__(self, keys: Sequence[KeyLike], strategy: str = "weighted"):
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        self._keys = list(keys)
        self._strategy = strategy
        self._state: Dict[str, _KeyState] = {
            self._key_id(k): _KeyState() for k in keys
        }
        self._rr_index = 0

    @staticmethod
    def _key_id(key: KeyLike) -> str:
        """Return a stable identifier for a key entry.

        ``source`` is usually unique (e.g. ``env:SILICONFLOW_API_KEY_1``).
        When it is not, append a short key suffix for disambiguation.
        """
        key_id = key.source or "unknown"
        if key.api_key and len(key.api_key) >= 8:
            key_id = f"{key_id}:{key.api_key[-8:]}"
        return key_id

    @staticmethod
    def _classify_error(error: str) -> str:
        """Classify an error string into a cooldown category."""
        error_l = str(error).lower()
        if re.search(r"\b429\b|rate.?limit|too.?many.?request", error_l):
            return "rate_limit"
        if re.search(r"\b401\b|\b403\b|unauthorized|auth", error_l):
            return "auth_error"
        if re.search(r"\b5\d{2}\b|server.?error|internal.?error", error_l):
            return "server_error"
        if re.search(r"timeout|timed.?out", error_l):
            return "timeout"
        return "unknown"

    def _is_available(self, state: _KeyState, now: datetime) -> bool:
        if state.status == "expired":
            return False
        if state.status == "cooling" and state.cooldown_until:
            if now >= state.cooldown_until:
                state.status = "active"
                state.cooldown_until = None
                state.cooldown_reason = ""
            else:
                return False
        return state.status == "active"

    def _available_keys(self) -> List[KeyLike]:
        now = datetime.now(timezone.utc)
        return [
            k for k in self._keys
            if self._is_available(self._state[self._key_id(k)], now)
        ]

    def pick(self) -> Optional[KeyLike]:
        """Return the currently active key according to the strategy."""
        candidates = self._available_keys()
        if not candidates:
            return None

        if self._strategy == "round_robin":
            idx = self._rr_index % len(candidates)
            self._rr_index = (self._rr_index + 1) % len(candidates)
            return candidates[idx]

        if self._strategy == "random":
            return random.choice(candidates)

        # weighted: prefer higher success rate, then fewer total calls
        def score(key: KeyLike) -> float:
            state = self._state[self._key_id(key)]
            if state.total_calls == 0:
                return 1.0
            success_rate = state.success_calls / state.total_calls
            return success_rate * 1000 - state.total_calls

        return max(candidates, key=score)

    def report_success(self, key: KeyLike) -> None:
        """Mark a key as having succeeded."""
        state = self._state[self._key_id(key)]
        state.total_calls += 1
        state.success_calls += 1
        state.consecutive_failures = 0
        if state.status != "expired":
            state.status = "active"
            state.cooldown_until = None
            state.cooldown_reason = ""

    def report_failure(self, key: KeyLike, error: str = "unknown") -> None:
        """Mark a key as having failed and apply cooldown."""
        state = self._state[self._key_id(key)]
        state.total_calls += 1
        state.consecutive_failures += 1

        error_type = self._classify_error(error)
        base_minutes = COOLDOWN_MINUTES.get(error_type, COOLDOWN_MINUTES["unknown"])
        actual_minutes = base_minutes * (2 ** max(0, state.consecutive_failures - 1))
        cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=actual_minutes)

        if state.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            state.status = "expired"
            state.cooldown_until = None
            state.cooldown_reason = error_type
        else:
            state.status = "cooling"
            state.cooldown_until = cooldown_until
            state.cooldown_reason = error_type

    def health(self, details: bool = False) -> Dict[str, Any]:
        """Return counts by status, optionally with per-key diagnostics."""
        result: Dict[str, int] = {"active": 0, "cooling": 0, "expired": 0, "total": 0}
        for state in self._state.values():
            result["total"] += 1
            result[state.status] = result.get(state.status, 0) + 1
        if not details:
            return result

        key_details = []
        for key in self._keys:
            state = self._state[self._key_id(key)]
            key_details.append(
                {
                    "id": self._key_id(key),
                    "source": key.source,
                    "provider": key.provider,
                    "model": key.model,
                    "status": state.status,
                    "total_calls": state.total_calls,
                    "success_calls": state.success_calls,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_until": (
                        state.cooldown_until.isoformat() if state.cooldown_until else None
                    ),
                    "cooldown_reason": state.cooldown_reason,
                }
            )
        return {**result, "keys": key_details}

    def reset(self, key: KeyLike) -> bool:
        """Manually reset a key to active."""
        state = self._state.get(self._key_id(key))
        if state is None:
            return False
        state.status = "active"
        state.consecutive_failures = 0
        state.cooldown_until = None
        state.cooldown_reason = ""
        return True
