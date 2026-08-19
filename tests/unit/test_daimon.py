"""
SignalCollector (Daimon) 单元测试

覆盖公共行为：
1. SignalCollector.__init__() — 初始化（注入 store）
2. _parse_session_to_signal() — 从 session 数据解析信号
3. _estimate_duration() — 会话时长估算
4. _calculate_follow_up_depth() — 追问深度计算
5. _infer_termination_type() — 终止类型推断
6. _infer_output_type() — 产出类型推断
7. collect_from_distill_queue() — 从 distill_queue 采集信号
8. _parse_git_log() — 解析 git log 输出
9. _parse_git_stat() — 解析 git shortstat
10. _infer_commit_type() — 推断 commit 类型
11. collect_from_git() — Git 信号采集（含 subprocess mock）
12. _resolve_sources_from_config() — 根据配置解析启用的数据源
13. collect_all() — 统一采集入口
14. get_collection_summary() — 采集摘要

Mock 策略：
- SignalStore 全部 mock，不写入真实数据库
- git subprocess 用 mock 替换
- 文件系统操作使用 tmp_path
"""

from unittest.mock import MagicMock, patch

import pytest

from core.persona.daimon import SignalCollector
from core.persona.psyche import GitSignal

# ---------- Fixtures ----------


@pytest.fixture
def mock_store():
    """返回一个完全 mock 的 SignalStore。"""
    store = MagicMock()
    store.session_exists.return_value = False
    store.git_commit_exists.return_value = False
    store.knowledge_page_exists.return_value = False
    store.file_system_exists.return_value = False
    store.note_exists.return_value = False
    store.get_signal_stats.return_value = {}
    return store


@pytest.fixture
def collector(mock_store):
    """返回使用 mock store 的 SignalCollector。"""
    return SignalCollector(store=mock_store)


# ---------- 初始化 ----------


def test_init_uses_provided_store(mock_store):
    """初始化时应使用传入的 store 实例。"""
    c = SignalCollector(store=mock_store)
    assert c.store is mock_store


def test_init_defaults_to_get_signal_store():
    """未传入 store 时应调用 get_signal_store() 获取默认实例。"""
    with patch("core.persona.daimon.get_signal_store") as mock_get:
        mock_store = MagicMock()
        mock_get.return_value = mock_store
        c = SignalCollector()
        assert c.store is mock_store


# ---------- Session 信号解析 ----------


def test_parse_session_to_signal_basic(collector):
    """解析正常 session 数据应返回完整的 SessionSignal。"""
    data = {
        "session_id": "sess-001",
        "created_at": "2024-01-15T10:00:00",
        "task_type": "coding",
        "task_subtype": "python",
        "working_dir": "/tmp/proj",
        "agent": "claude",
        "messages": [
            {"role": "user", "content": "帮我写个函数", "timestamp": "2024-01-15T10:00:00"},
            {"role": "assistant", "content": "好的", "timestamp": "2024-01-15T10:01:00"},
            {"role": "user", "content": "不对，应该这样", "timestamp": "2024-01-15T10:02:00"},
        ],
    }
    signal = collector._parse_session_to_signal(data)
    assert signal is not None
    assert signal.session_id == "sess-001"
    assert signal.task_type == "coding"
    assert signal.task_subtype == "python"
    assert signal.user_msg_count == 2
    assert signal.correction_count == 1
    assert signal.follow_up_depth == 1
    assert signal.working_dir == "/tmp/proj"
    assert signal.agent == "claude"
    assert signal.duration_seconds == 120


def test_parse_session_to_signal_no_user_messages(collector):
    """没有用户消息时应返回 None。"""
    data = {
        "session_id": "sess-002",
        "messages": [
            {"role": "assistant", "content": "你好"},
        ],
    }
    assert collector._parse_session_to_signal(data) is None


def test_parse_session_to_signal_empty_messages(collector):
    """空消息列表时应返回 None。"""
    data = {"session_id": "sess-003", "messages": []}
    assert collector._parse_session_to_signal(data) is None


def test_parse_session_to_signal_context_tags_json_string(collector):
    """context_tags 为 JSON 字符串时应正确解析。"""
    data = {
        "session_id": "sess-004",
        "messages": [{"role": "user", "content": "test"}],
        "context_tags": '["coding", "python"]',
    }
    signal = collector._parse_session_to_signal(data)
    assert signal.context_tags == ["coding", "python"]


def test_parse_session_to_signal_context_tags_list(collector):
    """context_tags 为列表时应直接使用。"""
    data = {
        "session_id": "sess-005",
        "messages": [{"role": "user", "content": "test"}],
        "context_tags": ["debugging"],
    }
    signal = collector._parse_session_to_signal(data)
    assert signal.context_tags == ["debugging"]


# ---------- 时长估算 ----------


def test_estimate_duration_with_timestamps(collector):
    """消息有时间戳时应正确估算时长。"""
    messages = [
        {"role": "user", "timestamp": "2024-01-15T10:00:00"},
        {"role": "assistant", "timestamp": "2024-01-15T10:05:00"},
    ]
    duration = collector._estimate_duration(messages)
    assert duration == 300.0


def test_estimate_duration_single_message(collector):
    """单条消息时返回 0。"""
    messages = [{"role": "user", "timestamp": "2024-01-15T10:00:00"}]
    assert collector._estimate_duration(messages) == 0.0


def test_estimate_duration_no_timestamps(collector):
    """无时间戳时返回 0。"""
    messages = [{"role": "user", "content": "hello"}]
    assert collector._estimate_duration(messages) == 0.0


def test_estimate_duration_z_suffix(collector):
    """处理带 Z 后缀的 ISO 时间戳。"""
    messages = [
        {"role": "user", "timestamp": "2024-01-15T10:00:00Z"},
        {"role": "user", "timestamp": "2024-01-15T10:00:30Z"},
    ]
    assert collector._estimate_duration(messages) == 30.0


# ---------- 追问深度 ----------


def test_calculate_follow_up_depth(collector):
    """计算追问深度：用户回复 assistant 后的追问次数。"""
    messages = [
        {"role": "user", "content": "问题1"},
        {"role": "assistant", "content": "回答1"},
        {"role": "user", "content": "追问1"},
        {"role": "assistant", "content": "回答2"},
        {"role": "user", "content": "追问2"},
    ]
    assert collector._calculate_follow_up_depth(messages) == 2


def test_calculate_follow_up_depth_no_assistant(collector):
    """没有 assistant 回复时深度为 0。"""
    messages = [
        {"role": "user", "content": "问题1"},
        {"role": "user", "content": "问题2"},
    ]
    assert collector._calculate_follow_up_depth(messages) == 0


# ---------- 终止类型推断 ----------


def test_infer_termination_type_satisfied(collector):
    """包含满意关键词时返回 satisfied。"""
    messages = [{"role": "user", "content": "好的，完美"}]
    assert collector._infer_termination_type(messages) == "satisfied"


def test_infer_termination_type_progress(collector):
    """包含推进关键词时返回 progress。"""
    messages = [{"role": "user", "content": "开始吧，下一步"}]
    assert collector._infer_termination_type(messages) == "progress"


def test_infer_termination_type_delegated(collector):
    """包含委托关键词时返回 delegated。"""
    messages = [{"role": "user", "content": "你决定吧"}]
    assert collector._infer_termination_type(messages) == "delegated"


def test_infer_termination_type_abandoned(collector):
    """包含放弃关键词时返回 abandoned。"""
    messages = [{"role": "user", "content": "算了，放弃"}]
    assert collector._infer_termination_type(messages) == "abandoned"


def test_infer_termination_type_unknown(collector):
    """无匹配关键词时返回 unknown。"""
    messages = [{"role": "user", "content": "随便说点"}]
    assert collector._infer_termination_type(messages) == "unknown"


def test_infer_termination_type_empty(collector):
    """空消息列表返回空字符串。"""
    assert collector._infer_termination_type([]) == ""


# ---------- 产出类型推断 ----------


def test_infer_output_type_code(collector):
    """包含代码块时返回 code。"""
    messages = [{"role": "user", "content": "```python\ndef foo():\n    pass\n```"}]
    assert collector._infer_output_type(messages, {}) == "code"


def test_infer_output_type_document(collector):
    """包含标题且长度足够时返回 document。"""
    messages = [{"role": "user", "content": "# 标题\n" + "x" * 600}]
    assert collector._infer_output_type(messages, {}) == "document"


def test_infer_output_type_decision(collector):
    """包含决策关键词时返回 decision。"""
    messages = [{"role": "user", "content": "选哪个方案"}]
    assert collector._infer_output_type(messages, {}) == "decision"


def test_infer_output_type_discussion(collector):
    """无特征时返回 discussion。"""
    messages = [{"role": "user", "content": "随便聊聊"}]
    assert collector._infer_output_type(messages, {}) == "discussion"


# ---------- Distill Queue 采集 ----------


def _init_amphora_db(tmp_path, monkeypatch):
    """将 amphora DB 指向临时路径并初始化。"""
    from core.kia import amphora

    monkeypatch.setattr(amphora, "_DB_PATH", tmp_path / "amphora.db")
    amphora._init_db()


def test_collect_from_distill_queue_empty_queue(collector, tmp_path, monkeypatch):
    """amphora 队列为空时返回 0。"""
    _init_amphora_db(tmp_path, monkeypatch)
    assert collector.collect_from_distill_queue() == 0


def test_collect_from_distill_queue_parses_valid_session(collector, tmp_path, monkeypatch):
    """正确解析 amphora 任务并插入信号。"""
    from core.kia import amphora

    _init_amphora_db(tmp_path, monkeypatch)
    amphora.enqueue_with_receipt(
        "sess-dq-001",
        [{"role": "user", "content": "hello"}],
        meta={"source": "claude", "task_type": "coding"},
    )

    count = collector.collect_from_distill_queue()
    assert count == 1

    # 验证 store.insert_session_signal 被调用且参数正确
    call_args = collector.store.insert_session_signal.call_args[0][0]
    assert call_args.session_id == "sess-dq-001"
    assert call_args.task_type == "coding"


def test_collect_from_distill_queue_deduplication(collector, tmp_path, monkeypatch):
    """已存在的 session 应被跳过。"""
    from core.kia import amphora

    _init_amphora_db(tmp_path, monkeypatch)
    amphora.enqueue_with_receipt(
        "sess-dup",
        [{"role": "user", "content": "hello"}],
        meta={"source": "claude"},
    )

    collector.store.session_exists.return_value = True
    assert collector.collect_from_distill_queue() == 0
    collector.store.insert_session_signal.assert_not_called()


# ---------- Git Log 解析 ----------


def test_parse_git_log_basic(collector):
    """解析标准 git log --format=%H|%ci|%s --shortstat 输出。"""
    log_output = (
        "abc123|2024-01-15T10:00:00+08:00|feat: add feature\n"
        " 3 files changed, 50 insertions(+), 10 deletions(-)\n"
        "def456|2024-01-15T11:00:00+08:00|fix: bugfix\n"
    )
    commits = collector._parse_git_log(log_output)
    assert len(commits) == 2
    assert commits[0]["hash"] == "abc123"
    assert commits[0]["message"] == "feat: add feature"
    assert commits[0]["timestamp"] == "2024-01-15T10:00:00+08:00"
    assert commits[0]["files_changed"] == 3
    assert commits[0]["lines_added"] == 50
    assert commits[0]["lines_deleted"] == 10


def test_parse_git_log_no_stat(collector):
    """无 shortstat 行时基本字段仍应正确解析。"""
    log_output = "abc123|2024-01-15T10:00:00+08:00|initial commit\n"
    commits = collector._parse_git_log(log_output)
    assert len(commits) == 1
    assert commits[0]["hash"] == "abc123"
    assert commits[0].get("files_changed", 0) == 0


def test_parse_git_log_empty(collector):
    """空输入返回空列表。"""
    assert collector._parse_git_log("") == []


# ---------- Git Stat 解析 ----------


def test_parse_git_stat_full(collector):
    """完整 shortstat 行解析。"""
    stat = collector._parse_git_stat(" 5 files changed, 100 insertions(+), 20 deletions(-)")
    assert stat["files_changed"] == 5
    assert stat["lines_added"] == 100
    assert stat["lines_deleted"] == 20


def test_parse_git_stat_single_file(collector):
    """单文件变更解析。"""
    stat = collector._parse_git_stat(" 1 file changed, 10 insertions(+), 5 deletions(-)")
    assert stat["files_changed"] == 1
    assert stat["lines_added"] == 10
    assert stat["lines_deleted"] == 5


def test_parse_git_stat_test_files(collector):
    """包含 test 关键词时应标记 test_files_changed。"""
    stat = collector._parse_git_stat(" 2 files changed, 10 insertions(+) in test_foo.py")
    assert stat["test_files_changed"] == 1


def test_parse_git_stat_empty(collector):
    """空行返回全 0 结果。"""
    stat = collector._parse_git_stat("")
    assert stat["files_changed"] == 0
    assert stat["lines_added"] == 0
    assert stat["lines_deleted"] == 0


# ---------- Commit 类型推断 ----------


def test_infer_commit_type_feat(collector):
    """feat 前缀识别。"""
    assert collector._infer_commit_type("feat: add login") == "feat"
    assert collector._infer_commit_type("feature: add login") == "feat"
    assert collector._infer_commit_type("add login page") == "feat"


def test_infer_commit_type_fix(collector):
    """fix 前缀识别。"""
    assert collector._infer_commit_type("fix: resolve bug") == "fix"
    assert collector._infer_commit_type("bugfix: resolve bug") == "fix"


def test_infer_commit_type_docs(collector):
    """docs 前缀识别。"""
    assert collector._infer_commit_type("docs: update README") == "docs"


def test_infer_commit_type_test(collector):
    """test 前缀识别。"""
    assert collector._infer_commit_type("test: add unit tests") == "test"


def test_infer_commit_type_other(collector):
    """无匹配时返回 other。"""
    assert collector._infer_commit_type("random message") == "other"


# ---------- Git 采集（含 subprocess mock） ----------


def test_collect_from_git_with_mocked_subprocess(collector, tmp_path):
    """mock git log 输出，验证信号正确解析和存储。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    git_output = (
        "abc123|2024-01-15T10:00:00+08:00|feat: add feature\n"
        " 2 files changed, 30 insertions(+), 5 deletions(-)\n"
    )

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = git_output

    with patch("core.persona.daimon.subprocess.run", return_value=mock_result):
        count = collector.collect_from_git(repo_paths=[str(repo_path)])

    assert count == 1
    call_args = collector.store.insert_git_signal.call_args[0][0]
    assert isinstance(call_args, GitSignal)
    assert call_args.commit_hash == "abc123"
    assert call_args.commit_type == "feat"
    assert call_args.files_changed == 2
    assert call_args.lines_added == 30
    assert call_args.lines_deleted == 5


def test_collect_from_git_skips_failed_repos(collector, tmp_path):
    """git 命令失败时应跳过该仓库。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""

    with patch("core.persona.daimon.subprocess.run", return_value=mock_result):
        assert collector.collect_from_git(repo_paths=[str(repo_path)]) == 0

    collector.store.insert_git_signal.assert_not_called()


def test_collect_from_git_deduplication(collector, tmp_path):
    """已存在的 commit 应被跳过。"""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    git_output = "abc123|2024-01-15T10:00:00+08:00|feat: add feature\n"

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = git_output

    collector.store.git_commit_exists.return_value = True

    with patch("core.persona.daimon.subprocess.run", return_value=mock_result):
        assert collector.collect_from_git(repo_paths=[str(repo_path)]) == 0

    collector.store.insert_git_signal.assert_not_called()


# ---------- 数据源配置解析 ----------


def test_resolve_sources_from_config_persona_disabled(collector):
    """画像系统关闭时至少保留 session 源。"""
    mock_config = MagicMock()
    mock_config.persona_enabled = False
    mock_config.persona_data_sources = {}

    with patch("core.persona.daimon.get_config", return_value=mock_config):
        sources = collector._resolve_sources_from_config()
        assert sources == ["session"]


def test_resolve_sources_from_config_all_enabled(collector):
    """所有数据源启用时应返回完整列表。"""
    mock_config = MagicMock()
    mock_config.persona_enabled = True
    mock_config.persona_data_sources = {
        "session": {"enabled": True},
        "git": {"enabled": True},
        "wiki": {"enabled": True},
        "file_system": {"enabled": True},
    }

    with patch("core.persona.daimon.get_config", return_value=mock_config):
        sources = collector._resolve_sources_from_config()
        assert "session" in sources
        assert "wiki_state" in sources
        assert "sync_engine" in sources
        assert "git" in sources
        assert "wiki" in sources
        assert "file_system" in sources


def test_resolve_sources_from_config_rejects_enabled_unsupported_source(collector):
    """启用未实现的数据源时不能静默 no-op。"""
    mock_config = MagicMock()
    mock_config.persona_enabled = True
    mock_config.persona_data_sources = {
        "session": {"enabled": True},
        "notes": {"enabled": True},
    }

    with patch("core.persona.daimon.get_config", return_value=mock_config):
        with pytest.raises(ValueError, match="unsupported enabled persona data source"):
            collector._resolve_sources_from_config()


def test_resolve_sources_from_config_partial(collector):
    """部分数据源禁用时只返回启用的源。"""
    mock_config = MagicMock()
    mock_config.persona_enabled = True
    mock_config.persona_data_sources = {
        "session": {"enabled": True},
        "git": {"enabled": False},
        "notes": {"enabled": False},
        "wiki": {"enabled": False},
        "file_system": {"enabled": False},
    }

    with patch("core.persona.daimon.get_config", return_value=mock_config):
        sources = collector._resolve_sources_from_config()
        assert sources == ["session", "wiki_state", "sync_engine"]


# ---------- 统一采集入口 ----------


def test_collect_all_with_explicit_sources(collector):
    """显式指定 sources 时只采集指定源。"""
    with patch.object(collector, "collect_from_distill_queue", return_value=3):
        results = collector.collect_all(sources=["session"])
        assert results == {"session": 3}


def test_collect_all_rejects_unknown_explicit_source(collector):
    """显式请求未注册 collector 时应失败。"""
    with pytest.raises(ValueError, match="unsupported persona SignalCollector source"):
        collector.collect_all(sources=["notes"])


def test_collect_all_error_handling(collector):
    """采集器异常时应返回 -1 错误标记。"""
    with patch.object(collector, "collect_from_distill_queue", side_effect=OSError("boom")):
        results = collector.collect_all(sources=["session"])
        assert results == {"session": -1}


def test_collect_all_uses_config_when_sources_none(collector):
    """sources 为 None 时从配置解析。"""
    mock_config = MagicMock()
    mock_config.persona_enabled = False
    mock_config.persona_data_sources = {}

    with patch("core.persona.daimon.get_config", return_value=mock_config):
        with patch.object(collector, "collect_from_distill_queue", return_value=5):
            results = collector.collect_all()
            assert results == {"session": 5}


# ---------- 采集摘要 ----------


def test_get_collection_summary(collector):
    """get_collection_summary 应返回格式化的摘要字符串。"""
    collector.store.get_signal_stats.return_value = {
        "session": 10,
        "git": 5,
        "knowledge": 3,
    }
    summary = collector.get_collection_summary()
    assert "信号采集摘要" in summary
    assert "session: 10 条信号" in summary
    assert "git: 5 条信号" in summary
    assert "总计: 18 条" in summary


# ---------- Wiki State 采集 ----------


def test_collect_from_wiki_state_no_db(collector, tmp_path):
    """wiki_state.db 不存在时返回 0。"""
    with patch.object(collector, "_wiki_state_db", return_value=tmp_path / "nonexistent.db"):
        assert collector.collect_from_wiki_state() == 0


# ---------- 便捷函数 ----------


def test_collect_all_signals_function():
    """collect_all_signals 便捷函数应返回采集结果。"""
    from core.persona.daimon import collect_all_signals

    with patch("core.persona.daimon.SignalCollector") as MockCollector:
        mock_instance = MagicMock()
        mock_instance.collect_all.return_value = {"session": 1}
        MockCollector.return_value = mock_instance

        result = collect_all_signals()
        assert result == {"session": 1}
        mock_instance.collect_all.assert_called_once_with(None)


def test_collect_and_log_function():
    """collect_and_log 便捷函数应调用 collect_all 并返回结果。"""
    from core.persona.daimon import collect_and_log

    with patch("core.persona.daimon.SignalCollector") as MockCollector:
        mock_instance = MagicMock()
        mock_instance.collect_all.return_value = {"session": 2}
        mock_instance.get_collection_summary.return_value = "summary"
        MockCollector.return_value = mock_instance

        result = collect_and_log()
        assert result == {"session": 2}
        mock_instance.get_collection_summary.assert_called_once()
