# -*- coding: utf-8 -*-
"""
AgentSource 接口契约

每个 AI Agent 实现一个子类，接入 SyncFramework。
必须实现：name, model_tag, discover_sessions, parse_turns
可选覆写：data_dir, trigger_strategy, build_extra_tags, on_session_start, on_session_end
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from typing import Any, Callable, Dict, List, Optional, cast

from core.sync_framework.native_sqlite import (
    NativeSQLiteReadError,
    connect_native_sqlite_readonly,
    native_storage_failure_evidence,
)
from core.ops.durable_io import (
    canonical_native_path,
    open_native_binary,
)

TURN_STRUCTURED_METADATA_KEYS = frozenset(
    {
        "tool_calls",
        "tool_results",
        "reasoning",
        "attachments",
        "raw_event_refs",
        "source_files",
        "completeness",
    }
)


@dataclass
class SessionInfo:
    """可同步的会话信息"""

    session_id: str
    source_path: Path
    working_dir: Optional[str] = None
    mtime: Optional[float] = None
    canonical_session_id: Optional[str] = None
    session_aliases: List[str] = field(default_factory=list)
    source_kind: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_paths: List[Path] = field(default_factory=list)


def canonicalize_session_info(session_info: SessionInfo) -> SessionInfo:
    """Return the one storage identity for aliases and split native sessions.

    The function is deliberately dependency-free so daemon, CLI, and raw-only
    maintenance paths can share exactly the same canonical identity contract.
    The original source id remains in ``session_aliases`` for provenance.
    """
    canonical_id = session_info.canonical_session_id or session_info.session_id
    if canonical_id == session_info.session_id:
        return session_info
    aliases = list(dict.fromkeys([session_info.session_id, *session_info.session_aliases]))
    return SessionInfo(
        session_id=canonical_id,
        source_path=session_info.source_path,
        working_dir=session_info.working_dir,
        mtime=session_info.mtime,
        canonical_session_id=canonical_id,
        session_aliases=aliases,
        source_kind=session_info.source_kind,
        metadata=dict(session_info.metadata or {}),
        source_paths=list(session_info.source_paths or []),
    )


@dataclass
class Turn:
    """单轮对话记录 — 扩展以支持完整对话录入契约"""

    turn_number: int
    user_content: str
    assistant_content: str
    timestamp: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    # A producer-provided message/event id.  It must never be inferred from a
    # generic ``id`` field: those often identify a session or container.
    native_event_id: str = ""
    # 完整录入契约字段（P0-0）
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    reasoning: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    raw_event_refs: List[Dict[str, Any]] = field(default_factory=list)
    source_files: List[str] = field(default_factory=list)
    completeness: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # 确保 completeness 有默认值
        if not self.completeness:
            self.completeness = {
                "visible_text": "full",
                "tool_results": "full" if self.tool_results else "unavailable",
                "reasoning": "full" if self.reasoning else "unavailable",
                "attachments": "full" if self.attachments else "unavailable",
                "truncated": False,
                "loss_reasons": [],
            }


SESSION_DISPOSITIONS = frozenset(
    {
        "parsed",
        "typed_empty",
        "evidence_excluded",
        "legacy_unverified",
    }
)


class NativeSourceContractError(RuntimeError):
    """Stable source-boundary failure that never carries native content."""

    def __init__(
        self,
        code: str,
        *,
        storage_evidence: Dict[str, Any] | None = None,
    ) -> None:
        normalized = str(code or "")
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,127}", normalized):
            normalized = "native_source_contract_failed"
        self.code = normalized
        evidence = dict(storage_evidence or {})
        allowed_keys = {
            "failure_class",
            "os_errno",
            "retryable",
            "sqlite_errorcode",
            "sqlite_errorname",
        }
        if not set(evidence).issubset(allowed_keys):
            evidence = {}
        failure_class = evidence.get("failure_class")
        sqlite_errorname = evidence.get("sqlite_errorname")
        if (
            evidence
            and (
                not isinstance(failure_class, str)
                or re.fullmatch(
                    r"(?:sqlite|os)_(?:transient|nontransient)|storage_untyped",
                    failure_class,
                )
                is None
                or not isinstance(evidence.get("retryable"), bool)
                or (
                    sqlite_errorname is not None
                    and (
                        not isinstance(sqlite_errorname, str)
                        or re.fullmatch(
                            r"SQLITE_[A-Z0-9_]{1,96}",
                            sqlite_errorname,
                        )
                        is None
                    )
                )
                or any(
                    isinstance(evidence.get(key), bool)
                    or not isinstance(evidence.get(key), int)
                    for key in ("os_errno", "sqlite_errorcode")
                    if key in evidence
                )
            )
        ):
            evidence = {}
        self.details = evidence
        self.retryable = evidence.get("retryable") is True
        super().__init__(normalized)

    @classmethod
    def from_storage_failure(
        cls,
        code: str,
        failure: BaseException,
    ) -> "NativeSourceContractError":
        return cls(
            code,
            storage_evidence=native_storage_failure_evidence(failure),
        )


def native_artifact_content_state(
    files: List[Path],
    *,
    sort_key: Optional[Callable[[Path], Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return one content-bound state for an exact ordered artifact set."""

    if not files:
        return None
    ordered = sorted(files, key=sort_key) if sort_key is not None else list(files)
    digest = hashlib.sha256()
    total_size = 0
    max_mtime = 0.0
    for index, path in enumerate(ordered):
        try:
            with open_native_binary(path) as handle:
                data = handle.read()
                metadata = os.fstat(handle.fileno())
        except OSError as exc:
            raise NativeSourceContractError.from_storage_failure(
                "native_session_state_read_failed",
                exc,
            ) from None
        label = path.name.encode("utf-8", errors="surrogateescape")
        digest.update(index.to_bytes(8, "big"))
        digest.update(len(label).to_bytes(8, "big"))
        digest.update(label)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        total_size += len(data)
        max_mtime = max(max_mtime, metadata.st_mtime)
    return {
        "mtime": max_mtime,
        "size": total_size,
        "file_count": len(ordered),
        "fingerprint": digest.hexdigest(),
        "fingerprint_contract": "ordered-artifact-name-bytes-sha256-v1",
    }


@dataclass(frozen=True)
class SessionParseResult:
    """Parsed turns plus an explicit disposition for the native session."""

    turns: tuple[Turn, ...]
    disposition: str
    reason_code: str
    artifact_evidence_hash: str = ""
    infrastructure_attempt_count: int = 1
    recovered_infrastructure_failure: Dict[str, Any] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        if self.disposition not in SESSION_DISPOSITIONS - {"legacy_unverified"}:
            raise NativeSourceContractError("native_session_disposition_invalid")
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,127}", self.reason_code):
            raise NativeSourceContractError("native_session_disposition_reason_invalid")
        if self.disposition == "parsed" and not self.turns:
            raise NativeSourceContractError("native_session_parsed_without_turns")
        if self.disposition != "parsed" and self.turns:
            raise NativeSourceContractError("native_session_nonparsed_has_turns")
        if self.artifact_evidence_hash and not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            self.artifact_evidence_hash,
        ):
            raise NativeSourceContractError("native_session_evidence_hash_invalid")
        if (
            isinstance(self.infrastructure_attempt_count, bool)
            or not isinstance(self.infrastructure_attempt_count, int)
            or self.infrastructure_attempt_count < 1
            or self.infrastructure_attempt_count > 16
        ):
            raise NativeSourceContractError(
                "native_session_infrastructure_attempt_count_invalid"
            )
        recovered = dict(self.recovered_infrastructure_failure or {})
        allowed_recovery_keys = {
            "error_code",
            "exception_type",
            "failure_class",
            "os_errno",
            "reason_code",
            "signal",
            "sqlite_errorcode",
            "sqlite_errorname",
        }
        if (
            not set(recovered).issubset(allowed_recovery_keys)
            or bool(recovered) != (self.infrastructure_attempt_count > 1)
            or any(
                not isinstance(recovered.get(key), str)
                or re.fullmatch(r"[a-z][a-z0-9_]{2,127}", recovered[key])
                is None
                for key in ("error_code", "reason_code")
                if key in recovered
            )
            or (
                "exception_type" in recovered
                and (
                    not isinstance(recovered["exception_type"], str)
                    or re.fullmatch(
                        r"[A-Za-z_][A-Za-z0-9_.]{0,127}",
                        recovered["exception_type"],
                    )
                    is None
                )
            )
            or (
                "failure_class" in recovered
                and recovered["failure_class"]
                not in {
                    "os_nontransient",
                    "os_transient",
                    "sqlite_nontransient",
                    "sqlite_transient",
                    "storage_untyped",
                }
            )
            or (
                "sqlite_errorname" in recovered
                and (
                    not isinstance(recovered["sqlite_errorname"], str)
                    or re.fullmatch(
                        r"SQLITE_[A-Z0-9_]{1,96}",
                        recovered["sqlite_errorname"],
                    )
                    is None
                )
            )
            or any(
                isinstance(recovered.get(key), bool)
                or not isinstance(recovered.get(key), int)
                for key in ("os_errno", "signal", "sqlite_errorcode")
                if key in recovered
            )
            or (
                "signal" in recovered
                and not 1 <= recovered["signal"] <= 127
            )
        ):
            raise NativeSourceContractError(
                "native_session_infrastructure_recovery_evidence_invalid"
            )


@dataclass
class SyncResult:
    """同步结果"""

    session_id: str
    turn_number: int
    action: str  # "new" | "updated" | "skipped" | "noise" | "failed"
    backend_uids: List[str] = field(default_factory=list)
    content_hash: Optional[str] = None
    error: Optional[str] = None
    # A non-empty revision id proves canonical Raw accepted this exact turn.
    # Continuous capture cursors may advance only when this receipt exists.
    raw_event_id: Optional[str] = None


@dataclass
class BatchSyncResult:
    """批量同步结果

    契约化 SyncEngine.sync_batch 的返回类型，替代裸 Dict。
    """

    agent: str
    total_sessions: int
    successful: List[Dict[str, Any]] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    turn_stats: Dict[str, int] = field(
        default_factory=lambda: {"new": 0, "updated": 0, "skipped": 0, "noise": 0, "failed": 0}
    )


class AgentSource(ABC):
    """Agent 数据源抽象，每个 AI 系统实现一个子类"""

    # ========== 必须实现 ==========

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent 标识名，如 'claude', 'kimi', 'openclaw'"""
        ...

    @property
    @abstractmethod
    def model_tag(self) -> str:
        """模型标签，如 'claude-code', 'kimi-k2.5'"""
        ...

    @abstractmethod
    def discover_sessions(self) -> List[SessionInfo]:
        """发现当前所有可同步的会话"""
        ...

    @abstractmethod
    def parse_turns(self, session_path: Path) -> List[Turn]:
        """解析会话文件，提取按轮次排列的对话记录"""
        ...

    # ========== 可选覆写 ==========

    def parse_session(self, session_info: SessionInfo) -> List[Turn]:
        """Parse one discovered session with its full identity and provenance.

        File-backed adapters keep the historical ``parse_turns(path)``
        implementation through this default. Database-backed adapters can
        override this seam when the physical source file alone cannot identify
        a native session safely.
        """
        return self.parse_turns(session_info.source_path)

    def native_artifact_paths(self, session_info: SessionInfo) -> List[Path]:
        """Return every native artifact whose bytes can affect this parse.

        Multi-file and database adapters must override this parser-owned seam.
        The returned paths are used for immutable migration inventory, not
        persisted as reader-facing evidence.
        """
        return [session_info.source_path]

    @property
    def data_dir(self) -> Optional[Path]:
        """
        Agent 数据目录。
        返回 None 时由框架通过 PathDiscover 自动探测。
        子类可覆写以提供精确路径，跳过自动发现。
        """
        return None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        """
        声明触发策略，框架据此选择 TriggerDispatcher 实现。
        不覆写则默认：WatchdogTrigger + on_modified + 5s debounce
        """
        return {
            "type": "watchdog",
            "events": ["modified"],
            "debounce": 5.0,
            "recursive": True,
        }

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Agent 自定义标签"""
        return []

    def on_session_start(self, session_id: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """KIA Hook：Session 开始时调用"""
        return {}

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:  # noqa
        """
        返回 session 的聚合状态（多文件/数据库来源必须覆写）。
        用于 L1 扫描判断 session 是否变化，避免只看单个入口文件。

        返回 dict 必须包含：
          - mtime: 所有相关文件的最大 mtime
          - size: 所有相关文件的总大小（字节）
          - file_count: 相关文件数量
          - fingerprint: 可复现的哈希字符串
        """
        return native_artifact_content_state([session_info.source_path])

    def completeness_capabilities(self) -> Dict[str, Any]:
        """
        声明该 AgentSource 理论上能采集到什么。
        用于 doctor/audit 显示来源完整性等级。
        返回值统一为 bool：True = 支持且能获取，False = 不支持或不确定。
        """
        return {
            "visible_text": True,
            "tool_calls": False,
            "tool_results": False,
            "reasoning": False,
            "attachments": False,
            "raw_files": True,
            "source_fidelity": "full",
        }

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """KIA Hook：Session 结束时调用。

        可选生命周期钩子。子类可按需覆盖以执行清理、归档或触发后续处理。
        基类默认空操作，不强制要求实现。
        """


def parse_discovered_session(source: Any, session_info: SessionInfo) -> List[Turn]:
    """Use the session-aware seam while preserving third-party source compatibility."""
    parser = getattr(source, "parse_session", None)
    if callable(parser):
        return cast(List[Turn], parser(session_info))
    return cast(List[Turn], source.parse_turns(session_info.source_path))


def _artifact_content_evidence(path: Path) -> dict[str, Any]:
    """Hash one native artifact without exposing its path or content."""

    try:
        resolved = canonical_native_path(path)
        with open_native_binary(resolved) as handle:
            header = handle.read(16)
        if header == b"SQLite format 3\x00":
            connection = connect_native_sqlite_readonly(resolved)
            try:
                connection.execute("BEGIN")
                digest = hashlib.sha256()
                size = 0
                for statement in connection.iterdump():
                    encoded = statement.encode("utf-8")
                    digest.update(encoded)
                    digest.update(b"\n")
                    size += len(encoded) + 1
            finally:
                connection.close()
            return {
                "kind": "sqlite-logical-dump-v1",
                "sha256": digest.hexdigest(),
                "size": size,
            }
        digest = hashlib.sha256()
        size = 0
        with open_native_binary(resolved) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
        return {
            "kind": "file-bytes-v1",
            "sha256": digest.hexdigest(),
            "size": size,
        }
    except (NativeSQLiteReadError, OSError, sqlite3.Error) as exc:
        raise NativeSourceContractError.from_storage_failure(
            "native_session_artifact_evidence_failed",
            exc,
        ) from None


def native_session_artifact_evidence_hash(
    source: Any,
    session_info: SessionInfo,
) -> str:
    """Bind one session to every parser-owned native artifact."""

    evidence_owner = getattr(source, "session_artifact_evidence_hash", None)
    if callable(evidence_owner):
        evidence_hash = str(evidence_owner(session_info) or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_hash):
            raise NativeSourceContractError(
                "native_session_evidence_hash_invalid"
            )
        return evidence_hash
    path_owner = getattr(source, "native_artifact_paths", None)
    declared_paths = (
        list(path_owner(session_info) or [])
        if callable(path_owner)
        else [session_info.source_path]
    )
    if not declared_paths:
        raise NativeSourceContractError("native_session_artifact_set_empty")
    try:
        paths = sorted(
            {
                canonical_native_path(Path(path))
                for path in declared_paths
            },
            key=str,
        )
    except OSError as exc:
        raise NativeSourceContractError.from_storage_failure(
            "native_session_artifact_evidence_failed",
            exc,
        ) from None
    evidence = [_artifact_content_evidence(path) for path in paths]
    encoded = json.dumps(
        {
            "contract": "mnemos.native_session_artifact_evidence.v1",
            "source": str(getattr(source, "name", "") or ""),
            "canonical_session_id": str(
                session_info.canonical_session_id or session_info.session_id
            ),
            "artifacts": evidence,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def parse_discovered_session_result(
    source: Any,
    session_info: SessionInfo,
) -> SessionParseResult:
    """Return the non-lossy parse/disposition boundary used by formal capture."""

    parser = getattr(source, "parse_session_result", None)
    if callable(parser):
        bound_owner = getattr(
            source,
            "_framework_bound_session_artifact_evidence_hash",
            None,
        )
        if callable(bound_owner):
            bound_hash = str(bound_owner(session_info) or "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", bound_hash):
                raise NativeSourceContractError(
                    "native_session_evidence_hash_invalid"
                )
            result = parser(session_info)
            if (
                not isinstance(result, SessionParseResult)
                or result.artifact_evidence_hash != bound_hash
            ):
                raise NativeSourceContractError(
                    "native_session_bound_evidence_mismatch"
                )
            return result
        before_hash = native_session_artifact_evidence_hash(source, session_info)
        result = parser(session_info)
        if not isinstance(result, SessionParseResult):
            raise NativeSourceContractError("native_session_parse_result_invalid")
        after_hash = native_session_artifact_evidence_hash(source, session_info)
        if before_hash != after_hash:
            raise NativeSourceContractError(
                "native_session_artifact_changed_during_parse"
            )
        if (
            result.artifact_evidence_hash
            and result.artifact_evidence_hash != after_hash
        ):
            raise NativeSourceContractError(
                "native_session_self_signed_evidence_mismatch"
            )
        return SessionParseResult(
            turns=result.turns,
            disposition=result.disposition,
            reason_code=result.reason_code,
            artifact_evidence_hash=after_hash,
            infrastructure_attempt_count=(
                result.infrastructure_attempt_count
            ),
            recovered_infrastructure_failure=dict(
                result.recovered_infrastructure_failure
            ),
        )
    before_hash = native_session_artifact_evidence_hash(source, session_info)
    turns = tuple(parse_discovered_session(source, session_info) or [])
    after_hash = native_session_artifact_evidence_hash(source, session_info)
    if before_hash != after_hash:
        raise NativeSourceContractError(
            "native_session_artifact_changed_during_parse"
        )
    return SessionParseResult(
        turns=turns,
        disposition="parsed" if turns else "typed_empty",
        reason_code="native_turns_parsed" if turns else "valid_empty_native_session",
        artifact_evidence_hash=after_hash,
    )
