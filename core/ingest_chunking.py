# -*- coding: utf-8 -*-
"""
SemanticChunker —  ingestion 侧语义分片

把外部文件（PDF/Word/txt/epub...）提取出的长文本按语义边界切分为多个 chunk，
每个 chunk 独立入队 amphora 触发蒸馏，避免单条消息被 DistillationEngine 的 token
限制强制截断而丢失内容。

分片策略：
1. 优先按 Markdown 标题（# ~ ######）切分，保留标题层级作为上下文。
2. 无标题时按空行（段落）切分。
3. 单一大段超过 max_chunk_chars 时，继续按段落切分。
4. 相邻小 chunk（< min_chunk_chars）在满足上限的前提下合并，避免碎片。
"""

from __future__ import annotations

import re
from typing import List, Optional

# Constants extracted from magic numbers
SEMANTIC_CHUNKER_DEFAULT_MAX_CHUNK_CHARS = 12000


class SemanticChunker:
    """基于标题/段落的轻量级语义分片器。"""

    # 默认单 chunk 字符上限：约对应 4k~6k tokens（中文更密，英文更疏）
    DEFAULT_MAX_CHUNK_CHARS = SEMANTIC_CHUNKER_DEFAULT_MAX_CHUNK_CHARS
    DEFAULT_MIN_CHUNK_CHARS = 1000

    def __init__(
        self,
        max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
        min_chunk_chars: int = DEFAULT_MIN_CHUNK_CHARS,
    ):
        self.max_chunk_chars = max(max_chunk_chars, min_chunk_chars)
        self.min_chunk_chars = max(1, min_chunk_chars)

    def chunk_text(self, text: str, source_name: str = "") -> List[str]:
        """对文本做语义分片，返回 chunk 列表。"""
        if not text:
            return []

        text = text.strip()
        if len(text) <= self.max_chunk_chars:
            return [text]

        # 优先按 Markdown 标题切分
        heading_blocks = self._split_by_headings(text)
        if heading_blocks:
            chunks = self._chunk_by_headings(heading_blocks)
        else:
            chunks = self._chunk_by_paragraphs(text)

        return self._merge_small_chunks(chunks)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    @staticmethod
    def _split_by_headings(text: str) -> Optional[List[dict]]:
        """按 Markdown 标题把文本分成若干块；无标题时返回 None。"""
        pattern = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        matches = list(pattern.finditer(text))
        if not matches:
            return None

        blocks = []
        for i, m in enumerate(matches):
            level = len(m.group(1))
            title = m.group(2).strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[start:end].strip()
            blocks.append({"level": level, "title": title, "body": body})
        return blocks

    def _chunk_by_headings(self, blocks: List[dict]) -> List[str]:
        """按标题块继续切分。"""
        chunks: List[str] = []
        for block in blocks:
            heading_line = f"{'#' * block['level']} {block['title']}"
            body = block["body"]
            if not body:
                chunks.append(heading_line)
                continue

            if len(body) <= self.max_chunk_chars:
                chunks.append(f"{heading_line}\n\n{body}")
            else:
                # 大段继续按段落切分，保留标题作为前缀
                chunks.extend(self._chunk_by_paragraphs(body, prefix=heading_line))
        return chunks

    def _chunk_by_paragraphs(self, text: str, prefix: str = "") -> List[str]:
        """按段落切分，可选在每个 chunk 前加前缀（如标题）。"""
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            return [prefix] if prefix else []

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        prefix_len = len(prefix) + 2 if prefix else 0  # +2 for "\n\n"

        def _flush():
            if not current:
                return
            body = "\n\n".join(current)
            chunks.append(f"{prefix}\n\n{body}" if prefix else body)

        for p in paragraphs:
            p_len = len(p)
            # 单段就超限：强行保留（DistillationEngine 内部还会再切）
            if current and current_len + p_len + prefix_len > self.max_chunk_chars:
                _flush()
                current = [p]
                current_len = p_len
            else:
                current.append(p)
                current_len += p_len

        _flush()
        return chunks

    def _merge_small_chunks(self, chunks: List[str]) -> List[str]:
        """合并相邻小 chunk，减少碎片。"""
        if not chunks:
            return []

        merged: List[str] = [chunks[0]]
        for chunk in chunks[1:]:
            last = merged[-1]
            if (
                len(last) < self.min_chunk_chars
                and len(last) + len(chunk) + 8 <= self.max_chunk_chars
            ):
                merged[-1] = f"{last}\n\n---\n\n{chunk}"
            else:
                merged.append(chunk)
        return merged
