# -*- coding: utf-8 -*-
"""Replayable table artifacts for document distillation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

from core.config import get_config


TABLE_ARTIFACT_SCHEMA_VERSION = "mnemos.document_table_artifact.v1"


class DocumentTableArtifactStore:
    """Persist full Markdown tables and return compact metadata for LLM prompts."""

    def __init__(self, wiki_base: Path | None = None):
        self.wiki_base = wiki_base

    def record(
        self,
        table_lines: List[str],
        *,
        session_id: str,
        table_index: int,
        max_rows: int,
    ) -> Dict[str, Any]:
        raw_markdown = "\n".join(table_lines)
        sha256 = hashlib.sha256(raw_markdown.encode("utf-8")).hexdigest()
        safe_session = safe_table_session_id(session_id)
        uri = f"mnemos-table://document/{safe_session}/table/{table_index}"
        headers = parse_markdown_table_row(table_lines[0]) if table_lines else []
        data_lines = [
            line for line in table_lines[1:] if not is_markdown_table_separator(line)
        ]
        parsed_rows = [
            {
                "row_number": idx,
                "values": parse_markdown_table_row(line),
                "cells": cells_by_header(headers, parse_markdown_table_row(line)),
            }
            for idx, line in enumerate(data_lines, start=1)
        ]
        row_chunks = table_row_chunks(uri, len(parsed_rows), max(1, max_rows - 2))
        evidence_refs = table_evidence_refs(uri, headers, len(parsed_rows))
        artifact_path = self.artifact_path(safe_session)
        payload = {
            "schema_version": TABLE_ARTIFACT_SCHEMA_VERSION,
            "uri": uri,
            "session_id": session_id,
            "table_index": table_index,
            "row_count": len(table_lines),
            "data_row_count": len(parsed_rows),
            "col_count": len(headers),
            "sha256": sha256,
            "headers": headers,
            "rows": parsed_rows,
            "row_chunks": row_chunks,
            "chunk_uris": [chunk["uri"] for chunk in row_chunks],
            "sample_policy": {
                "prompt_mode": "sample_with_full_artifact",
                "sample_rows": min(3, len(parsed_rows)),
            },
            "evidence_refs": evidence_refs,
            "markdown": raw_markdown,
        }
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        with artifact_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        metadata = dict(payload)
        metadata.pop("rows", None)
        metadata.pop("markdown", None)
        metadata["artifact_path"] = str(artifact_path)
        return metadata

    def artifact_path(self, safe_session: str) -> Path:
        if self.wiki_base is not None:
            root = self.wiki_base / "99-Artifacts" / "document_tables"
        else:
            root = get_config().database_dir / "document_table_artifacts"
        return root / f"{safe_session}.jsonl"


def preprocess_large_tables(
    content: str,
    *,
    artifact_store: DocumentTableArtifactStore,
    session_id: str = "",
    max_rows: int = 12,
    max_cols: int = 8,
) -> tuple[str, List[Dict[str, Any]]]:
    """Sample large Markdown tables for prompts while preserving full replay artifacts."""
    artifacts: List[Dict[str, Any]] = []
    lines = content.split("\n")
    result: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and line.strip().endswith("|"):
            table_lines: List[str] = []
            while (
                i < len(lines)
                and lines[i].strip().startswith("|")
                and lines[i].strip().endswith("|")
            ):
                table_lines.append(lines[i])
                i += 1
            if len(table_lines) < 2:
                result.extend(table_lines)
                continue
            row_count = len(table_lines)
            col_count = len(parse_markdown_table_row(table_lines[0]))
            if row_count > max_rows or col_count > max_cols:
                artifact = artifact_store.record(
                    table_lines,
                    session_id=session_id,
                    table_index=len(artifacts),
                    max_rows=max_rows,
                )
                artifacts.append(artifact)
                result.extend(format_table_sample(table_lines, artifact))
            else:
                result.extend(table_lines)
        else:
            result.append(line)
            i += 1
    return "\n".join(result), artifacts


def format_table_sample(table_lines: List[str], artifact: Dict[str, Any]) -> List[str]:
    header = table_lines[0]
    separator = (
        table_lines[1]
        if len(table_lines) > 1 and is_markdown_table_separator(table_lines[1])
        else None
    )
    data_rows = [
        line for line in table_lines[1:] if not is_markdown_table_separator(line)
    ][:3]
    first_header = (artifact.get("headers") or ["column"])[0]
    return [
        (
            f"> 📊 **大表格**：{artifact['row_count']} 行 × {artifact['col_count']} 列；"
            f"完整结构化表格已保存为 {artifact['uri']}"
        ),
        f"> 回放 artifact: {artifact['artifact_path']}；sha256={artifact['sha256']}",
        (
            "> 表格结论必须引用 row/cell evidence refs，例如 "
            f"{artifact['uri']}#row=1 或 {artifact['uri']}#row=1&cell={first_header}"
        ),
        f"> 数据行分块: {', '.join(artifact['chunk_uris'])}",
        f"> 以下仅展示前 {len(data_rows)} 行样例；完整行列以 artifact 为准。",
        header,
        *([separator] if separator else []),
        *data_rows,
    ]


def attach_table_artifacts(
    fragments: List[Any],
    data: Dict[str, Any],
    artifacts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not artifacts:
        return data
    data = dict(data or {})
    existing_value = data.get("table_artifacts")
    existing = existing_value if isinstance(existing_value, list) else []
    data["table_artifacts"] = [*existing, *artifacts]
    coverage = dict(data.get("source_coverage") or {})
    coverage["full_table_replay"] = True
    coverage["table_artifacts"] = [
        {
            "uri": artifact["uri"],
            "row_count": artifact["row_count"],
            "col_count": artifact["col_count"],
            "sha256": artifact["sha256"],
        }
        for artifact in artifacts
    ]
    data["source_coverage"] = coverage
    for frag in fragments:
        frag.frontmatter = dict(frag.frontmatter or {})
        frag.frontmatter.setdefault("table_artifacts", artifacts)
        evidence_refs = list(frag.frontmatter.get("evidence_refs") or [])
        for artifact in artifacts:
            evidence_refs.extend(artifact.get("evidence_refs") or [])
        frag.frontmatter["evidence_refs"] = dedupe_list(evidence_refs)
    return data


def parse_markdown_table_row(row: str) -> List[str]:
    stripped = row.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def is_markdown_table_separator(row: str) -> bool:
    cells = parse_markdown_table_row(row)
    return bool(cells) and all(
        "-" in cell and set(cell.replace(" ", "")) <= {"-", ":"}
        for cell in cells
    )


def safe_table_session_id(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id or "unknown").strip("._-")
    return safe[:80] or "unknown"


def cells_by_header(headers: List[str], values: List[str]) -> Dict[str, str]:
    if not headers:
        return {}
    return {
        header: values[idx] if idx < len(values) else ""
        for idx, header in enumerate(headers)
    }


def table_row_chunks(uri: str, row_count: int, chunk_size: int) -> List[Dict[str, Any]]:
    chunks = []
    for chunk_index, start in enumerate(range(1, row_count + 1, chunk_size)):
        end = min(row_count, start + chunk_size - 1)
        chunks.append(
            {
                "uri": f"{uri}/chunk/{chunk_index}",
                "row_start": start,
                "row_end": end,
            }
        )
    return chunks


def table_evidence_refs(uri: str, headers: List[str], row_count: int) -> List[str]:
    refs = [uri]
    if row_count:
        refs.append(f"{uri}#row=1")
        refs.append(f"{uri}#row={row_count}")
    if headers and row_count:
        refs.append(f"{uri}#row=1&cell={headers[0]}")
    return dedupe_list(refs)


def dedupe_list(values: List[Any]) -> List[Any]:
    seen = set()
    result = []
    for value in values:
        key = (
            json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, dict)
            else str(value)
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
