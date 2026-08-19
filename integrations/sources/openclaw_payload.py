"""Lossless decoding helpers for OpenClaw native payloads."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

from core.sync_framework.agent_source import NativeSourceContractError
from core.ops.durable_io import read_native_bytes


def read_native_jsonl(path: Path) -> List[Dict[str, Any]]:
    """Read every native line, preserving malformed text and bytes as typed refs."""
    try:
        raw_lines = read_native_bytes(path).splitlines()
    except OSError:
        raise NativeSourceContractError(
            "native_openclaw_jsonl_read_failed"
        ) from None
    events: List[Dict[str, Any]] = []
    for line_number, raw_line in enumerate(raw_lines, start=1):
        if not raw_line.strip():
            continue
        try:
            text = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            events.append(
                {
                    "_mnemos_raw_event_ref": {
                        "line_number": line_number,
                        "raw_base64": base64.b64encode(raw_line).decode("ascii"),
                        "raw_encoding": "base64",
                        "decode_error": "invalid_utf8",
                    }
                }
            )
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            events.append(
                {
                    "_mnemos_raw_event_ref": {
                        "line_number": line_number,
                        "raw": text,
                        "decode_error": "invalid_json",
                    }
                }
            )
            continue
        if not isinstance(event, dict):
            events.append(
                {
                    "_mnemos_raw_event_ref": {
                        "line_number": line_number,
                        "raw": text,
                        "decode_error": "non_object_json",
                    }
                }
            )
            continue
        events.append(event)
    return events


def dict_items(value: Any) -> List[Dict[str, Any]]:
    """Normalize a mapping or list of mappings without string iteration."""
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def malformed_collection_refs(
    value: Any,
    event_type: str,
) -> List[Dict[str, Any]]:
    """Preserve every non-mapping item rejected by a structured collection."""
    if value is None or isinstance(value, dict):
        return []
    if isinstance(value, list):
        return [
            {"event_type": event_type, "raw": item}
            for item in value
            if not isinstance(item, dict)
        ]
    return [{"event_type": event_type, "raw": value}]


def invalid_present_ref(
    payload: Mapping[str, Any],
    key: str,
    validator: Callable[[Any], bool],
    event_type: str,
) -> List[Dict[str, Any]]:
    """Preserve an explicitly present known field when its shape is invalid."""
    if key not in payload or validator(payload[key]):
        return []
    return [
        {
            "event_type": event_type,
            "field": key,
            "raw": payload[key],
        }
    ]


def conflicting_alias_ref(
    payload: Mapping[str, Any],
    primary: str,
    alternate: str,
    event_type: str,
) -> List[Dict[str, Any]]:
    """Preserve both declared aliases when callers supplied different payloads."""
    if (
        primary not in payload
        or alternate not in payload
        or payload[primary] == payload[alternate]
    ):
        return []
    return [
        {
            "event_type": event_type,
            "raw": {
                primary: payload[primary],
                alternate: payload[alternate],
            },
        }
    ]


def session_identity_refs(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Preserve malformed or conflicting native session identity aliases."""
    refs = conflicting_alias_ref(
        payload,
        "sessionId",
        "session_id",
        "conflicting_session_identity_fields",
    )
    for key in ("sessionId", "session_id"):
        refs.extend(
            invalid_present_ref(
                payload,
                key,
                lambda value: isinstance(value, str) and bool(value),
                "malformed_session_identity",
            )
        )
    return refs


def tool_alias_conflict_refs(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Preserve conflicting snake/camel tool payload aliases."""
    refs: List[Dict[str, Any]] = []
    for primary, alternate, event_type in (
        ("tool_calls", "toolCalls", "conflicting_tool_call_fields"),
        ("tool_results", "toolResults", "conflicting_tool_result_fields"),
    ):
        refs.extend(
            conflicting_alias_ref(
                payload,
                primary,
                alternate,
                event_type,
            )
        )
    return refs


def dedupe_raw_refs(refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate full-fidelity refs without dropping artifact provenance."""
    deduped: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = json.dumps(
            ref,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if key not in seen:
            seen.add(key)
            deduped.append(ref)
    return deduped


def content_to_text_attachments_and_refs(
    content: Any,
) -> tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Decode reversible text/attachment shapes and retain every other shape."""
    if isinstance(content, str):
        return content, [], []
    if isinstance(content, dict):
        known = {"text", "content"}
        keys = set(content)
        text_value = content.get("text")
        content_value = content.get("content")
        has_conflict = (
            "text" in content
            and "content" in content
            and text_value != content_value
        )
        selected = text_value if "text" in content else content_value
        canonical = (
            bool(keys)
            and keys <= known
            and not has_conflict
            and isinstance(selected, str)
        )
        refs: List[Dict[str, Any]] = []
        if not canonical:
            refs.append(
                {
                    "event_type": "unparsed_content_block",
                    "raw": content,
                }
            )
        return selected if isinstance(selected, str) else "", [], refs
    if not isinstance(content, list):
        refs = (
            []
            if content == ""
            else [{"event_type": "unparsed_content_block", "raw": content}]
        )
        return "", [], refs
    texts: List[str] = []
    attachments: List[Dict[str, Any]] = []
    raw_event_refs: List[Dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
            continue
        if not isinstance(block, dict):
            raw_event_refs.append(
                {
                    "event_type": "unparsed_content_block",
                    "raw": block,
                }
            )
            continue
        btype = str(block.get("type") or "")
        if btype in ("text", "input_text", "output_text"):
            text_value = block.get("text")
            if isinstance(text_value, str):
                texts.append(text_value)
            if not isinstance(text_value, str) or set(block) - {"type", "text"}:
                raw_event_refs.append(
                    {
                        "event_type": "unparsed_content_block",
                        "raw": block,
                    }
                )
        elif any(
            token in btype.lower()
            for token in ("file", "image", "media", "attach")
        ):
            attachments.append(
                {
                    "type": btype or "attachment",
                    "name": block.get("name") or block.get("filename") or "",
                    "path": block.get("path") or "",
                    "url": block.get("url") or "",
                    "mime_type": (
                        block.get("mime_type") or block.get("media_type") or ""
                    ),
                    "raw": block,
                }
            )
        elif "text" in block:
            if isinstance(block.get("text"), str):
                texts.append(block["text"])
            raw_event_refs.append(
                {
                    "event_type": "unparsed_content_block",
                    "raw": block,
                }
            )
        else:
            raw_event_refs.append(
                {
                    "event_type": "unparsed_content_block",
                    "raw": block,
                }
            )
    return (
        "\n".join(text for text in texts if text),
        attachments,
        raw_event_refs,
    )


def messages_snapshot_to_text(
    messages: Any,
) -> tuple[str, str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Decode one completed-event message snapshot without dropping residuals."""
    if not isinstance(messages, list):
        return "", "", [], []
    user_parts: List[str] = []
    assistant_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []
    raw_event_refs: List[Dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raw_event_refs.append(
                {
                    "event_type": "malformed_snapshot_message",
                    "raw": message,
                }
            )
            continue
        role = str(message.get("role") or "")
        text, message_attachments, message_refs = (
            content_to_text_attachments_and_refs(
                message.get("content", "")
            )
        )
        if role == "user" and text:
            user_parts.append(text)
        elif role == "assistant" and text:
            assistant_parts.append(text)
        elif role not in {"user", "assistant"}:
            raw_event_refs.append(
                {
                    "event_type": "unparsed_snapshot_role",
                    "role": role,
                    "raw": message,
                }
            )
        attachments.extend(message_attachments)
        raw_event_refs.extend(message_refs)
        residual = {
            key: value
            for key, value in message.items()
            if key not in {"role", "content"}
        }
        if residual:
            raw_event_refs.append(
                {
                    "event_type": "snapshot_message_residual",
                    "raw": residual,
                }
            )
    return (
        "\n\n".join(user_parts),
        "\n\n".join(assistant_parts),
        attachments,
        raw_event_refs,
    )
