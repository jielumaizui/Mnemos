"""
wiki_builder.py 单元测试

覆盖项：
- fetch_l1_sessions() — StorageBackend 查询与会话分组
- reconstruct_session() — JSON/Markdown/纯文本三种解析路径
- _is_session_completed() — 基于 5 分钟超时的完成检测
- _is_processed() / _mark_processed() — SQLite 状态追踪
- _mask_wiki_generated_blocks() — 回流防护内容屏蔽
- _try_parse_json() — 截断 JSON 兼容解析
- _parse_markdown_turns() — Markdown 多格式提取
"""

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

# 先 mock config，避免 import 时读取文件系统
_FAKE_CONFIG = MagicMock()
_FAKE_CONFIG.wiki_dir = Path(tempfile.gettempdir()) / "mnemos_test_wiki"
_FAKE_CONFIG.data_dir = Path(tempfile.gettempdir()) / "mnemos_test_data"
_FAKE_CONFIG.database_dir = _FAKE_CONFIG.data_dir

with patch("core.config.get_config", return_value=_FAKE_CONFIG):
    from core.hephaestus import wiki_builder as wb


@pytest.fixture
def tmp_db_path(tmp_path):
    """提供隔离的 SQLite 数据库路径，并通过 monkeypatch 注入 wiki_builder"""
    db_path = tmp_path / "wiki_state.db"
    with patch.object(wb, "_get_wiki_db", return_value=db_path):
        yield db_path


@pytest.fixture
def mock_backend():
    """构造返回可控数据的 StorageBackend mock"""
    client = Mock()
    return client


# ---------------------------------------------------------------------------
# 1. fetch_l1_sessions
# ---------------------------------------------------------------------------

from core.sync_framework.storage_backend import StorageResult  # noqa: E402


def test_fetch_l1_sessions_groups_by_session_tag(mock_backend):
    """fetch_l1_sessions 应按 session= 标签正确分组，仅保留 L1 记录"""
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1",
            content="hello",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        ),
        StorageResult(
            uid="m2",
            content="world",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        ),
        StorageResult(
            uid="m3",
            content="foo",
            tags=["layer=L1", "session=s2"],
            metadata={},
            created_at="",
            updated_at="",
        ),
    ]

    result = wb.fetch_l1_sessions(mock_backend)

    assert "s1" in result
    assert "s2" in result
    assert len(result["s1"]) == 2
    assert len(result["s2"]) == 1
    assert result["s1"][0]["uid"] == "m1"
    mock_backend.list_by_tags.assert_called_once()


def test_fetch_l1_sessions_returns_empty_on_exception(mock_backend):
    """fetch_l1_sessions 在查询异常时应返回空字典，不抛异常"""
    mock_backend.list_by_tags.side_effect = RuntimeError("network down")

    result = wb.fetch_l1_sessions(mock_backend)

    assert result == {}


def test_fetch_l1_sessions_skips_records_without_session_tag(mock_backend):
    """没有 session= 标签的 L1 记录应被跳过"""
    mock_backend.list_by_tags.return_value = [
        StorageResult(
            uid="m1", content="hello", tags=["layer=L1"], metadata={}, created_at="", updated_at=""
        ),
        StorageResult(
            uid="m2",
            content="world",
            tags=["layer=L1", "session=s1"],
            metadata={},
            created_at="",
            updated_at="",
        ),
    ]

    result = wb.fetch_l1_sessions(mock_backend)

    assert list(result.keys()) == ["s1"]
    assert len(result["s1"]) == 1


# ---------------------------------------------------------------------------
# 2. reconstruct_session — JSON 路径
# ---------------------------------------------------------------------------


def test_reconstruct_session_json_path():
    """reconstruct_session 应正确解析 save_session_full 格式的 JSON 内容"""
    records = [
        {
            "content": json.dumps(
                {
                    "_meta": {"segment": "1/1"},
                    "messages": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "world"},
                    ],
                }
            ),
            "tags": ["session=abc123", "source=claude", "model=gpt-4"],
            "createTime": "2024-01-01T00:00:00Z",
        }
    ]

    messages, meta = wb.reconstruct_session(records)

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "hello"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "world"
    assert meta["source"] == "claude"
    assert meta["model"] == "gpt-4"
    assert meta["session_id"] == "abc123"
    assert meta["total_chunks"] == 1


def test_reconstruct_session_detects_skip_distill():
    """reconstruct_session 应识别 skip-distill=true 标签并写入 meta"""
    records = [
        {
            "content": json.dumps(
                {
                    "_meta": {"segment": "1/1"},
                    "messages": [{"role": "user", "content": "hi"}],
                }
            ),
            "tags": ["session=x", "skip-distill=true"],
            "createTime": "2024-01-01T00:00:00Z",
        }
    ]

    _, meta = wb.reconstruct_session(records)

    assert meta["has_skip_distill"] is True


# ---------------------------------------------------------------------------
# 3. reconstruct_session — Markdown 路径
# ---------------------------------------------------------------------------


def test_reconstruct_session_markdown_path():
    """reconstruct_session 应正确解析 sync_engine 生成的 Markdown 格式"""
    records = [
        {
            "content": "## Turn 1\n\n**User** (gpt-4):\n\nHello world\n\n**Assistant**:\n\nHi there\n\n---\n",  # noqa: E501
            "tags": ["session=md123", "source=claude"],
            "createTime": "2024-01-01T00:00:00Z",
        }
    ]

    messages, meta = wb.reconstruct_session(records)

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello world"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Hi there"
    assert meta["session_id"] == "md123"


def test_reconstruct_session_markdown_with_segment_prefix():
    """reconstruct_session 应移除 [N/M] «title» 分片前缀后再解析 Markdown"""
    records = [
        {
            "content": "[1/2] «test»\n\n## Turn 1\n\n**User** (gpt-4):\n\nHello\n\n**Assistant**:\n\nWorld\n\n---\n",  # noqa: E501
            "tags": ["session=seg123"],
            "createTime": "2024-01-01T00:00:00Z",
        }
    ]

    messages, _ = wb.reconstruct_session(records)

    assert len(messages) == 2
    assert messages[0]["content"] == "Hello"
    assert messages[1]["content"] == "World"


# ---------------------------------------------------------------------------
# 4. reconstruct_session — 纯文本 fallback
# ---------------------------------------------------------------------------


def test_reconstruct_session_plain_text_fallback():
    """无法解析为 JSON 或 Markdown 时，应将内容作为 system 角色消息 fallback"""
    records = [
        {
            "content": "This is just plain text without any structure",
            "tags": ["session=fallback123"],
            "createTime": "2024-01-01T00:00:00Z",
        }
    ]

    messages, meta = wb.reconstruct_session(records)

    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert "plain text" in messages[0]["content"]
    assert messages[0]["timestamp"] == "2024-01-01T00:00:00Z"
    assert meta["session_id"] == "fallback123"


def _pipeline_stats():
    return {
        "new_knowledge": [],
        "pipeline_used": 0,
        "processed": 0,
        "skip_reasons": [],
        "skipped_low_quality": 0,
        "failed": 0,
    }


def test_skill_session_is_processed_only_from_committed_asset_and_page_receipt(
    mock_backend, monkeypatch, tmp_path
):
    """COG-013: a display suggestion alone can never mark a session processed."""
    from core.hephaestus.distillation_models import (
        CognitionAssetCommitReceipt,
        DistillationResult,
        KnowledgeFragment,
    )
    from core.pipeline_receipts import DistillationWriteReceipt

    fragment = KnowledgeFragment(
        form="方法论",
        title="完整认知资产提交后的会话终态",
        frontmatter={"摘要": "仅在资产和页面都持久化后进入终态。", "领域": "认知"},
        background="旧路径只保存建议字符串。",
        core_content="# 完整认知资产\n" + "必须先提交 typed asset 再写入 Wiki。" * 12,
        boundaries={"applies": "skill 判断"},
        anti_patterns=["只保存 suggestion"],
        related_concepts=["typed receipt"],
    )
    result = DistillationResult(
        session_id="skill-session",
        judgment="skill",
        fragments=[fragment],
        cognition_asset_receipt=CognitionAssetCommitReceipt(
            status="committed",
            asset_id="cogasset-1",
            content_hash="sha256:asset",
        ),
    )
    page = tmp_path / "00-Inbox" / "skill.md"
    engine = Mock()
    engine.process.return_value = result
    engine.write_pages_with_receipt.return_value = DistillationWriteReceipt(
        status="committed",
        terminal_reason="all_expected_artifacts_committed",
        written_pages=(str(page),),
        expected_count=1,
        written_count=1,
        required_consumer_receipts=("cognition_asset:cogasset-1:committed",),
    )
    marked = Mock()
    monkeypatch.setattr(wb, "_mark_processed", marked)
    monkeypatch.setattr(wb, "_link_session_records_to_wiki", Mock())
    stats = _pipeline_stats()

    created = wb._process_distillation_result(
        engine,
        "skill-session",
        [],
        [{"role": "user", "content": "full cognition"}],
        {"source": "codex"},
        92.0,
        mock_backend,
        stats,
    )

    assert created == 1
    engine.write_pages_with_receipt.assert_called_once_with(result)
    marked.assert_called_once()
    assert marked.call_args.args[4] == str(page)
    assert marked.call_args.kwargs["method"] == "pipeline_skill"
    assert stats["new_knowledge"][0]["method"] == "pipeline_skill"
    assert stats["skip_reasons"] == []
    assert not hasattr(wb, "_emit_knowledge_distilled")


def test_skill_session_without_committed_asset_receipt_remains_retryable(mock_backend, monkeypatch):
    """A missing typed asset receipt must not be converted into processed/skip."""
    from core.hephaestus.distillation_models import DistillationResult, KnowledgeFragment
    from core.pipeline_receipts import DistillationWriteReceipt

    result = DistillationResult(
        session_id="skill-no-asset",
        judgment="skill",
        fragments=[
            KnowledgeFragment(
                form="方法论",
                title="缺少认知资产收据时不得完成",
                frontmatter={"摘要": "缺少资产收据。", "领域": "认知"},
                background="收据缺失。",
                core_content="# 缺少收据\n" + "该会话必须保留在可重试状态。" * 12,
                boundaries={"applies": "skill"},
                anti_patterns=[],
                related_concepts=[],
            )
        ],
    )
    engine = Mock()
    engine.process.return_value = result
    engine.write_pages_with_receipt.return_value = DistillationWriteReceipt(
        status="retryable_failed",
        terminal_reason="cognition_asset_commit_failed",
        expected_count=1,
        failed_count=1,
        required_consumer_receipts=("cognition_asset:unassigned:retryable_failed",),
    )
    marked = Mock()
    monkeypatch.setattr(wb, "_mark_processed", marked)
    stats = _pipeline_stats()

    created = wb._process_distillation_result(
        engine,
        "skill-no-asset",
        [],
        [{"role": "user", "content": "retry"}],
        {"source": "codex"},
        90.0,
        mock_backend,
        stats,
    )

    assert created == 0
    marked.assert_not_called()
    assert stats["failed"] == 1
    assert stats["processed"] == 0


# ---------------------------------------------------------------------------
# 5. _is_session_completed
# ---------------------------------------------------------------------------


def test_is_session_completed_true_when_old():
    """最新 chunk 超过 5 分钟应判定为已完成"""
    old_time = (
        (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat().replace("+00:00", "Z")
    )
    records = [{"createTime": old_time}]

    assert wb._is_session_completed("sid1", records) is True


def test_is_session_completed_false_when_recent():
    """最新 chunk 在 5 分钟内应判定为未完成"""
    recent_time = (
        (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    )
    records = [{"createTime": recent_time}]

    assert wb._is_session_completed("sid2", records) is False


def test_is_session_completed_false_when_empty():
    """空 records 列表应返回 False"""
    assert wb._is_session_completed("sid3", []) is False


def test_is_session_completed_uses_latest_time():
    """多个 chunk 时应以最新时间为准判断完成状态"""
    old_time = (
        (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat().replace("+00:00", "Z")
    )
    recent_time = (
        (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
    )
    records = [
        {"createTime": old_time},
        {"createTime": recent_time},
    ]

    assert wb._is_session_completed("sid4", records) is False


# ---------------------------------------------------------------------------
# 6. _is_processed / _mark_processed
# ---------------------------------------------------------------------------


def test_is_processed_and_mark_processed(tmp_db_path):
    """_mark_processed 写入后 _is_processed 应返回 True，未写入的 session 返回 False"""
    assert wb._is_processed("sid_a") is False

    wb._mark_processed("sid_a", "claude", 5, 75.0, method="pipeline")

    assert wb._is_processed("sid_a") is True
    assert wb._is_processed("sid_b") is False


def test_mark_processed_persists_wiki_path(tmp_db_path):
    """_mark_processed 写入 wiki_path 后应在 wiki_pages 表中可查询"""
    wb._mark_processed("sid_c", "hermes", 3, 60.0, wiki_path="00-Inbox/test.md", method="pipeline")

    with wb._get_conn() as conn:
        row = conn.execute(
            "SELECT file_path, type FROM wiki_pages WHERE page_id = ?",
            ("sid_c",),
        ).fetchone()

    assert row is not None
    assert row[0] == "00-Inbox/test.md"
    assert row[1] == "source"


def test_mark_processed_status_alignment(tmp_db_path):
    """_mark_processed 的 status 字段应与 distill_method 对齐"""
    wb._mark_processed("sid_d", "claude", 2, 30.0, method="skipped_low_quality")

    with wb._get_conn() as conn:
        row = conn.execute(
            "SELECT status, distill_method FROM processed_sessions WHERE session_id = ?",
            ("sid_d",),
        ).fetchone()

    assert row[1] == "skipped_low_quality"
    assert row[0] == "skipped_low_quality"


# ---------------------------------------------------------------------------
# 8. _mask_wiki_generated_blocks
# ---------------------------------------------------------------------------


def test_mask_wiki_generated_blocks_replaces_all_patterns():
    """_mask_wiki_generated_blocks 应替换所有三种 Wiki 注入块"""
    content = (
        "hello <wiki-context>secret</wiki-context> world "
        "<!-- wiki-injected -->injected<!-- /wiki-injected --> end "
        "<!-- auto-maintained -->auto<!-- /auto-maintained -->"
    )

    result = wb._mask_wiki_generated_blocks(content)

    assert "[wiki-context-blocked]" in result
    assert "[wiki-injected-blocked]" in result
    assert "[auto-maintained-blocked]" in result
    assert "secret" not in result
    assert "<wiki-context>" not in result
    assert "<!-- wiki-injected -->" not in result
    assert "<!-- auto-maintained -->" not in result
    assert "hello" in result
    assert "world" in result
    assert "end" in result


def test_mask_wiki_generated_blocks_leaves_normal_content():
    """普通内容应原样保留"""
    content = "This is normal content without any wiki markers."

    result = wb._mask_wiki_generated_blocks(content)

    assert result == content


# ---------------------------------------------------------------------------
# 10. _try_parse_json
# ---------------------------------------------------------------------------


def test_try_parse_json_valid():
    """标准 JSON 应正确解析"""
    data = {"messages": [{"role": "user", "content": "hello"}]}
    result = wb._try_parse_json(json.dumps(data))

    assert result == data


def test_try_parse_json_truncated_with_messages():
    """截断 JSON（messages 数组存在但后续缺失）应通过正则提取恢复"""
    content = '{"_meta": {"segment": "1/1"}, "messages": [{"role": "user", "content": "hello"}'
    result = wb._try_parse_json(content)

    assert result is not None
    assert "messages" in result
    assert result["messages"][0]["content"] == "hello"


def test_try_parse_json_returns_none_for_non_json():
    """非 JSON 内容应返回 None"""
    assert wb._try_parse_json("just plain text") is None
    assert wb._try_parse_json("not { starting") is None


# ---------------------------------------------------------------------------
# 11. _parse_markdown_turns
# ---------------------------------------------------------------------------


def test_parse_markdown_turns_standard_format():
    """标准 ## Turn N 格式应正确提取消息"""
    md = "## Turn 1\n\n**User** (gpt-4):\n\nHello\n\n**Assistant**:\n\nWorld\n\n---\n"
    result = wb._parse_markdown_turns(md)

    assert result is not None
    assert len(result) == 2
    assert result[0]["role"] == "user"
    assert result[0]["content"] == "Hello"
    assert result[1]["role"] == "assistant"
    assert result[1]["content"] == "World"


def test_parse_markdown_turns_simplified_format():
    """无 Turn 标题的简化格式应正确提取"""
    md = "**User** (gpt-4):\n\nHello\n\n**Assistant**:\n\nWorld\n\n---\n"
    result = wb._parse_markdown_turns(md)

    assert result is not None
    assert len(result) == 2


def test_parse_markdown_turns_skips_json():
    """JSON 内容应返回 None，避免错误解析"""
    result = wb._parse_markdown_turns('{"key": "value"}')
    assert result is None


def test_parse_markdown_turns_empty():
    """空内容应返回 None"""
    assert wb._parse_markdown_turns("") is None
    assert wb._parse_markdown_turns("   ") is None


# ---------------------------------------------------------------------------
# 8. get_stats
# ---------------------------------------------------------------------------


def test_get_stats_returns_structure(tmp_db_path):
    """get_stats 应返回包含预期字段的字典"""
    stats = wb.get_stats()

    assert "total_processed" in stats
    assert "avg_quality_score" in stats
    assert "source_pages" in stats
    assert "wiki_dir" in stats
    assert stats["total_processed"] == 0
    assert stats["avg_quality_score"] == 0
    assert stats["source_pages"] == 0


def test_get_stats_with_data(tmp_db_path):
    """写入数据后 get_stats 应反映正确统计"""
    wb._mark_processed("s1", "claude", 5, 80.0, method="pipeline")
    wb._mark_processed("s2", "hermes", 3, 60.0, method="pipeline")

    stats = wb.get_stats()

    assert stats["total_processed"] == 2
    assert stats["avg_quality_score"] == 70.0


def test_generated_empty_moc_has_substantive_operational_context(tmp_path):
    path = tmp_path / "empty-moc.md"

    wb._write_moc(path, "待复盘", "需要人工复核的页面。", [], lambda _page: "")

    from scripts.wiki_lint import extract_frontmatter

    _frontmatter, body = extract_frontmatter(path.read_text(encoding="utf-8"))
    assert len(body.strip()) >= 200
    assert "空列表含义" in body
    assert "不需要创建占位知识页" in body
