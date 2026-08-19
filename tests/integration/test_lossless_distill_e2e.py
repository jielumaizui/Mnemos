"""Lossless distillation input contracts for complete conversation paths."""

from core.hephaestus.distillation_engine import DistillationEngine
from core.hephaestus.distillation_text import build_session_text, clean_message_content
from core.hephaestus.wiki_builder import _extract_messages_from_record


def _record_meta() -> dict[str, object]:
    return {
        "source": "",
        "model": "",
        "cwd": "",
        "session_id": "",
        "has_skip_distill": False,
    }


def test_complete_distillation_cleaner_preserves_code_and_shell_commands():
    code = "```python\n" + "\n".join(
        f"root005_code_line_{index} = {index}" for index in range(1, 21)
    ) + "\n```"
    commands = "\n".join(
        f"git status --short root005_command_{index}" for index in range(1, 7)
    )

    cleaned = clean_message_content(f"{code}\n{commands}")

    for sentinel in (
        "root005_code_line_1 = 1",
        "root005_code_line_11 = 11",
        "root005_code_line_20 = 20",
        "root005_command_1",
        "root005_command_4",
        "root005_command_6",
    ):
        assert sentinel in cleaned
    assert "omitted" not in cleaned


def test_complete_distillation_cleaner_preserves_visible_formatting_bytes():
    content = "  ROOT005_LEADING\n1.\n\n\nROOT005_TRAILING  "

    assert clean_message_content(content) == content


def test_canonical_builder_preserves_attachment_placeholder_and_reports_exclusion():
    content = (
        "ROOT005_VISIBLE_FIRST\n"
        "[attachment:image/png sha256=ROOT005_ATTACHMENT_PLACEHOLDER]\n"
        "[thinking]ROOT005_PRIVATE_NOT_FOR_EXTRACTION[/thinking]\n"
        "ROOT005_VISIBLE_TAIL"
    )
    meta = {}

    text = build_session_text(
        [{"role": "user", "content": content}],
        max_tokens=1,
        per_message_token_limit=1,
        out_meta=meta,
        lossless=True,
    )

    assert "ROOT005_VISIBLE_FIRST" in text
    assert "ROOT005_ATTACHMENT_PLACEHOLDER" in text
    assert "ROOT005_VISIBLE_TAIL" in text
    assert "ROOT005_PRIVATE_NOT_FOR_EXTRACTION" not in text
    assert meta["explicit_exclusion_count"] == 1
    assert meta["explicit_excluded_chars"] > 0
    assert meta["message_truncations"][0]["explicit_exclusions"][0]["kind"] == (
        "private_thinking"
    )
    assert meta["budget_overflow_tokens"] > 0
    assert meta["silent_omission_count"] == 0
    assert meta["truncated"] is False


def test_chunked_extractor_input_preserves_first_middle_and_tail_sentinels():
    content = (
        "ROOT005_FIRST_SENTINEL\n"
        + "\n".join(f"payload_{index} " * 20 for index in range(240))
        + "\nROOT005_MIDDLE_SENTINEL\n"
        + "\n".join(f"tail_payload_{index} " * 20 for index in range(240))
        + "\nROOT005_TAIL_SENTINEL"
    )
    chunks = DistillationEngine._chunk_messages(
        [{"role": "user", "content": content}],
        max_tokens_per_chunk=400,
    )
    extractor_inputs = [
        build_session_text(chunk, max_tokens=400, lossless=True) for chunk in chunks
    ]
    combined = "\n".join(extractor_inputs)
    expanded_content = "".join(msg["content"] for chunk in chunks for msg in chunk)

    assert len(chunks) > 1
    assert expanded_content == content
    assert "omitted" not in combined
    assert "ROOT005_FIRST_SENTINEL" in combined
    assert "ROOT005_MIDDLE_SENTINEL" in combined
    assert "ROOT005_TAIL_SENTINEL" in combined


def test_wiki_builder_plaintext_fallback_preserves_full_content():
    content = "ROOT005_FALLBACK_FIRST\n" + ("完整原文" * 200) + "\nROOT005_FALLBACK_TAIL"

    messages = _extract_messages_from_record(
        {"content": content, "createTime": "2026-07-10T00:00:00+08:00"},
        _record_meta(),
    )

    assert len(messages) == 1
    assert messages[0]["content"] == content
