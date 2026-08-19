# -*- coding: utf-8 -*-
"""Common base classes and helpers for AgentSource implementations.

Corresponds to S10 in MNEMOS_CODE_AUDIT_2026_06_24.md: reduce duplication
across integrations/sources/*.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from core.ops.durable_io import DurableIOError, inspect_path_kind
from core.sync_framework.agent_source import (
    AgentSource,
    NativeSourceContractError,
    SessionInfo,
    Turn,
    native_artifact_content_state,
)
from core.ops.durable_io import open_native_text


def native_path_kind(path: Path) -> str:
    """Inspect one authoritative native path without folding IO failure into absence."""

    try:
        return inspect_path_kind(Path(path))
    except DurableIOError:
        raise NativeSourceContractError(
            "native_path_inspection_unavailable"
        ) from None


def native_mapping_residual_ref(
    value: Any,
    *,
    consumed_keys: Set[str],
    event_type: str,
    **attributes: Any,
) -> Optional[Dict[str, Any]]:
    """Return the unconsumed portion of one native mapping.

    Parsers may project declared fields into :class:`Turn`, but every other
    native field remains source evidence.  Keeping the residual separate avoids
    both silent field loss and copying an entire structured payload twice.
    """

    if not isinstance(value, dict):
        return {
            "event_type": event_type,
            **attributes,
            "raw": value,
            "decode_error": "non_object_native_mapping",
        }
    residual = {key: item for key, item in value.items() if key not in consumed_keys}
    if not residual:
        return None
    return {
        "event_type": event_type,
        **attributes,
        "raw": residual,
    }


def attach_native_container_residual(
    turns: List[Turn],
    value: Any,
    *,
    consumed_keys: Set[str],
    source_name: str,
) -> List[Turn]:
    """Attach one container residual to exactly one turn.

    A valid container can contain no visible messages.  In that case a raw-only
    turn is emitted so the source is not misclassified as a genuinely empty
    native session.
    """

    ref = native_mapping_residual_ref(
        value,
        consumed_keys=consumed_keys,
        event_type="native_session_container_residual",
        source=source_name,
    )
    if ref is None:
        return turns
    if not turns:
        turns.append(
            Turn(
                turn_number=0,
                user_content="",
                assistant_content="",
                metadata={"source": source_name},
                raw_event_refs=[ref],
                completeness={
                    "visible_text": "unavailable",
                    "tool_calls": "unavailable",
                    "tool_results": "unavailable",
                    "reasoning": "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": ["native_container_without_visible_messages"],
                },
            )
        )
    else:
        turns[0].raw_event_refs.insert(0, ref)
    return turns


def stable_path_session_id(
    prefix: str,
    root: Path,
    path: Path,
    *,
    native_id: str = "",
) -> str:
    """Return a scan-root-independent opaque identity for one exact path."""

    resolved_path = Path(path).expanduser().resolve(strict=False)
    material = "\0".join(
        (str(prefix).lower(), str(resolved_path), str(native_id))
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-path-{digest}"


def lossless_text_message_turns(
    messages: Any,
    *,
    source_name: str,
) -> List[Turn]:
    """Normalize simple role/content messages while preserving every record."""

    if not isinstance(messages, list):
        messages = [messages]
    turns: List[Turn] = []
    user_content = ""
    assistant_content = ""
    raw_event_refs: List[Dict[str, Any]] = []
    loss_reasons: List[str] = []

    def flush() -> None:
        nonlocal user_content, assistant_content, raw_event_refs, loss_reasons
        if not (user_content or assistant_content or raw_event_refs):
            return
        turns.append(
            Turn(
                turn_number=len(turns),
                user_content=user_content,
                assistant_content=assistant_content,
                metadata={"source": source_name},
                raw_event_refs=list(raw_event_refs),
                completeness={
                    "visible_text": "full",
                    "tool_calls": "unavailable",
                    "tool_results": "unavailable",
                    "reasoning": "unavailable",
                    "attachments": "unavailable",
                    "truncated": False,
                    "loss_reasons": list(dict.fromkeys(loss_reasons)),
                },
            )
        )
        user_content = ""
        assistant_content = ""
        raw_event_refs = []
        loss_reasons = []

    for index, message in enumerate(messages):
        raw_event_refs.append(
            {
                "event_type": "native_message",
                "index": index,
                "raw": message,
            }
        )
        if not isinstance(message, dict):
            loss_reasons.append("non_object_native_message")
            continue
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("text") not in (None, "")
            )
            if any(
                not isinstance(part, dict)
                or set(part) - {"type", "text"}
                or str(part.get("type") or "") not in {"", "text"}
                for part in content
            ):
                loss_reasons.append("native_content_part_preserved_as_raw")
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)
            loss_reasons.append("native_content_value_preserved_as_raw")

        if role == "user":
            if user_content or assistant_content:
                current_ref = raw_event_refs.pop()
                flush()
                raw_event_refs.append(current_ref)
            user_content = text
        elif role in {"assistant", "model", "ai"}:
            assistant_content = (
                f"{assistant_content}\n{text}"
                if assistant_content and text
                else assistant_content or text
            )
        else:
            loss_reasons.append(f"unknown_native_role:{role or 'empty'}")

    flush()
    return turns


def _derive_session_id(
    path: Path,
    session_id_func: Optional[Callable[[Path], str]],
    session_id_from: str,
) -> str:
    """根据策略从路径推导 session_id。"""
    if session_id_func is not None:
        return session_id_func(path)
    if session_id_from == "name":
        return path.name
    return path.stem


def _derive_working_dir(path: Path, root: Path, working_dir_from: str) -> str:
    """根据策略从路径推导 working_dir。"""
    if working_dir_from == "root":
        return str(root)
    if working_dir_from == "parent.parent":
        return str(path.parent.parent)
    return str(path.parent)


class BaseAgentSource(AgentSource):
    """Minimal base for AgentSource plugins.

    Provides safe defaults for ``build_extra_tags``, shared helpers for
    computing session state fingerprints, directory resolution, glob-based
    session discovery, and a configurable ``completeness_capabilities``
    implementation.

    Subclasses can either:
    - override ``completeness_capabilities`` / ``build_extra_tags`` as before, or
    - set the class attributes below and rely on the default implementations.
    """

    # Capability flags used by the default completeness_capabilities().
    _cap_visible_text: bool = True
    _cap_tool_calls: Any = False
    _cap_tool_results: Any = False
    _cap_reasoning: Any = False  # bool or "unknown" / "available" / "not_available"
    _cap_attachments: Any = False
    _cap_raw_files: bool = True
    _cap_source_fidelity: Any = "full"  # "full" / "derived" / "experimental"
    _cap_memory_scope: str = "unknown"
    _cap_host_memory_default: str = "unknown"
    _cap_host_memory_effect: str = "unknown"
    _cap_transcript_kind: str = "unknown"
    _cap_compression: str = "unknown"
    _cap_dedupe_strategy: str = "canonical_session_id+turn_number+content_hash"

    # Extra tags returned by the default build_extra_tags().
    _default_extra_tags: List[str] = []

    # If True, default build_extra_tags() returns source_fidelity=experimental.
    experimental: bool = False

    def completeness_capabilities(self) -> Dict[str, Any]:
        """Default capability dict derived from class attributes."""
        return {
            "visible_text": self._cap_visible_text,
            "tool_calls": self._cap_tool_calls,
            "tool_results": self._cap_tool_results,
            "reasoning": self._cap_reasoning,
            "attachments": self._cap_attachments,
            "raw_files": self._cap_raw_files,
            "source_fidelity": self._cap_source_fidelity,
            "memory_scope": self._cap_memory_scope,
            "host_memory_default": self._cap_host_memory_default,
            "host_memory_effect": self._cap_host_memory_effect,
            "transcript_kind": self._cap_transcript_kind,
            "compression": self._cap_compression,
            "dedupe_strategy": self._cap_dedupe_strategy,
        }

    def build_extra_tags(self, turn: Turn) -> List[str]:  # noqa: ARG002
        """Default extra tags from class attributes.

        Subclasses that need turn-dependent tags can still override this.
        """
        tags = list(self._default_extra_tags)
        if self.experimental and "source_fidelity=experimental" not in tags:
            tags.append("source_fidelity=experimental")
        return tags

    # ------------------------------------------------------------------
    # Directory resolution helpers
    # ------------------------------------------------------------------

    def _resolve_data_dir(
        self,
        candidates: List[Path],
        *,
        fallback: Optional[Path] = None,
    ) -> Optional[Path]:
        """Return the first existing directory from ``candidates``.

        If ``_override_data_dir`` is set (used by tests), it always wins.
        """
        if hasattr(self, "_override_data_dir"):
            return self._override_data_dir  # type: ignore[no-any-return]
        for p in candidates:
            if native_path_kind(p) != "missing":
                return p
        if fallback is not None and native_path_kind(fallback) != "missing":
            return fallback
        return None

    def _resolve_data_dir_with_env(
        self,
        env_name: Optional[str],
        default_paths: List[Path],
        *,
        subdir: Optional[str] = None,
        fallback: Optional[Path] = None,
    ) -> Optional[Path]:
        """Resolve data_dir from env var + default path candidates.

        If ``subdir`` is provided and the resolved directory contains that
        sub-directory, the sub-directory is returned.
        """
        candidates: List[Path] = []
        if env_name:
            env_val = os.getenv(env_name)
            if env_val:
                candidates.append(Path(env_val).expanduser())
        candidates.extend(default_paths)

        resolved = self._resolve_data_dir(candidates, fallback=fallback)
        if resolved is None or subdir is None:
            return resolved
        child = resolved / subdir
        return child if native_path_kind(child) != "missing" else resolved

    @staticmethod
    def _sessions_dir(base: Path, subdir: str = "sessions") -> Path:
        """Normalize ``base`` to its ``sessions`` sub-directory if present."""
        return base if base.name == subdir else base / subdir

    # ------------------------------------------------------------------
    # Session discovery helpers
    # ------------------------------------------------------------------

    def _read_jsonl(self, path: Path) -> List[Dict[str, Any]]:
        """Read every nonblank JSONL value or fail with a typed source error."""
        messages: List[Dict[str, Any]] = []
        try:
            with open_native_text(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        decoded = json.loads(line)
                        messages.append(
                            decoded
                            if isinstance(decoded, dict)
                            else {"_mnemos_raw_native_record": decoded}
                        )
                    except json.JSONDecodeError:
                        raise NativeSourceContractError(
                            "native_jsonl_decode_failed"
                        ) from None
        except (OSError, IOError):
            raise NativeSourceContractError(
                "native_artifact_read_failed"
            ) from None
        return messages

    def _compute_session_state(
        self,
        files: List[Path],
        sort_key: Optional[Callable[[Path], Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Compute aggregate state (mtime, size, file_count, fingerprint).

        Args:
            files: List of file paths to aggregate.
            sort_key: Optional key function for deterministic ordering.

        Returns:
            A content-bound state, or None when no files were declared.
        """
        return native_artifact_content_state(files, sort_key=sort_key)

    def _discover_by_glob(
        self,
        root: Optional[Path],
        pattern: str,
        *,
        recursive: bool = True,
        max_depth: Optional[int] = None,
        session_id_from: str = "stem",
        working_dir_from: str = "parent",
        session_id_func: Optional[Callable[[Path], str]] = None,
        skip_names: Optional[Set[str]] = None,
    ) -> List[SessionInfo]:
        """Discover SessionInfo objects by globbing under ``root``.

        Args:
            root: Base directory to scan. If None or missing, returns [].
            pattern: Glob pattern relative to ``root``.
            recursive: Use rglob when True, glob when False.
            max_depth: If set, skip files whose relative path depth exceeds it.
            session_id_from: How to derive session_id from the path.
                ``"stem"`` uses path.stem; ``"name"`` uses path.name.
            working_dir_from: How to derive working_dir.
                ``"parent"`` uses path.parent; ``"root"`` uses root;
                ``"parent.parent"`` uses path.parent.parent.
            session_id_func: Optional callable to compute session_id from path.
                Takes precedence over ``session_id_from``.
            skip_names: Set of file names to ignore.

        Returns:
            List of SessionInfo sorted by mtime descending.
        """
        if root is None:
            return []
        try:
            if native_path_kind(root) == "missing":
                return []
            iterator = root.rglob(pattern) if recursive else root.glob(pattern)

            sessions: List[SessionInfo] = []
            for path in iterator:
                if skip_names and path.name in skip_names:
                    continue
                if max_depth is not None:
                    try:
                        rel_parts = path.relative_to(root).parts
                        if len(rel_parts) > max_depth:
                            continue
                    except ValueError:
                        continue

                session_id = _derive_session_id(path, session_id_func, session_id_from)
                working_dir = _derive_working_dir(path, root, working_dir_from)
                mtime = path.stat().st_mtime
                sessions.append(
                    SessionInfo(
                        session_id=session_id,
                        source_path=path,
                        working_dir=working_dir,
                        mtime=mtime,
                    )
                )
            sessions.sort(key=lambda s: s.mtime or 0.0, reverse=True)
            return sessions
        except OSError:
            raise NativeSourceContractError(
                "native_session_discovery_failed"
            ) from None

    def _discover_vscode_workspace_sessions(
        self,
        base: Optional[Path],
        workspace_candidates: List[str],
        global_candidates: List[str],
        *,
        prefix: str,
    ) -> List[SessionInfo]:
        """Discover every manifest-declared JSON basename below one IDE root."""
        if base is None:
            return []
        try:
            if native_path_kind(base) == "missing":
                return []

            sessions: List[SessionInfo] = []
            workspace_dir = base / "workspaceStorage"
            global_dir = base / "User" / "globalStorage"
            candidates = sorted(set(workspace_candidates) | set(global_candidates))
            paths = sorted(
                {
                    path
                    for candidate in candidates
                    for path in base.rglob(candidate)
                    if native_path_kind(path) == "file"
                },
                key=str,
            )
            for path in paths:
                try:
                    workspace_relative = path.relative_to(workspace_dir)
                except ValueError:
                    workspace_relative = None
                try:
                    global_relative = path.relative_to(global_dir)
                except ValueError:
                    global_relative = None

                if workspace_relative and len(workspace_relative.parts) >= 2:
                    workspace_name = workspace_relative.parts[0]
                    session_id = workspace_name
                    working_dir = workspace_dir / workspace_name
                    source_kind = "workspace_json"
                    aliases = [workspace_name, path.name]
                    native_id = f"{workspace_name}:{workspace_relative.as_posix()}"
                elif global_relative is not None:
                    relative_label = global_relative.as_posix()
                    session_id = (
                        f"{prefix}-global-"
                        + relative_label.replace("/", "::")
                    )
                    working_dir = path.parent
                    source_kind = "global_json"
                    aliases = [session_id, path.name]
                    native_id = session_id
                else:
                    relative_label = path.relative_to(base).as_posix()
                    session_id = (
                        f"{prefix}-json-"
                        + relative_label.replace("/", "::")
                    )
                    working_dir = path.parent
                    source_kind = "generic_json"
                    aliases = [path.name]
                    native_id = session_id
                canonical_id = stable_path_session_id(
                    prefix,
                    base,
                    path,
                    native_id=native_id,
                )
                sessions.append(
                    SessionInfo(
                        session_id=session_id,
                        source_path=path,
                        working_dir=str(working_dir),
                        mtime=path.stat().st_mtime,
                        canonical_session_id=canonical_id,
                        session_aliases=aliases,
                        source_kind=source_kind,
                    )
                )
            sessions.sort(
                key=lambda item: (-(item.mtime or 0.0), item.session_id)
            )
            return sessions
        except OSError:
            raise NativeSourceContractError(
                "native_vscode_session_discovery_failed"
            ) from None
