"""Sanitize content before it can be sent to local AgentBackend processes."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, List, Sequence

from core.config import get_config
from core.trust.models import new_id, sha256_json, sha256_text, utc_now_iso


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*([^\s,'\"]{8,})"),
    re.compile(r"(?i)bearer\s+([A-Za-z0-9._~+/=-]{16,})"),
)
_PATH_PATTERN = re.compile(r"(?P<path>(?:~|/)[A-Za-z0-9._~+/=@:%-][^\s'\"<>)]*)")


@dataclass(frozen=True)
class PromptSanitizerFinding:
    kind: str
    source_label: str
    value_hash: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PromptSanitizerResult:
    allowed: bool
    redacted_text: str
    redacted_args: List[str]
    findings: List[PromptSanitizerFinding]
    audit_event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "redacted_text": self.redacted_text,
            "redacted_args": self.redacted_args,
            "findings": [finding.to_dict() for finding in self.findings],
            "audit_event_id": self.audit_event_id,
        }


class PromptSanitizerAuditStore:
    """Audit sanitizer decisions without storing prompt text or raw paths."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or get_config().database_dir / "agent_authorization.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_sanitizer_events (
                    event_id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    source_label TEXT NOT NULL,
                    allowed INTEGER NOT NULL,
                    findings_json TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def record(
        self,
        *,
        agent: str,
        source_label: str,
        allowed: bool,
        findings: Sequence[PromptSanitizerFinding],
        text: str,
        args: Sequence[str],
    ) -> str:
        event_id = new_id("sanitize")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO prompt_sanitizer_events (
                    event_id, agent, source_label, allowed, findings_json,
                    text_hash, args_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    agent,
                    source_label,
                    1 if allowed else 0,
                    json.dumps([finding.to_dict() for finding in findings], ensure_ascii=False),
                    sha256_text(text),
                    sha256_json(list(args)),
                    utc_now_iso(),
                ),
            )
        return event_id

    def list_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM prompt_sanitizer_events
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["findings"] = json.loads(item.pop("findings_json"))
            events.append(item)
        return events


class PromptSanitizer:
    """Block raw sensitive prompts before subprocess AgentBackend calls."""

    def __init__(
        self,
        *,
        wiki_base: Path | None = None,
        database_dir: Path | None = None,
        allowed_dirs: Iterable[Path] | None = None,
        audit_store: PromptSanitizerAuditStore | None = None,
    ):
        cfg = get_config()
        self._wiki_base = Path(wiki_base or cfg.wiki_dir).expanduser().resolve()
        self._database_dir = Path(database_dir or cfg.database_dir).expanduser().resolve()
        self._allowed_dirs = [
            Path(path).expanduser().resolve() for path in list(allowed_dirs or [])
        ]
        self._audit = audit_store

    def sanitize(
        self,
        *,
        agent: str,
        text: str,
        args: Sequence[str] = (),
        source_label: str = "agent_backend",
        allowed_dirs: Iterable[Path] | None = None,
    ) -> PromptSanitizerResult:
        effective_allowed = self._allowed_dirs + [
            Path(path).expanduser().resolve() for path in list(allowed_dirs or [])
        ]
        findings: list[PromptSanitizerFinding] = []
        redacted_text = text
        redacted_args = list(args)
        for value in _secret_values(text):
            findings.append(_finding("secret", source_label, value, "secret-like token"))
            redacted_text = _redact(redacted_text, value)
        for idx, arg in enumerate(redacted_args):
            for value in _secret_values(arg):
                findings.append(_finding("secret", source_label, value, "secret-like token"))
                redacted_args[idx] = _redact(redacted_args[idx], value)

        for value in _path_values(text):
            path_finding = self._classify_path(value, source_label, effective_allowed)
            if path_finding is not None:
                findings.append(path_finding)
                redacted_text = _redact(redacted_text, value)
        for idx, arg in enumerate(redacted_args):
            for value in _path_values(arg):
                path_finding = self._classify_path(value, source_label, effective_allowed)
                if path_finding is not None:
                    findings.append(path_finding)
                    redacted_args[idx] = _redact(redacted_args[idx], value)

        allowed = not findings
        audit_event_id = ""
        if self._audit is not None:
            audit_event_id = self._audit.record(
                agent=agent,
                source_label=source_label,
                allowed=allowed,
                findings=findings,
                text=text,
                args=list(args),
            )
        return PromptSanitizerResult(
            allowed=allowed,
            redacted_text=redacted_text,
            redacted_args=redacted_args,
            findings=findings,
            audit_event_id=audit_event_id,
        )

    def _classify_path(
        self,
        value: str,
        source_label: str,
        allowed_dirs: Sequence[Path],
    ) -> PromptSanitizerFinding | None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            return None
        resolved = path.resolve(strict=False)
        if _is_under_any(resolved, allowed_dirs):
            return None
        if _is_under(resolved, self._wiki_base):
            return _finding("wiki_path", source_label, value, "wiki path requires mirror")
        if _is_under(resolved, self._database_dir):
            return _finding("internal_path", source_label, value, "database dir path")
        if resolved.suffix in {".db", ".sqlite", ".sqlite3"}:
            return _finding("sqlite_path", source_label, value, "sqlite path")
        if _looks_like_config_path(resolved):
            return _finding("config_path", source_label, value, "config path")
        return _finding("unauthorized_path", source_label, value, "path outside allowed dirs")


def _secret_values(text: str) -> list[str]:
    values: list[str] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text):
            values.append(match.group(match.lastindex or 0))
    return values


def _path_values(text: str) -> list[str]:
    return [match.group("path").rstrip(".,;:") for match in _PATH_PATTERN.finditer(text)]


def _finding(kind: str, source_label: str, value: str, reason: str) -> PromptSanitizerFinding:
    return PromptSanitizerFinding(
        kind=kind,
        source_label=source_label,
        value_hash=sha256_text(value),
        reason=reason,
    )


def _redact(text: str, value: str) -> str:
    return text.replace(value, f"<redacted:{sha256_text(value)[:12]}>")


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def _is_under_any(path: Path, bases: Sequence[Path]) -> bool:
    return any(_is_under(path, base) for base in bases)


def _looks_like_config_path(path: Path) -> bool:
    lowered = str(path).lower()
    return "/config" in lowered or path.name in {".env", "main.json", "config.json"}
