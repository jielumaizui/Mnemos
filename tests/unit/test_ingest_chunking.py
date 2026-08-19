# -*- coding: utf-8 -*-
"""Tests for core.ingest_chunking.SemanticChunker."""

from __future__ import annotations

import pytest

from core.ingest_chunking import SemanticChunker


class TestSemanticChunker:
    @pytest.fixture
    def chunker(self):
        return SemanticChunker(max_chunk_chars=200, min_chunk_chars=50)

    def test_short_text_returns_single_chunk(self, chunker):
        text = "Short text."
        chunks = chunker.chunk_text(text)
        assert chunks == [text]

    def test_splits_by_headings(self, chunker):
        text = (
            "# Chapter 1\n\n" + "Body one. " * 30 + "\n\n"
            "## Section 1.1\n\n" + "More body. " * 30 + "\n\n"
            "# Chapter 2\n\n" + "Body two. " * 30
        )
        chunks = chunker.chunk_text(text)
        assert len(chunks) >= 2
        assert any("# Chapter 1" in c for c in chunks)
        assert any("# Chapter 2" in c for c in chunks)

    def test_splits_by_paragraphs_when_no_headings(self, chunker):
        paragraphs = [f"Paragraph {i} with enough characters to be meaningful." for i in range(10)]
        text = "\n\n".join(paragraphs)
        chunks = chunker.chunk_text(text)
        assert len(chunks) >= 2

    def test_large_body_under_heading_gets_split(self, chunker):
        body = "\n\n".join([f"Paragraph {i} has some text in it for sizing." for i in range(20)])
        text = f"# Title\n\n{body}"
        chunks = chunker.chunk_text(text)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert "# Title" in chunk

    def test_empty_text_returns_empty(self, chunker):
        assert chunker.chunk_text("") == []

    def test_merge_small_chunks(self, chunker):
        # Two tiny heading blocks should be merged if total under limit
        text = "# A\n\nSmall.\n\n# B\n\nAlso small."
        chunker.min_chunk_chars = 500
        chunks = chunker.chunk_text(text)
        assert len(chunks) == 1
        assert "# A" in chunks[0]
        assert "# B" in chunks[0]
