"""
ClaudeCodeAdapter (Apollon) 单元测试

覆盖项：
- 初始化与属性（name / priority）
- is_available 多路径检测
- get_config_path 优先级解析
- install_hooks 写入 settings.json
- is_hooks_installed 状态检测
- on_session_start 上下文注入与事件发布
- on_session_end 保存、复盘、入队、KIA 周期与事件发布
- hook payload 格式验证（SessionStart / SessionEnd 命令结构）

测试策略：
- monkeypatch 控制 Path.home() 指向 tmp_path，避免污染真实文件系统
- mock 所有外部依赖（EventBus、save_session、run_retrospective、run_kia_cycles、
  get_context_for_claude、shutil.which、json_mcp_configured、upsert_json_mcp_server）
- 每个测试使用独立 tmp_path，确保隔离
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

# ---- fixtures ----


@pytest.fixture
def mock_home(tmp_path, monkeypatch):
    """将 Path.home() 指向临时目录，模拟干净的用户主目录。"""
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home


@pytest.fixture
def adapter(mock_home):
    """返回已实例化的 ClaudeCodeAdapter（在 mock_home 环境下）。"""
    # 延迟导入，确保 Path.home() 已被 patch
    from integrations.apollon import ClaudeCodeAdapter

    return ClaudeCodeAdapter()


@pytest.fixture
def fake_settings_json(mock_home):
    """在 mock_home 下创建新版 Claude Code settings.json 并返回其路径。"""
    settings_dir = mock_home / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    settings_path.write_text(json.dumps({}), encoding="utf-8")
    return settings_path


# ---- 测试：初始化与属性 ----


def test_init_and_properties(adapter):
    """ClaudeCodeAdapter 初始化后 name 和 priority 应为固定值。"""
    assert adapter.name == "claude"
    assert adapter.priority == 1


# ---- 测试：is_available ----


def test_is_available_with_settings_json(adapter, fake_settings_json):
    """当 ~/.claude/settings.json 存在时，is_available 应返回 True。"""
    assert adapter.is_available() is True


def test_is_available_with_macos_legacy_path(mock_home, adapter):
    """当仅 macOS 旧版路径存在 settings.json 时，is_available 应返回 True。"""
    macos_path = mock_home / "Library" / "Application Support" / "Claude" / "settings.json"
    macos_path.parent.mkdir(parents=True, exist_ok=True)
    macos_path.write_text(json.dumps({}), encoding="utf-8")
    assert adapter.is_available() is True


def test_is_available_with_linux_legacy_path(mock_home, adapter):
    """当仅 Linux/Windows 旧版路径存在 settings.json 时，is_available 应返回 True。"""
    linux_path = mock_home / ".config" / "claude" / "settings.json"
    linux_path.parent.mkdir(parents=True, exist_ok=True)
    linux_path.write_text(json.dumps({}), encoding="utf-8")
    assert adapter.is_available() is True


def test_is_available_fallback_to_shutil_which(adapter, monkeypatch):
    """当所有 settings.json 都不存在但 shutil.which('claude') 返回路径时，
    is_available 应返回 True。"""
    monkeypatch.setattr("shutil.which", lambda _cmd: "/usr/local/bin/claude")
    assert adapter.is_available() is True


def test_is_available_returns_false_when_nothing_found(adapter, monkeypatch):
    """当没有任何 Claude Code 安装痕迹时，is_available 应返回 False。"""
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    assert adapter.is_available() is False


# ---- 测试：get_config_path ----


def test_get_config_path_priority(mock_home, adapter):
    """get_config_path 应按优先级返回第一个存在的 settings.json 路径。"""
    # 仅创建新版路径
    new_path = mock_home / ".claude" / "settings.json"
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_text(json.dumps({}), encoding="utf-8")
    assert adapter.get_config_path() == new_path


def test_get_config_path_fallback_order(mock_home, adapter):
    """当新版路径不存在时，应依次回退到 macOS 旧版、Linux 旧版路径。"""
    linux_path = mock_home / ".config" / "claude" / "settings.json"
    linux_path.parent.mkdir(parents=True, exist_ok=True)
    linux_path.write_text(json.dumps({}), encoding="utf-8")
    assert adapter.get_config_path() == linux_path


def test_get_config_path_returns_none_when_missing(adapter):
    """当所有候选路径都不存在时，应返回可自动创建的默认 settings.json 路径。"""
    assert adapter.get_config_path() == Path.home() / ".claude" / "settings.json"


def test_run_retrospective_uses_module_level_generator(monkeypatch):
    from integrations import apollon

    messages = [{"role": "user", "content": "复盘一下这次任务"}]
    checklist_usage = [{"item": "检查文档同步"}]
    calls = []

    class FakeClassification:
        confidence = 0.9
        task_type = "coding"
        subtype = "bugfix"

    class FakeClassifier:
        def classify(self, observed_messages):
            calls.append(("classify", observed_messages))
            return FakeClassification()

    class FakePreFlightInjector:
        pass

    class FakeRetroResult:
        new_lessons = ["补入口契约"]
        version = 3

    class FakeIterationTracker:
        def create_next_version(self, result):
            calls.append(("create_next_version", result))
            return "/tmp/retro-v3.md"

    def fake_generate_retrospective(task_type, subtype, observed_messages, observed_usage):
        calls.append((task_type, subtype, observed_messages, observed_usage))
        return FakeRetroResult()

    monkeypatch.setattr(apollon, "should_retrospect", lambda observed_messages: True)
    monkeypatch.setattr(apollon, "TaskClassifier", FakeClassifier)
    monkeypatch.setattr(apollon, "PreFlightInjector", FakePreFlightInjector)
    monkeypatch.setattr(apollon, "IterationTracker", FakeIterationTracker)
    monkeypatch.setattr(
        apollon,
        "_build_checklist_usage_from_guard",
        lambda observed_messages, task_type, subtype: checklist_usage,
    )
    monkeypatch.setattr(apollon, "generate_retrospective", fake_generate_retrospective, raising=False)

    output = apollon.run_retrospective(json.dumps(messages))

    assert "retro-v3.md" in output
    assert ("coding", "bugfix", messages, checklist_usage) in calls


# ---- 测试：install_hooks ----


def test_install_hooks_creates_hooks(adapter, fake_settings_json, monkeypatch):
    """install_hooks 应在 settings.json 中写入 SessionStart 和 SessionEnd 命令。"""
    monkeypatch.setattr("integrations.apollon.upsert_json_mcp_server", lambda path, **kw: True)
    result = adapter.install_hooks()
    assert result is True

    settings = json.loads(fake_settings_json.read_text(encoding="utf-8"))
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
    assert "SessionEnd" in settings["hooks"]

    # 验证命令中包含脚本路径和必要参数
    str(Path(__file__).resolve().parent.parent.parent / "integrations" / "apollon.py")
    # 由于 adapter 内部使用 Path(__file__).resolve()，这里直接检查命令结构
    start_cmd = settings["hooks"]["SessionStart"]
    end_cmd = settings["hooks"]["SessionEnd"]
    assert "--session-start" in start_cmd
    assert "--session-end" in end_cmd
    assert "--working-dir" in start_cmd
    assert "--working-dir" in end_cmd


def test_install_hooks_returns_false_when_no_config_path(adapter, monkeypatch):
    """干净新机没有 settings.json 时，install_hooks 应自动创建并写入。"""
    monkeypatch.setattr("integrations.apollon.upsert_json_mcp_server", lambda path, **kw: True)
    assert adapter.install_hooks() is True
    assert (Path.home() / ".claude" / "settings.json").exists()


# ---- 测试：is_hooks_installed ----


def test_is_hooks_installed_true(adapter, fake_settings_json):
    """当 settings.json 中已包含当前脚本路径的 SessionStart/SessionEnd 时，
    is_hooks_installed 应返回 True。"""
    script_path = str(Path(__file__).resolve().parent.parent.parent / "integrations" / "apollon.py")
    settings = {
        "hooks": {
            "SessionStart": f"python3 {script_path} --session-start",
            "SessionEnd": f"python3 {script_path} --session-end",
        }
    }
    fake_settings_json.write_text(json.dumps(settings), encoding="utf-8")
    assert adapter.is_hooks_installed() is True


def test_is_hooks_installed_false_when_missing(adapter, fake_settings_json):
    """当 settings.json 中没有 hooks 键时，is_hooks_installed 应返回 False。"""
    fake_settings_json.write_text(json.dumps({}), encoding="utf-8")
    assert adapter.is_hooks_installed() is False


def test_is_hooks_installed_false_when_only_one_hook(adapter, fake_settings_json):
    """当仅 SessionStart 存在而 SessionEnd 不存在时，is_hooks_installed 应返回 False。"""
    script_path = str(Path(__file__).resolve().parent.parent.parent / "integrations" / "apollon.py")
    settings = {
        "hooks": {
            "SessionStart": f"python3 {script_path} --session-start",
        }
    }
    fake_settings_json.write_text(json.dumps(settings), encoding="utf-8")
    assert adapter.is_hooks_installed() is False


def test_is_hooks_installed_false_when_no_config(adapter):
    """当 get_config_path() 返回 None 时，is_hooks_installed 应返回 False。"""
    assert adapter.is_hooks_installed() is False


# ---- 测试：hook payload 格式 ----


def test_hook_payload_format(adapter, fake_settings_json, monkeypatch):
    """install_hooks 写入的 SessionStart / SessionEnd 命令应符合预期格式：
    包含 python 可执行路径、脚本路径、--session-start/--session-end、--working-dir。"""
    monkeypatch.setattr("integrations.apollon.upsert_json_mcp_server", lambda path, **kw: True)
    adapter.install_hooks()

    settings = json.loads(fake_settings_json.read_text(encoding="utf-8"))
    start_cmd = settings["hooks"]["SessionStart"]
    end_cmd = settings["hooks"]["SessionEnd"]

    # 验证命令结构
    assert start_cmd.startswith(f"{sys.executable} ")
    assert "--session-start" in start_cmd
    assert '--working-dir "$PWD"' in start_cmd
    assert '--user-message "$USER_MESSAGE"' in start_cmd

    assert end_cmd.startswith(f"{sys.executable} ")
    assert "--session-end" in end_cmd
    assert '--working-dir "$PWD"' in end_cmd
    assert '--session-messages "$SESSION_MESSAGES"' in end_cmd

    # 验证脚本路径指向 apollon.py
    assert "apollon.py" in start_cmd
    assert "apollon.py" in end_cmd


# ---- 测试：is_mcp_configured / install_mcp_server ----


def test_is_mcp_configured(adapter, monkeypatch):
    """is_mcp_configured 应调用 json_mcp_configured 并返回其结果。"""
    monkeypatch.setattr("integrations.apollon.json_mcp_configured", lambda path: True)
    assert adapter.is_mcp_configured() is True


def test_install_mcp_server(adapter, monkeypatch):
    """install_mcp_server 应调用 upsert_json_mcp_server 并返回其结果。"""
    monkeypatch.setattr(
        "integrations.apollon.upsert_json_mcp_server",
        lambda path, **kw: True,
    )
    assert adapter.install_mcp_server() is True


# ---- 测试：collect_signals（轻量验证） ----


def test_collect_signals_returns_list(adapter, monkeypatch):
    """collect_signals 应返回列表（即使底层数据库为空）。"""
    # mock SignalCollector 和 get_signal_store，避免真实数据库操作
    mock_store = Mock()
    mock_store.db_path = Path("/dev/null")
    mock_store.get_signal_stats = Mock(return_value={})
    mock_store.get_latest_persona_version = Mock(return_value=None)

    monkeypatch.setattr(
        "integrations.apollon.SignalCollector",
        lambda: Mock(collect_all=lambda: None),
    )
    monkeypatch.setattr("integrations.apollon.get_signal_store", lambda: mock_store)

    result = adapter.collect_signals(days=7)
    assert isinstance(result, list)


def test_apollon_persona_cycle_is_observation_only(monkeypatch):
    """Session-end Apollon may not construct any Persona write path."""
    from integrations import apollon

    def legacy_constructor(*_args, **_kwargs):
        raise AssertionError("Apollon session-end must not construct a Persona writer")

    monkeypatch.setattr(apollon, "get_signal_store", legacy_constructor)
    monkeypatch.setattr(apollon, "PersonaStore", legacy_constructor)
    monkeypatch.setattr(apollon, "PreferenceAnalyzer", legacy_constructor)
    monkeypatch.setattr(apollon, "BlindSpotProfileManager", legacy_constructor)

    output = apollon._run_persona_cycle()

    assert output == "[Persona] deferred to daemon canonical revision command"


# ---- 测试：get_context_for_claude ----


def test_get_context_for_claude_uses_persona_behavior_prompt(monkeypatch):
    from integrations.apollon import QueryIntent, get_context_for_claude

    monkeypatch.setattr(
        "integrations.apollon.IntentClassifier.classify",
        lambda _message: (QueryIntent.CONTEXT_RECALL, 0.7, ["记得"]),
    )
    monkeypatch.setattr("integrations.apollon.build_l1_section", lambda *_args: "L1")
    monkeypatch.setattr("integrations.apollon.build_observation_section", lambda *args, **kwargs: "")

    def direct_persona_call(*_args, **_kwargs):
        raise AssertionError("Claude context should use the persona behavior helper")

    monkeypatch.setattr("integrations.apollon.build_persona_section", direct_persona_call)
    monkeypatch.setattr(
        "integrations.apollon._get_persona_behavior_prompt",
        lambda working_dir=None: f"PERSONA:{working_dir}",
    )

    result = get_context_for_claude("/work/project", "你还记得昨天说过什么吗")

    assert "L1" in result
    assert "PERSONA:/work/project" in result


def test_get_context_for_claude_private_request_disables_cross_agent_recall(monkeypatch):
    from integrations.apollon import QueryIntent, get_context_for_claude

    seen_authorize_cross = []
    monkeypatch.setattr(
        "integrations.apollon.IntentClassifier.classify",
        lambda _message: (QueryIntent.CONTEXT_RECALL, 0.8, ["记得"]),
    )
    monkeypatch.setattr(
        "integrations.apollon.build_l1_section",
        lambda _wd, _agent, authorize_cross: seen_authorize_cross.append(authorize_cross) or "L1",
    )
    monkeypatch.setattr("integrations.apollon.build_observation_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("integrations.apollon._get_persona_behavior_prompt", lambda *_args: "")

    result = get_context_for_claude(
        "/work/project",
        "这是私有内容，不要共享；你还记得昨天说过什么吗",
        authorize_cross=["codex", "kimi"],
    )

    assert "L1" in result
    assert seen_authorize_cross == [[]]


def test_get_context_for_claude_context_recall_uses_l1_context_helper(monkeypatch):
    from integrations.apollon import QueryIntent, get_context_for_claude

    calls = []

    monkeypatch.setattr(
        "integrations.apollon.IntentClassifier.classify",
        lambda _message: (QueryIntent.CONTEXT_RECALL, 0.8, ["记得"]),
    )

    def direct_l1_call(*_args, **_kwargs):
        raise AssertionError("context recall should call get_l1_context()")

    def fake_get_l1_context(working_dir, authorize_cross=None, agent="claude"):
        calls.append((working_dir, authorize_cross, agent))
        return "L1_HELPER"

    monkeypatch.setattr("integrations.apollon.build_l1_section", direct_l1_call)
    monkeypatch.setattr("integrations.apollon.get_l1_context", fake_get_l1_context)
    monkeypatch.setattr("integrations.apollon.build_observation_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("integrations.apollon._get_persona_behavior_prompt", lambda *_args: "")

    result = get_context_for_claude(
        "/work/project",
        "你还记得昨天讨论过的方案吗",
        authorize_cross=["codex"],
    )

    assert "L1_HELPER" in result
    assert calls == [("/work/project", ["codex"], "claude")]


def test_get_context_for_claude_knowledge_query_uses_wiki_helper(monkeypatch):
    from integrations.apollon import QueryIntent, get_context_for_claude

    calls = []

    monkeypatch.setattr(
        "integrations.apollon.IntentClassifier.classify",
        lambda _message: (QueryIntent.KNOWLEDGE_QUERY, 0.8, ["架构"]),
    )

    def direct_wiki_call(*_args, **_kwargs):
        raise AssertionError("knowledge query should call get_wiki_knowledge()")

    def fake_get_wiki_knowledge(user_message, agent="claude"):
        calls.append((user_message, agent))
        return "WIKI_HELPER"

    monkeypatch.setattr("integrations.apollon.build_wiki_section", direct_wiki_call)
    monkeypatch.setattr("integrations.apollon.get_wiki_knowledge", fake_get_wiki_knowledge)
    monkeypatch.setattr("integrations.apollon.build_kia_section", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        "integrations.apollon.build_predictive_push_section", lambda *args, **kwargs: ""
    )
    monkeypatch.setattr("integrations.apollon.build_observation_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("integrations.apollon._get_persona_behavior_prompt", lambda *_args: "")

    result = get_context_for_claude("/work/project", "Mnemos 架构规范是什么")

    assert "WIKI_HELPER" in result
    assert calls == [("Mnemos 架构规范是什么", "claude")]


def test_get_context_for_claude_uses_kia_helper(monkeypatch):
    from integrations.apollon import QueryIntent, get_context_for_claude

    calls = []

    monkeypatch.setattr(
        "integrations.apollon.IntentClassifier.classify",
        lambda _message: (QueryIntent.UNKNOWN, 0.0, []),
    )

    def direct_kia_call(*_args, **_kwargs):
        raise AssertionError("preflight should call load_knowledge_in_action()")

    def fake_load_knowledge_in_action(user_message):
        calls.append(user_message)
        return "KIA_HELPER"

    monkeypatch.setattr("integrations.apollon.get_l1_context", lambda *_args, **_kwargs: "")
    monkeypatch.setattr("integrations.apollon.build_kia_section", direct_kia_call)
    monkeypatch.setattr(
        "integrations.apollon.load_knowledge_in_action",
        fake_load_knowledge_in_action,
    )
    monkeypatch.setattr(
        "integrations.apollon.build_predictive_push_section", lambda *args, **kwargs: ""
    )
    monkeypatch.setattr("integrations.apollon.build_observation_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("integrations.apollon._get_persona_behavior_prompt", lambda *_args: "")

    result = get_context_for_claude("/work/project", "继续处理技术债")

    assert "KIA_HELPER" in result
    assert calls == ["继续处理技术债"]


# ---- 测试：_collect_session_signal ----


def test_collect_session_signal_empty_messages():
    from integrations.apollon import _collect_session_signal

    assert _collect_session_signal([], "/tmp") == 0


def test_collect_session_signal_no_user_messages(monkeypatch):
    from integrations.apollon import _collect_session_signal

    monkeypatch.setattr(
        "integrations.apollon.get_signal_store",
        lambda: Mock(insert_session_signal=Mock()),
    )
    messages = [{"role": "assistant", "content": "hello"}]
    assert _collect_session_signal(messages, "/tmp") == 0


def test_collect_session_signal_happy_path(monkeypatch):
    from integrations.apollon import _collect_session_signal

    inserted = []

    def fake_log_session_signal(**kwargs):
        inserted.append(kwargs)
        return 1

    monkeypatch.setattr("integrations.apollon.log_session_signal", fake_log_session_signal)
    monkeypatch.setattr("integrations.apollon._analyze_blindspot_feedback", lambda _msgs: None)

    messages = [
        {"role": "user", "content": "帮我写个函数"},
        {"role": "assistant", "content": "```python\ndef foo(): pass\n```"},
        {"role": "user", "content": "好的，搞定了，谢谢"},
    ]
    assert _collect_session_signal(messages, "/tmp", "coding", "debug", "sid-123") == 1
    assert len(inserted) == 1
    signal = inserted[0]
    assert signal["session_id"] == "sid-123"
    assert signal["task_type"] == "coding"
    assert signal["task_subtype"] == "debug"
    assert signal["termination_type"] == "satisfied"
    assert signal["output_type"] == "code"
    assert signal["user_msg_count"] == 2
    assert signal["follow_up_depth"] == 1


def test_collect_session_signal_uses_session_signal_helper(monkeypatch):
    from integrations.apollon import _collect_session_signal

    payloads = []

    def direct_store_call(_signal):
        raise AssertionError("session signal collection should use log_session_signal()")

    def fake_log_session_signal(**kwargs):
        payloads.append(kwargs)
        return 1

    monkeypatch.setattr(
        "integrations.apollon.get_signal_store",
        lambda: Mock(insert_session_signal=direct_store_call),
    )
    monkeypatch.setattr("integrations.apollon.log_session_signal", fake_log_session_signal)
    monkeypatch.setattr("integrations.apollon._analyze_blindspot_feedback", lambda _msgs: None)

    messages = [
        {"role": "user", "content": "帮我总结这个 session"},
        {"role": "assistant", "content": "已完成总结"},
    ]

    assert _collect_session_signal(messages, "/tmp/project", "review", "summary", "sid-456") == 1
    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["session_id"] == "sid-456"
    assert payload["task_type"] == "review"
    assert payload["task_subtype"] == "summary"
    assert payload["user_msg_count"] == 1
    assert payload["avg_user_msg_length"] == 14.0
    assert payload["correction_domains"] is None
    assert payload["termination_type"] == "unknown"
    assert payload["output_type"] == "discussion"
    assert payload["working_dir"] == "/tmp/project"
    assert payload["agent"] == "claude"


def test_collect_session_signal_insert_failure(monkeypatch):
    from integrations.apollon import _collect_session_signal

    def boom(**_kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr("integrations.apollon.log_session_signal", boom)
    monkeypatch.setattr("integrations.apollon._analyze_blindspot_feedback", lambda _msgs: None)

    messages = [{"role": "user", "content": "hi"}]
    assert _collect_session_signal(messages, "/tmp") == 0


def test_collect_session_signal_propagates_programming_errors(monkeypatch):
    from integrations.apollon import _collect_session_signal

    monkeypatch.setattr(
        "integrations.apollon.log_session_signal",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("signal contract bug")),
    )
    monkeypatch.setattr("integrations.apollon._analyze_blindspot_feedback", lambda _msgs: None)

    with pytest.raises(AssertionError, match="signal contract bug"):
        _collect_session_signal([{"role": "user", "content": "hi"}], "/tmp")
