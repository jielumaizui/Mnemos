# -*- coding: utf-8 -*-
"""
KimiSource — Kimi Agent 同步插件

实现 AgentSource 接口，接入 SyncFramework。
支持 Kimi / Kimi Code 的归档机制：context.jsonl + context_1.jsonl，
以及官方 sessions/<workDirKey>/<sessionId>/agents/main/wire.jsonl 布局。
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Any

from core.agent_kit.source_support_manifest import get_agent_source_support_manifest
from core.sync_framework.agent_source import (
    NativeSourceContractError,
    SessionInfo,
    Turn,
)
from core.ops.durable_io import open_native_binary

from integrations.sources.base import BaseAgentSource, native_path_kind
from integrations.sources.kimi_payload import (
    native_json_line_ref,
    native_json_value_key,
    read_native_jsonl,
)

logger = logging.getLogger(__name__)


def _is_attachment_type(value: Any) -> bool:
    text = str(value or "").lower()
    return any(token in text for token in ("image", "file", "media", "attachment"))


def _attachment_from_item(item: Dict[str, Any], *, role: str) -> Dict[str, Any]:
    return {
        "role": role,
        "type": item.get("type", "attachment"),
        "name": item.get("name") or item.get("filename") or "",
        "path": item.get("path") or "",
        "url": item.get("url") or "",
        "mime_type": item.get("mime_type") or item.get("media_type") or "",
        "raw": item,
    }


@dataclass(frozen=True)
class _KimiArtifact:
    """One native Kimi capture unit with a stable parsing boundary."""

    source_path: Path
    artifact_dir: Path
    session_root: Path
    source_kind: str


def _kimi_subagent_marker(
    path: Path,
    *,
    sessions_dir: Optional[Path],
) -> Optional[Path]:
    """Return the nearest structurally valid ``subagents`` marker.

    Workspace, session, and worker identifiers may all literally be named
    ``subagents``.  A valid marker must therefore have a worker level beneath
    it and must not be the sessions root child or the main-agent wire path.
    Invalid inner candidates are skipped so an outer real marker can still
    classify a worker that is itself named ``subagents``.
    """
    for candidate in path.parents:
        if candidate.name != "subagents":
            continue
        tail = path.relative_to(candidate).parts
        if sessions_dir is not None and candidate.parent == sessions_dir:
            continue
        if len(tail) < 2 or tail[:2] == ("agents", "main"):
            continue
        return candidate
    return None


def _describe_kimi_v1_fixed_point_artifact(
    path: Path,
    *,
    sessions_dir: Optional[Path],
) -> _KimiArtifact:
    """Reconstruct the v1 classifier solely for fail-closed migration aliases.

    The fixed-point classifier treated most path segments literally named
    ``subagents`` as structural markers.  Current discovery must not repeat
    that bug, but its previously emitted bare and qualified identities remain
    part of the migration denominator.
    """
    is_wire = path.name == "wire.jsonl"
    subagents_dir = next(
        (ancestor for ancestor in path.parents if ancestor.name == "subagents"),
        None,
    )
    if (
        subagents_dir is not None
        and sessions_dir is not None
        and subagents_dir.parent == sessions_dir
    ):
        subagents_dir = None
    if subagents_dir is not None:
        candidate_root = subagents_dir.parent
        session_root = (
            candidate_root.parent if candidate_root.name == "agents" else candidate_root
        )
        return _KimiArtifact(
            source_path=path,
            artifact_dir=path.parent,
            session_root=session_root,
            source_kind="subagent_wire" if is_wire else "subagent_context",
        )
    if is_wire and path.parent.name == "main" and path.parent.parent.name == "agents":
        session_root = path.parent.parent.parent
    else:
        session_root = path.parent
    return _KimiArtifact(
        source_path=path,
        artifact_dir=path.parent,
        session_root=session_root,
        source_kind="main_wire" if is_wire else "main_context",
    )


def _describe_kimi_artifact(
    path: Path,
    *,
    sessions_dir: Optional[Path] = None,
) -> _KimiArtifact:
    """Classify a native file without folding a child artifact into its parent.

    A Kimi session root is still useful for parent lineage, but it is not the
    parsing root for every artifact below it.  Context segments aggregate only
    with their own sibling segments; each wire file remains a standalone
    artifact.
    """
    is_wire = path.name == "wire.jsonl"
    subagents_dir = _kimi_subagent_marker(path, sessions_dir=sessions_dir)
    if subagents_dir is not None:
        candidate_root = subagents_dir.parent
        session_root = (
            candidate_root.parent if candidate_root.name == "agents" else candidate_root
        )
        return _KimiArtifact(
            source_path=path,
            artifact_dir=path.parent,
            session_root=session_root,
            source_kind="subagent_wire" if is_wire else "subagent_context",
        )

    if is_wire and path.parent.name == "main" and path.parent.parent.name == "agents":
        session_root = path.parent.parent.parent
    else:
        session_root = path.parent
    return _KimiArtifact(
        source_path=path,
        artifact_dir=path.parent,
        session_root=session_root,
        source_kind="main_wire" if is_wire else "main_context",
    )


def _kimi_session_dir(path: Path, *, sessions_dir: Optional[Path] = None) -> Path:
    """Return the native parent session root without changing artifact identity."""
    return _describe_kimi_artifact(path, sessions_dir=sessions_dir).session_root


def _kimi_working_dir(path: Path, *, sessions_dir: Optional[Path] = None) -> Optional[str]:
    session_dir = _kimi_session_dir(path, sessions_dir=sessions_dir)
    parent = session_dir.parent
    if parent.name == "sessions" or parent == sessions_dir:
        return None
    return str(parent)


class KimiSource(BaseAgentSource):
    """Kimi 数据源插件"""

    _cap_tool_calls = True
    _cap_tool_results = True
    _cap_reasoning = "available"
    _cap_attachments = True
    _cap_source_fidelity = "full"
    _cap_memory_scope = "kimi_code_or_legacy_sessions"
    _cap_host_memory_default = "host_dependent_unknown"
    _cap_host_memory_effect = (
        "Kimi memory/context affects prompts; context/wire JSONL remains the passive capture source"
    )
    _cap_transcript_kind = "native_context_or_wire_jsonl"
    _cap_compression = "context_segments_merged_per_artifact; wires_independent"

    _default_extra_tags = ["has-reasoning=true"]

    @property
    def name(self) -> str:
        return "kimi"

    @property
    def model_tag(self) -> str:
        return "kimi-k2.5"

    @property
    def data_dir(self) -> Optional[Path]:
        """返回 Kimi 会话文件所在目录。

        会话位于 ``~/.kimi/sessions`` 或 ``~/.kimi-code/sessions``。Kimi Code
        新布局会把 wire 事件写到 ``<session>/agents/main/wire.jsonl``。
        """
        override: Optional[Path] = getattr(self, "_override_data_dir", None)
        if override is not None:
            child = override / "sessions"
            return (
                child
                if native_path_kind(child) != "missing"
                else override
            )

        candidates: List[Path] = []
        for env_name in ("KIMI_CODE_HOME", "KIMI_HOME"):
            value = os.getenv(env_name)
            if value:
                candidates.append(Path(value).expanduser())
        candidates.extend([Path.home() / ".kimi-code", Path.home() / ".kimi"])

        for candidate in candidates:
            if native_path_kind(candidate) == "missing":
                continue
            sessions = candidate / "sessions"
            if native_path_kind(sessions) != "missing":
                return sessions
            return candidate
        return None

    @property
    def trigger_strategy(self) -> Dict[str, Any]:
        return {
            "type": "hybrid",
            "events": ["modified", "created"],
            "debounce": 5.0,
            "recursive": True,
        }

    def _artifact_variants(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the one manifest-owned Kimi artifact layout contract."""
        native = get_agent_source_support_manifest().source("kimi").native
        resolution = native.get("artifact_resolution")
        if not isinstance(resolution, Mapping):
            raise RuntimeError("Kimi artifact resolution is not declared by the support manifest")
        variants = resolution.get("variants")
        if not isinstance(variants, (list, tuple)):
            raise RuntimeError("Kimi artifact variants are not declared by the support manifest")
        result: Dict[str, Mapping[str, Any]] = {}
        for variant in variants:
            if not isinstance(variant, Mapping):
                raise RuntimeError("Kimi artifact variant is malformed")
            source_kind = str(variant.get("source_kind") or "")
            if not source_kind or source_kind in result:
                raise RuntimeError("Kimi artifact variant source kind is malformed")
            result[source_kind] = variant
        return result

    @staticmethod
    def _matches_artifact_selector(
        path: Path,
        selector: str,
        *,
        sessions_dir: Path,
    ) -> bool:
        artifact = _describe_kimi_artifact(path, sessions_dir=sessions_dir)
        if selector == "outside_subagents":
            return artifact.source_kind == "main_context"
        if selector == "under_subagents":
            return artifact.source_kind in {"subagent_context", "subagent_wire"}
        if selector == "main_wire":
            return artifact.source_kind == "main_wire"
        return False

    @staticmethod
    def _variant_pattern(path_glob: str) -> str:
        prefix = "sessions/"
        return path_glob[len(prefix) :] if path_glob.startswith(prefix) else path_glob

    def _context_files(self, artifact_dir: Path) -> List[Path]:
        return sorted(
            (
                path
                for path in artifact_dir.glob("context*.jsonl")
                if native_path_kind(path) == "file"
            ),
            key=self._context_file_sort_key,
        )

    def _artifact_files(self, artifact: _KimiArtifact) -> List[Path]:
        if artifact.source_kind.endswith("context"):
            return self._context_files(artifact.artifact_dir)
        return (
            [artifact.source_path]
            if native_path_kind(artifact.source_path) == "file"
            else []
        )

    def _artifact_session_info(
        self,
        artifact: _KimiArtifact,
        *,
        sessions_dir: Path,
        variant: Mapping[str, Any],
    ) -> Optional[SessionInfo]:
        files = self._artifact_files(artifact)
        if not files:
            return None
        source_path = artifact.source_path
        if artifact.source_kind.endswith("context"):
            source_path = next((path for path in files if path.name == "context.jsonl"), files[-1])
            artifact = _KimiArtifact(
                source_path=source_path,
                artifact_dir=artifact.artifact_dir,
                session_root=artifact.session_root,
                source_kind=artifact.source_kind,
            )
        try:
            relative = artifact.artifact_dir.relative_to(sessions_dir).as_posix()
        except ValueError:
            relative = artifact.artifact_dir.as_posix()
        digest_v1 = hashlib.sha256(
            f"kimi-native-artifact-v1:{artifact.source_kind}:{relative}".encode("utf-8")
        ).hexdigest()[:24]
        digest = hashlib.sha256(
            f"kimi-native-artifact-v2:{artifact.source_kind}:{relative}".encode("utf-8")
        ).hexdigest()[:24]
        native_session_id = artifact.session_root.name
        canonical_id = f"{native_session_id}::{artifact.source_kind}::{digest}"
        legacy_qualified_id = (
            f"{native_session_id}::{artifact.source_kind}::{digest_v1}"
        )
        fixed_point_artifact = _describe_kimi_v1_fixed_point_artifact(
            source_path,
            sessions_dir=sessions_dir,
        )
        try:
            fixed_point_relative = fixed_point_artifact.artifact_dir.relative_to(
                sessions_dir
            ).as_posix()
        except ValueError:
            fixed_point_relative = fixed_point_artifact.artifact_dir.as_posix()
        fixed_point_native_id = fixed_point_artifact.session_root.name
        fixed_point_digest = hashlib.sha256(
            (
                "kimi-native-artifact-v1:"
                f"{fixed_point_artifact.source_kind}:{fixed_point_relative}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        fixed_point_qualified_id = (
            f"{fixed_point_native_id}::{fixed_point_artifact.source_kind}::"
            f"{fixed_point_digest}"
        )
        metadata: Dict[str, Any] = {
            "artifact_identity_version": "kimi-native-artifact-v2",
            "source_artifact_id": f"kimi-artifact-{digest}",
            "native_session_id": native_session_id,
            "artifact_aggregation": str(variant["aggregation"]),
            "identity_contract_version": "kimi-native-artifact-v2",
            "identity_reconciliation_required": True,
            "legacy_canonical_session_ids": list(
                dict.fromkeys(
                    [
                        native_session_id,
                        legacy_qualified_id,
                        fixed_point_native_id,
                        fixed_point_qualified_id,
                    ]
                )
            ),
        }
        parent_relation = str(variant["parent_relation"])
        if parent_relation != "none":
            try:
                parent_relative = artifact.session_root.relative_to(
                    sessions_dir
                ).as_posix()
            except ValueError:
                parent_relative = artifact.session_root.as_posix()
            parent_digest = hashlib.sha256(
                f"kimi-native-artifact-v2:main_context:{parent_relative}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
            metadata["parent_session_id"] = native_session_id
            metadata["parent_relation"] = parent_relation
            metadata["canonical_parent_session_id"] = (
                f"{native_session_id}::main_context::{parent_digest}"
            )
            metadata["parent_source_artifact_id"] = (
                f"kimi-artifact-{parent_digest}"
            )
        try:
            mtime = max(path.stat().st_mtime for path in files)
        except OSError:
            raise NativeSourceContractError(
                "native_kimi_artifact_stat_failed"
            ) from None
        return SessionInfo(
            session_id=canonical_id,
            source_path=source_path,
            working_dir=_kimi_working_dir(source_path, sessions_dir=sessions_dir),
            mtime=mtime,
            canonical_session_id=canonical_id,
            session_aliases=[native_session_id],
            source_kind=artifact.source_kind,
            metadata=metadata,
        )

    def discover_sessions(self) -> List[SessionInfo]:
        """Discover each main, subagent, and wire artifact independently."""
        base = self.data_dir
        if not base:
            return []
        sessions_dir = self._sessions_dir(base)
        if native_path_kind(sessions_dir) == "missing":
            return []
        variants = self._artifact_variants()
        artifacts: Dict[tuple[str, Path], _KimiArtifact] = {}
        for source_kind, variant in variants.items():
            path_glob = str(variant.get("path_glob") or "")
            selector = str(variant.get("selector") or "")
            if not path_glob or not selector:
                raise RuntimeError("Kimi artifact variant is incomplete")
            for path in sessions_dir.glob(self._variant_pattern(path_glob)):
                if native_path_kind(path) != "file" or not self._matches_artifact_selector(
                    path,
                    selector,
                    sessions_dir=sessions_dir,
                ):
                    continue
                artifact = _describe_kimi_artifact(path, sessions_dir=sessions_dir)
                if artifact.source_kind != source_kind:
                    continue
                artifacts[(source_kind, artifact.artifact_dir)] = artifact

        sessions: List[SessionInfo] = []
        for artifact in artifacts.values():
            info = self._artifact_session_info(
                artifact,
                sessions_dir=sessions_dir,
                variant=variants[artifact.source_kind],
            )
            if info is not None:
                sessions.append(info)
        sessions.sort(key=lambda item: (-(item.mtime or 0), str(item.source_path)))
        return sessions

    def parse_session(self, session_info: SessionInfo) -> List[Turn]:
        """Parse exactly the artifact identity returned by discovery."""
        source_kind = session_info.source_kind or _describe_kimi_artifact(
            session_info.source_path
        ).source_kind
        if source_kind.endswith("context"):
            return self._parse_context_turns(session_info.source_path)
        if source_kind.endswith("wire"):
            return self._parse_wire_turns(session_info.source_path)
        return []

    def native_artifact_paths(self, session_info: SessionInfo) -> List[Path]:
        """Declare the exact context segments or wire file consumed by parsing."""
        artifact = _describe_kimi_artifact(session_info.source_path)
        source_kind = session_info.source_kind or artifact.source_kind
        if source_kind.endswith("context"):
            return self._context_files(artifact.artifact_dir)
        return (
            [session_info.source_path]
            if native_path_kind(session_info.source_path) == "file"
            else []
        )

    def parse_turns(self, session_path: Path) -> List[Turn]:
        """Compatibility parser for one native Kimi artifact path."""
        artifact = _describe_kimi_artifact(session_path)
        if artifact.source_kind.endswith("wire"):
            return self._parse_wire_turns(session_path)
        return self._parse_context_turns(session_path)

    def _parse_context_turns(self, session_path: Path) -> List[Turn]:
        artifact = _describe_kimi_artifact(session_path)
        context_files = self._context_files(artifact.artifact_dir)
        all_messages = self._read_all_context_files(artifact.artifact_dir)
        return self._pair_messages_to_turns(
            all_messages,
            [str(path) for path in context_files],
        )

    @staticmethod
    def _context_file_sort_key(path: Path) -> tuple:
        """Order numeric archives, other stable segments, then the active file."""
        m = re.match(r"context_(\d+)\.jsonl$", path.name)
        if m:
            return (0, int(m.group(1)), path.name)
        if path.name == "context.jsonl":
            return (2, 0, "")  # 当前活跃文件始终最后读
        return (1, 0, path.name)

    @staticmethod
    def _native_event_identity(message: Mapping[str, Any]) -> Optional[str]:
        """Return a duplicate key only when the native source proves one.

        Identical text without a native event id can be two real turns, so it
        is intentionally preserved.  Archive copies carrying the same native
        id are safe to merge at the artifact seam.
        """
        candidates: List[Mapping[str, Any]] = [message]
        nested = message.get("message")
        if isinstance(nested, Mapping):
            candidates.append(nested)
        for candidate in candidates:
            for key in ("event_id", "eventId", "message_id", "messageId", "uuid"):
                value = candidate.get(key)
                if isinstance(value, (str, int)) and str(value):
                    return f"{key}:{value}"
        return None

    @classmethod
    def _canonical_json_value_key(cls, value: Any) -> tuple:
        """Return a type-sensitive structural key for JSON value equality.

        JSON numbers compare by numeric value, so ``1`` and ``1.0`` are the
        same value.  Booleans remain a distinct JSON type even though Python
        makes ``True == 1``.  Non-finite decoder extensions are retained as
        typed malformed values instead of being collapsed into valid numbers.
        """
        lossless_key = native_json_value_key(value)
        if lossless_key is not None:
            return lossless_key
        if value is None:
            return ("null",)
        if isinstance(value, bool):
            return ("boolean", value)
        if isinstance(value, int):
            return ("number", Decimal(value))
        if isinstance(value, float):
            if math.isfinite(value):
                return ("number", Decimal(str(value)))
            return ("non_finite_number", repr(value))
        if isinstance(value, str):
            return ("string", value)
        if isinstance(value, list):
            return (
                "array",
                tuple(cls._canonical_json_value_key(item) for item in value),
            )
        if isinstance(value, dict):
            return (
                "object",
                tuple(
                    (key, cls._canonical_json_value_key(item))
                    for key, item in sorted(value.items())
                ),
            )
        return ("non_json_value", type(value).__name__, repr(value))

    def _read_all_context_files(self, artifact_dir: Path) -> List[Dict[str, Any]]:
        """Merge sibling context segments in declared order without wire bodies."""
        all_messages: List[Dict[str, Any]] = []
        seen_native_events: Dict[str, Dict[tuple, str]] = {}
        for context_file in self._context_files(artifact_dir):
            for message in read_native_jsonl(context_file):
                event_id = self._native_event_identity(message)
                if event_id is not None:
                    payload_key = self._canonical_json_value_key(message)
                    seen_payloads = seen_native_events.setdefault(event_id, {})
                    if payload_key in seen_payloads:
                        continue
                    if seen_payloads:
                        message["_mnemos_native_identity_conflict"] = {
                            "native_event_identity": event_id,
                            "first_source_file": next(iter(seen_payloads.values())),
                            "current_source_file": str(context_file),
                        }
                    seen_payloads[payload_key] = str(context_file)
                all_messages.append(message)
        return all_messages

    def get_session_state(self, session_info: SessionInfo) -> Optional[Dict[str, Any]]:  # noqa
        """Return state for the exact discovered artifact, never its siblings."""
        artifact = _describe_kimi_artifact(session_info.source_path)
        if (session_info.source_kind or artifact.source_kind).endswith("context"):
            files = self._context_files(artifact.artifact_dir)
        else:
            files = [session_info.source_path]
        return self._compute_content_state(files)

    def _compute_content_state(self, files: List[Path]) -> Optional[Dict[str, Any]]:
        """Hash ordered artifact bytes while exposing mtime only as a poll hint."""
        ordered = sorted(files, key=self._context_file_sort_key)
        digest = hashlib.sha256()
        total_size = 0
        max_mtime = 0.0
        file_count = 0
        for path in ordered:
            try:
                with open_native_binary(path) as handle:
                    data = handle.read()
                    metadata = os.fstat(handle.fileno())
            except OSError:
                raise NativeSourceContractError(
                    "native_kimi_artifact_read_failed"
                ) from None
            name = path.name.encode("utf-8", errors="surrogateescape")
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            total_size += len(data)
            max_mtime = max(max_mtime, metadata.st_mtime)
            file_count += 1
        if file_count == 0:
            return None
        return {
            "mtime": max_mtime,
            "size": total_size,
            "file_count": file_count,
            "fingerprint": digest.hexdigest(),
            "fingerprint_contract": "kimi-ordered-artifact-bytes-v2",
        }

    _SYSTEM_ROLES = ("_system_prompt", "_checkpoint", "_usage", "system")

    @staticmethod
    def _is_system_role(role: str) -> bool:
        return role in KimiSource._SYSTEM_ROLES

    def _message_timestamp(
        self,
        msg: Dict[str, Any],
        state: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """Extract a stable message timestamp across known Kimi archive shapes."""
        for key in ("timestamp", "created_at", "createdAt", "time", "ts"):
            value = msg.get(key)
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                if state is not None:
                    self._record_malformed(
                        state,
                        "malformed_native_timestamp",
                        {"field": key, "value": value},
                    )
                return None
            if isinstance(value, (int, float)):
                try:
                    epoch = value / 1000 if value > 10_000_000_000 else value
                    return datetime.fromtimestamp(
                        epoch,
                        tz=timezone.utc,
                    ).isoformat()
                except (OSError, OverflowError, ValueError):
                    if state is not None:
                        self._record_malformed(
                            state,
                            "malformed_native_timestamp",
                            {"field": key, "value": value},
                        )
                    return None
            if isinstance(value, str):
                return value
            if state is not None:
                self._record_malformed(
                    state,
                    "malformed_native_timestamp",
                    {"field": key, "value": value},
                )
            return None
        return None

    def _build_completeness(self, state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "visible_text": "full",
            "tool_calls": "full" if state["tool_calls"] else "unavailable",
            "tool_results": "full" if state["tool_results"] else "unavailable",
            "reasoning": "full" if state["reasoning"] else "unavailable",
            "attachments": "full" if state["attachments"] else "unavailable",
            "truncated": False,
            "loss_reasons": list(state["loss_reasons"]),
        }

    def _flush_turn(self, state: Dict[str, Any], turn_number: int) -> Turn:
        return Turn(
            turn_number=turn_number,
            user_content=state["user_content"],
            assistant_content=state["assistant_content"],
            timestamp=state.get("timestamp") or None,
            metadata=dict(state["meta"]),
            tool_calls=list(state["tool_calls"]),
            tool_results=list(state["tool_results"]),
            reasoning=state["reasoning"],
            attachments=list(state["attachments"]),
            raw_event_refs=list(state["raw_events"]),
            source_files=list(state["source_files"]),
            completeness=self._build_completeness(state),
        )

    def _reset_turn_state(self, source_files: Optional[List[str]]) -> Dict[str, Any]:
        return {
            "user_content": "",
            "assistant_content": "",
            "timestamp": "",
            "meta": {},
            "tool_calls": [],
            "tool_results": [],
            "reasoning": "",
            "attachments": [],
            "raw_events": [],
            "source_files": list(source_files or []),
            "loss_reasons": [],
        }

    @staticmethod
    def _record_malformed(
        state: Dict[str, Any],
        event_type: str,
        raw: Any,
        *,
        role: str = "",
    ) -> None:
        ref: Dict[str, Any] = {"event_type": event_type, "raw": raw}
        if role:
            ref["role"] = role
        state["raw_events"].append(ref)
        state["loss_reasons"].append(event_type)

    @staticmethod
    def _native_envelope_ref(
        event_type: str,
        raw: Any,
        *,
        source_record: Any,
        role: str = "",
    ) -> Dict[str, Any]:
        ref: Dict[str, Any] = {"event_type": event_type, "raw": raw}
        if role:
            ref["role"] = role
        source_line = native_json_line_ref(source_record)
        if source_line is not None:
            ref["source_line"] = source_line
        return ref

    def _parse_user_content(
        self, msg: Dict[str, Any], state: Dict[str, Any]
    ) -> str:
        raw = msg.get("content", "")
        if not isinstance(raw, list):
            if isinstance(raw, str):
                return raw
            self._record_malformed(
                state,
                "malformed_user_content",
                raw,
                role="user",
            )
            return ""

        texts: List[str] = []
        for item in raw:
            if not isinstance(item, dict):
                self._record_malformed(
                    state,
                    "malformed_user_block",
                    item,
                    role="user",
                )
                continue
            itype = item.get("type", "")
            if itype == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    texts.append(text)
                else:
                    self._record_malformed(
                        state,
                        "malformed_user_text",
                        text,
                        role="user",
                    )
            elif _is_attachment_type(itype):
                state["attachments"].append(_attachment_from_item(item, role="user"))
                state["raw_events"].append({"role": "user", "event_type": itype, "raw": item})
            else:
                state["raw_events"].append({"role": "user", "event_type": itype, "raw": item})
                state["loss_reasons"].append(f"user_unknown_block:{itype}")
        return "\n".join(texts)

    def _parse_assistant_content(
        self, msg: Dict[str, Any], state: Dict[str, Any]
    ) -> None:
        parts = msg.get("content", [])
        texts = []
        reasoning = ""

        if isinstance(parts, list):
            for p in parts:
                if not isinstance(p, dict):
                    self._record_malformed(
                        state,
                        "malformed_assistant_block",
                        p,
                        role="assistant",
                    )
                    continue
                ptype = p.get("type", "")
                if ptype == "text":
                    text = p.get("text", "")
                    if isinstance(text, str):
                        texts.append(text)
                    else:
                        self._record_malformed(
                            state,
                            "malformed_assistant_text",
                            text,
                            role="assistant",
                        )
                elif ptype == "think":
                    think = p.get("think", "")
                    if isinstance(think, str):
                        reasoning = (
                            reasoning + "\n" + think if reasoning else think
                        )
                    else:
                        self._record_malformed(
                            state,
                            "malformed_assistant_think",
                            think,
                            role="assistant",
                        )
                elif ptype == "tool_use":
                    state["tool_calls"].append(
                        {
                            "name": p.get("name", "unknown"),
                            "input": p.get("input", {}),
                            "id": p.get("id", ""),
                        }
                    )
                elif _is_attachment_type(ptype):
                    state["attachments"].append(_attachment_from_item(p, role="assistant"))
                    state["raw_events"].append({"role": "assistant", "event_type": ptype, "raw": p})
                else:
                    state["raw_events"].append({"role": "assistant", "event_type": ptype, "raw": p})
                    state["loss_reasons"].append(f"assistant_unknown_block:{ptype}")
        elif isinstance(parts, str):
            texts.append(parts)
        else:
            self._record_malformed(
                state,
                "malformed_assistant_content",
                parts,
                role="assistant",
            )

        assistant_text = "\n\n".join(texts)
        if assistant_text:
            state["assistant_content"] = (
                state["assistant_content"] + "\n\n" + assistant_text
                if state["assistant_content"]
                else assistant_text
            )
        if reasoning:
            state["reasoning"] = (
                state["reasoning"] + "\n" + reasoning
                if state["reasoning"]
                else reasoning
            )
            state["meta"]["reasoning"] = state["reasoning"]

    def _parse_tool_content(self, msg: Dict[str, Any], state: Dict[str, Any]) -> None:
        tool_content = msg.get("content", "")
        tool_texts = []
        if isinstance(tool_content, list):
            for item in tool_content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text = item.get("text", "")
                    if isinstance(text, str):
                        tool_texts.append(text)
                    else:
                        self._record_malformed(
                            state,
                            "malformed_tool_text",
                            text,
                            role="tool",
                        )
                elif isinstance(item, dict) and _is_attachment_type(item.get("type")):
                    state["attachments"].append(_attachment_from_item(item, role="tool"))
                    state["raw_events"].append({"role": "tool", "event_type": item.get("type"), "raw": item})
                else:
                    state["raw_events"].append({"role": "tool", "event_type": "unknown", "raw": item})
        elif isinstance(tool_content, str):
            tool_texts = [tool_content]
        else:
            self._record_malformed(
                state,
                "malformed_tool_content",
                tool_content,
                role="tool",
            )

        tool_result_text = "\n".join(tool_texts)
        if tool_result_text:
            state["tool_results"].append(
                {
                    "tool_call_id": msg.get("tool_call_id", ""),
                    "content": tool_result_text,
                    "name": msg.get("name", ""),
                }
            )

    def _maybe_flush_turn(
        self, turns: List[Turn], state: Dict[str, Any], turn_number: int
    ) -> int:
        if not any(
            (
                state["user_content"],
                state["assistant_content"],
                state["tool_calls"],
                state["tool_results"],
                state["reasoning"],
                state["attachments"],
                state["raw_events"],
            )
        ):
            return turn_number
        turns.append(self._flush_turn(state, turn_number))
        state["loss_reasons"] = []
        return turn_number + 1

    def _pair_messages_to_turns(
        self, messages: List[Dict[str, Any]], source_files: Optional[List[str]] = None
    ) -> List[Turn]:
        """将消息列表配对为 Turn 列表 — 完整录入版（P0-6）"""
        turns: List[Turn] = []
        state = self._reset_turn_state(source_files)
        turn_number = 0

        for msg in messages:
            decode_ref = msg.get("_mnemos_raw_event_ref")
            if isinstance(decode_ref, dict):
                state["raw_events"].append(
                    {
                        "event_type": "native_jsonl_decode_error",
                        "raw": decode_ref,
                    }
                )
                state["loss_reasons"].append(
                    f"native_jsonl_{decode_ref.get('decode_error') or 'decode_error'}"
                )
                continue
            role = msg.get("role", "")
            identity_conflict = msg.get("_mnemos_native_identity_conflict")
            native_message = {
                key: value
                for key, value in msg.items()
                if key != "_mnemos_native_identity_conflict"
            }

            if self._is_system_role(role):
                state["raw_events"].append(
                    self._native_envelope_ref(
                        "system",
                        native_message,
                        source_record=msg,
                        role=role,
                    )
                )
                continue

            if role == "user":
                turn_number = self._maybe_flush_turn(turns, state, turn_number)
                state = self._reset_turn_state(source_files)
                state["raw_events"].append(
                    self._native_envelope_ref(
                        "native_context_message",
                        native_message,
                        source_record=msg,
                        role="user",
                    )
                )
                if isinstance(identity_conflict, dict):
                    state["raw_events"].append(
                        {
                            "role": "user",
                            "event_type": "native_event_id_payload_conflict",
                            "raw": identity_conflict,
                        }
                    )
                    state["loss_reasons"].append("native_event_id_payload_conflict")
                native_event_id = self._native_event_identity(msg)
                if native_event_id:
                    state["meta"]["native_event_id"] = native_event_id
                timestamp = self._message_timestamp(msg, state)
                if timestamp:
                    state["timestamp"] = timestamp
                    state["meta"]["timestamp"] = timestamp
                state["user_content"] = self._parse_user_content(msg, state)

            elif role == "assistant":
                state["raw_events"].append(
                    self._native_envelope_ref(
                        "native_context_message",
                        native_message,
                        source_record=msg,
                        role="assistant",
                    )
                )
                if isinstance(identity_conflict, dict):
                    state["raw_events"].append(
                        {
                            "role": "assistant",
                            "event_type": "native_event_id_payload_conflict",
                            "raw": identity_conflict,
                        }
                    )
                    state["loss_reasons"].append("native_event_id_payload_conflict")
                timestamp = self._message_timestamp(msg, state)
                if timestamp and not state.get("timestamp"):
                    state["timestamp"] = timestamp
                    state["meta"]["timestamp"] = timestamp
                self._parse_assistant_content(msg, state)

            elif role == "tool":
                state["raw_events"].append(
                    self._native_envelope_ref(
                        "native_context_message",
                        native_message,
                        source_record=msg,
                        role="tool",
                    )
                )
                if isinstance(identity_conflict, dict):
                    state["raw_events"].append(
                        {
                            "role": "tool",
                            "event_type": "native_event_id_payload_conflict",
                            "raw": identity_conflict,
                        }
                    )
                    state["loss_reasons"].append("native_event_id_payload_conflict")
                self._parse_tool_content(msg, state)

            else:
                state["raw_events"].append(
                    self._native_envelope_ref(
                        "unknown_context_role",
                        native_message,
                        source_record=msg,
                        role=role,
                    )
                )
                state["loss_reasons"].append(
                    f"unknown_context_role:{role or 'missing'}"
                )

        if any(
            (
                state["user_content"],
                state["assistant_content"],
                state["tool_calls"],
                state["tool_results"],
                state["reasoning"],
                state["attachments"],
                state["raw_events"],
            )
        ):
            turns.append(self._flush_turn(state, turn_number))

        return turns

    def _parse_wire_turns(self, wire_path: Path) -> List[Turn]:
        events = read_native_jsonl(wire_path)
        turns: List[Turn] = []
        state = self._reset_turn_state([str(wire_path)])
        turn_number = 0

        def flush_if_needed() -> None:
            nonlocal state, turn_number
            if any(
                (
                    state["user_content"],
                    state["assistant_content"],
                    state["tool_calls"],
                    state["tool_results"],
                    state["reasoning"],
                    state["attachments"],
                    state["raw_events"],
                )
            ):
                turns.append(self._flush_turn(state, turn_number))
                turn_number += 1
            state = self._reset_turn_state([str(wire_path)])

        for event in events:
            decode_ref = event.get("_mnemos_raw_event_ref")
            if isinstance(decode_ref, dict):
                state["raw_events"].append(
                    {
                        "event_type": "native_jsonl_decode_error",
                        "raw": decode_ref,
                    }
                )
                state["loss_reasons"].append(
                    f"native_jsonl_{decode_ref.get('decode_error') or 'decode_error'}"
                )
                continue
            message = event.get("message", {})
            if not isinstance(message, dict):
                state["raw_events"].append(
                    self._native_envelope_ref(
                        "native_wire_event",
                        event,
                        source_record=event,
                    )
                )
                state["raw_events"].append(
                    {
                        "event_type": "malformed_wire_message",
                        "raw": message,
                        "envelope": event,
                    }
                )
                state["loss_reasons"].append("malformed_wire_message")
                continue
            mtype = message.get("type", "")
            payload = message.get("payload", {})
            malformed_payload: Any = None
            if not isinstance(payload, dict):
                malformed_payload = payload
                payload = {}

            if mtype == "TurnBegin":
                flush_if_needed()
            state["raw_events"].append(
                self._native_envelope_ref(
                    "native_wire_event",
                    event,
                    source_record=event,
                )
            )
            if malformed_payload is not None:
                state["raw_events"].append(
                    {
                        "event_type": "malformed_wire_payload",
                        "wire_event_type": mtype,
                        "raw": malformed_payload,
                    }
                )
                state["loss_reasons"].append("malformed_wire_payload")

            if mtype == "TurnBegin":
                native_event_id = self._native_event_identity(event)
                if native_event_id:
                    state["meta"]["native_event_id"] = native_event_id
                user_input = payload.get("user_input", [])
                state["user_content"] = self._wire_text_and_attachments(
                    user_input, state, role="user"
                )
                timestamp = event.get("timestamp")
                if timestamp is not None and timestamp != "":
                    normalized_timestamp = self._message_timestamp(
                        {"timestamp": timestamp},
                        state,
                    )
                    if normalized_timestamp:
                        state["timestamp"] = normalized_timestamp
                        state["meta"]["timestamp"] = normalized_timestamp
            elif mtype == "ContentPart":
                ptype = payload.get("type", "")
                if ptype == "text":
                    text = payload.get("text", "")
                    if isinstance(text, str) and text:
                        state["assistant_content"] = (
                            state["assistant_content"] + "\n\n" + text
                            if state["assistant_content"]
                            else text
                        )
                    elif not isinstance(text, str):
                        self._record_malformed(
                            state,
                            "malformed_wire_text",
                            text,
                            role="assistant",
                        )
                elif ptype == "think":
                    think = payload.get("think", "")
                    if isinstance(think, str) and think:
                        state["reasoning"] = (
                            state["reasoning"] + "\n" + think if state["reasoning"] else think
                        )
                        state["meta"]["reasoning"] = state["reasoning"]
                    elif not isinstance(think, str):
                        self._record_malformed(
                            state,
                            "malformed_wire_think",
                            think,
                            role="assistant",
                        )
                elif _is_attachment_type(ptype):
                    state["attachments"].append(_attachment_from_item(payload, role="assistant"))
                else:
                    state["raw_events"].append({"role": "assistant", "event_type": ptype, "raw": payload})
                    state["loss_reasons"].append(f"wire_unknown_content:{ptype}")
            elif mtype == "ToolCall":
                function = payload.get("function", {})
                function = function if isinstance(function, dict) else {}
                state["tool_calls"].append(
                    {
                        "id": payload.get("id", ""),
                        "name": function.get("name") or payload.get("name", ""),
                        "input": function.get("arguments") or payload.get("arguments", {}),
                    }
                )
            elif mtype == "ToolResult":
                state["tool_results"].append(
                    {
                        "tool_call_id": payload.get("tool_call_id", ""),
                        "content": payload.get("return_value") or payload.get("content", ""),
                        "raw": payload,
                    }
                )
            else:
                state["raw_events"].append({"event_type": mtype or "unknown", "raw": event})

        flush_if_needed()
        return turns

    def _wire_text_and_attachments(
        self, content: Any, state: Dict[str, Any], *, role: str
    ) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            self._record_malformed(
                state,
                f"malformed_wire_{role}_content",
                content,
                role=role,
            )
            return ""
        texts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                self._record_malformed(
                    state,
                    f"malformed_wire_{role}_block",
                    item,
                    role=role,
                )
                continue
            itype = item.get("type", "")
            if itype == "text":
                text = item.get("text", "")
                if isinstance(text, str):
                    texts.append(text)
                else:
                    self._record_malformed(
                        state,
                        f"malformed_wire_{role}_text",
                        text,
                        role=role,
                    )
            elif _is_attachment_type(itype):
                state["attachments"].append(_attachment_from_item(item, role=role))
                state["raw_events"].append({"role": role, "event_type": itype, "raw": item})
            else:
                state["raw_events"].append({"role": role, "event_type": itype, "raw": item})
                state["loss_reasons"].append(f"wire_unknown_user_block:{itype}")
        return "\n".join(text for text in texts if text)

    def build_extra_tags(self, turn: Turn) -> List[str]:
        """Kimi 自定义标签"""
        tags = []
        if turn.metadata.get("reasoning"):
            tags.append("has-reasoning=true")
        return tags
