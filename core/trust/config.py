"""Configuration helpers for the trusted push decision system."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import get_config


TRUSTED_PUSH_MODES = {"off", "shadow", "enforce"}


@dataclass(frozen=True)
class TrustedPushConfig:
    """Runtime configuration for trusted push."""

    mode: str
    db_path: Path
    evidence_ttl_days: int = 14
    rejected_evidence_ttl_hours: int = 24
    high_entropy_min_length: int = 80
    high_entropy_threshold: float = 4.2

    @property
    def enabled(self) -> bool:
        return self.mode in {"shadow", "enforce"}

    @property
    def enforce(self) -> bool:
        return self.mode == "enforce"

    @property
    def shadow(self) -> bool:
        return self.mode == "shadow"


def _coerce_mode(value: Any) -> str:
    mode = str(value or "off").strip().lower()
    return mode if mode in TRUSTED_PUSH_MODES else "off"


def load_trusted_push_config(config: Any | None = None, wiki_base: Path | None = None) -> TrustedPushConfig:
    """Load trusted push configuration from the shared Mnemos config."""

    cfg = config or get_config()
    mode = _coerce_mode(cfg.get("trusted_push.mode", "off"))
    db_value = cfg.get("trusted_push.db_path", None)
    if db_value:
        db_path = Path(str(db_value)).expanduser()
    else:
        database_dir = getattr(cfg, "database_dir", None)
        if database_dir is None and wiki_base is not None:
            database_dir = Path(wiki_base) / ".mnemos"
        if database_dir is None:
            database_dir = Path.home() / ".mnemos"
        db_path = Path(database_dir) / "trusted_push.db"
    return TrustedPushConfig(
        mode=mode,
        db_path=db_path,
        evidence_ttl_days=int(cfg.get("trusted_push.evidence_ttl_days", 14) or 14),
        rejected_evidence_ttl_hours=int(
            cfg.get("trusted_push.rejected_evidence_ttl_hours", 24) or 24
        ),
        high_entropy_min_length=int(
            cfg.get("trusted_push.high_entropy_min_length", 80) or 80
        ),
        high_entropy_threshold=float(
            cfg.get("trusted_push.high_entropy_threshold", 4.2) or 4.2
        ),
    )
