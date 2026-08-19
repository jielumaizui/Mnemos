"""
AgentSource 解析测试 — Aider / Gemini CLI / Cursor / Windsurf

覆盖：
- discover_sessions 能发现会话文件
- parse_turns 能正确解析为 Turn 列表
"""

import json
import tempfile
from pathlib import Path


import unittest
from unittest.mock import patch


class TestAiderSource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_discover_sessions_finds_chat_history(self):
        """发现 .aider.chat.history.md"""
        from integrations.sources.aider_source import AiderSource

        source = AiderSource()
        # 在项目目录下创建 aider 历史文件
        project_dir = Path(self.tmpdir.name) / "myproject"
        project_dir.mkdir()
        history_file = project_dir / ".aider.chat.history.md"
        history_file.write_text("# Chat history\n", encoding="utf-8")

        # 临时设置搜索根目录
        import os

        old_env = os.environ.get("AIDER_PROJECT_ROOTS", "")
        os.environ["AIDER_PROJECT_ROOTS"] = str(self.tmpdir.name)
        try:
            sessions = source.discover_sessions()
            self.assertGreaterEqual(len(sessions), 1)
            self.assertEqual(sessions[0].session_id, "myproject")
        finally:
            os.environ["AIDER_PROJECT_ROOTS"] = old_env

    def test_parse_turns_from_markdown(self):
        """解析 Markdown 格式的聊天记录"""
        from integrations.sources.aider_source import AiderSource

        source = AiderSource()
        history_file = Path(self.tmpdir.name) / "chat.md"
        history_file.write_text(
            """#### /message
Hello aider

#### assistant
Hello! How can I help you today?

#### /message
Write a Python function

#### assistant
```python
def hello():
    print("hello")
```
""",
            encoding="utf-8",
        )

        turns = source.parse_turns(history_file)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].user_content, "Hello aider")
        self.assertIn("How can I help", turns[0].assistant_content)
        self.assertEqual(turns[1].user_content, "Write a Python function")
        self.assertIn("def hello()", turns[1].assistant_content)

    def test_parse_turns_empty_file(self):
        """空文件返回空列表"""
        from integrations.sources.aider_source import AiderSource

        source = AiderSource()
        history_file = Path(self.tmpdir.name) / "empty.md"
        history_file.write_text("", encoding="utf-8")
        turns = source.parse_turns(history_file)
        self.assertEqual(len(turns), 0)

    def test_zero_session_inventory_binds_the_exact_search_root(self):
        from core.sync_framework.native_artifact_inventory import (
            build_native_artifact_inventory,
        )
        from integrations.sources.aider_source import AiderSource

        with patch.dict(
            "os.environ",
            {
                "AIDER_PROJECT_ROOTS": self.tmpdir.name,
                "AIDER_CHAT_HISTORY_FILE": "",
            },
            clear=False,
        ):
            inventory = build_native_artifact_inventory([AiderSource()])

        assert inventory.sources[0].source_name == "aider"
        assert inventory.sources[0].session_count == 0
        assert len(inventory.sources[0].root_identity_hashes) == 1

    def test_explicit_history_file_is_discovered_and_root_bound(self):
        from integrations.sources.aider_source import AiderSource

        history = Path(self.tmpdir.name) / "direct" / ".aider.chat.history.md"
        history.parent.mkdir()
        history.write_text("#### /message\nhello\n", encoding="utf-8")
        with patch.dict(
            "os.environ",
            {
                "AIDER_CHAT_HISTORY_FILE": str(history),
                "AIDER_PROJECT_ROOTS": "",
            },
            clear=False,
        ):
            source = AiderSource()
            sessions = source.discover_sessions()
            roots = source.observed_roots()

        assert history in [session.source_path for session in sessions]
        assert history.parent in roots

    def test_missing_explicit_history_file_cannot_be_verified_empty(self):
        from core.sync_framework.native_artifact_inventory import (
            NativeArtifactInventoryError,
            build_native_artifact_inventory,
        )
        from integrations.sources.aider_source import AiderSource

        missing = Path(self.tmpdir.name) / "missing" / ".aider.chat.history.md"
        with patch.dict(
            "os.environ",
            {
                "AIDER_CHAT_HISTORY_FILE": str(missing),
                "AIDER_PROJECT_ROOTS": self.tmpdir.name,
            },
            clear=False,
        ):
            try:
                build_native_artifact_inventory([AiderSource()])
            except NativeArtifactInventoryError as exc:
                assert str(exc) == "native_root_not_detected"
            else:
                raise AssertionError("missing explicit Aider history was accepted")

    def test_directory_configured_as_history_file_cannot_be_verified_empty(self):
        from core.sync_framework.native_artifact_inventory import (
            NativeArtifactInventoryError,
            build_native_artifact_inventory,
        )
        from integrations.sources.aider_source import AiderSource

        invalid = Path(self.tmpdir.name) / "history-directory"
        invalid.mkdir()
        with patch.dict(
            "os.environ",
            {
                "AIDER_CHAT_HISTORY_FILE": str(invalid),
                "AIDER_PROJECT_ROOTS": self.tmpdir.name,
            },
            clear=False,
        ):
            try:
                build_native_artifact_inventory([AiderSource()])
            except NativeArtifactInventoryError as exc:
                assert str(exc) == "native_root_unresolvable"
            else:
                raise AssertionError("directory Aider history was accepted")


class TestGeminiCliSource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_discover_sessions_finds_jsonl(self):
        """发现 Gemini CLI 的 JSONL 会话文件"""
        from integrations.sources.gemini_cli_source import GeminiCliSource

        source = GeminiCliSource()
        # 创建模拟数据目录
        sessions_dir = Path(self.tmpdir.name) / "sessions"
        sessions_dir.mkdir()
        session_file = sessions_dir / "session-1.jsonl"
        session_file.write_text("", encoding="utf-8")

        # patch data_dir
        source._override_data_dir = Path(self.tmpdir.name)
        sessions = source.discover_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "session-1")

    def test_parse_turns_from_jsonl(self):
        """解析 Gemini JSONL 格式"""
        from integrations.sources.gemini_cli_source import GeminiCliSource

        source = GeminiCliSource()
        session_file = Path(self.tmpdir.name) / "test.jsonl"
        lines = [
            json.dumps({"role": "user", "content": "What is AI?"}),
            json.dumps({"role": "assistant", "content": "AI stands for Artificial Intelligence."}),
            json.dumps({"role": "user", "content": "Tell me more"}),
            json.dumps(
                {
                    "role": "model",
                    "content": "Sure, AI encompasses machine learning, deep learning, and more.",
                }
            ),
        ]
        session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        turns = source.parse_turns(session_file)
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0].user_content, "What is AI?")
        self.assertIn("Artificial Intelligence", turns[0].assistant_content)
        self.assertEqual(turns[1].user_content, "Tell me more")
        self.assertIn("machine learning", turns[1].assistant_content)

    def test_parse_turns_with_parts_array(self):
        """解析带 parts 数组的 Gemini 格式"""
        from integrations.sources.gemini_cli_source import GeminiCliSource

        source = GeminiCliSource()
        session_file = Path(self.tmpdir.name) / "test.jsonl"
        lines = [
            json.dumps({"role": "user", "parts": [{"text": "Hello"}]}),
            json.dumps({"role": "model", "parts": [{"text": "Hi there"}]}),
        ]
        session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        turns = source.parse_turns(session_file)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user_content, "Hello")
        self.assertEqual(turns[0].assistant_content, "Hi there")


class TestCursorSource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_discover_sessions_finds_json(self):
        """发现 Cursor 的 JSON 会话文件"""
        from integrations.sources.cursor_source import CursorSource

        source = CursorSource()
        ws_dir = Path(self.tmpdir.name) / "workspaceStorage" / "ws-1"
        ws_dir.mkdir(parents=True)
        chat_file = ws_dir / "chat_history.json"
        chat_file.write_text("[]", encoding="utf-8")

        source._override_data_dir = Path(self.tmpdir.name)
        sessions = source.discover_sessions()
        self.assertGreaterEqual(len(sessions), 1)
        self.assertTrue(any(s.session_id == "ws-1" for s in sessions))

    def test_parse_turns_from_json(self):
        """解析 Cursor JSON 聊天记录"""
        from integrations.sources.cursor_source import CursorSource

        source = CursorSource()
        session_file = Path(self.tmpdir.name) / "chat.json"
        data = [
            {"role": "user", "content": "How do I use React hooks?"},
            {"role": "assistant", "content": "React hooks allow you to use state..."},
        ]
        session_file.write_text(json.dumps(data), encoding="utf-8")

        turns = source.parse_turns(session_file)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user_content, "How do I use React hooks?")
        self.assertIn("React hooks", turns[0].assistant_content)

    def test_experimental_flag_is_reflected_in_source_tags(self):
        """Cursor 实验源应把保真度标记写入默认 tag 契约。"""
        from integrations.sources.cursor_source import CursorSource

        source = CursorSource()

        self.assertIs(source.experimental, True)
        self.assertEqual(source.completeness_capabilities()["source_fidelity"], "experimental")
        self.assertIn("source_fidelity=experimental", source.build_extra_tags(None))

    def test_uninstalled_source_binds_existing_detection_parent(self):
        from core.sync_framework.native_artifact_inventory import (
            build_native_artifact_inventory,
        )
        from integrations.sources.cursor_source import CursorSource

        home = Path(self.tmpdir.name)
        (home / "Library" / "Application Support").mkdir(parents=True)
        with (
            patch("integrations.sources.cursor_source.sys.platform", "darwin"),
            patch("integrations.sources.cursor_source.Path.home", return_value=home),
            patch.dict("os.environ", {"CURSOR_HOME": ""}, clear=False),
        ):
            source = CursorSource()
            assert source.data_dir is None
            inventory = build_native_artifact_inventory([source])

        assert inventory.sources[0].session_count == 0
        assert len(inventory.sources[0].root_identity_hashes) == 1

    def test_missing_explicit_home_cannot_fall_back_to_platform_parent(self):
        from core.sync_framework.native_artifact_inventory import (
            NativeArtifactInventoryError,
            build_native_artifact_inventory,
        )
        from integrations.sources.cursor_source import CursorSource

        home = Path(self.tmpdir.name)
        (home / "Library" / "Application Support").mkdir(parents=True)
        missing = home / "configured-cursor"
        with (
            patch("integrations.sources.cursor_source.sys.platform", "darwin"),
            patch("integrations.sources.cursor_source.Path.home", return_value=home),
            patch.dict("os.environ", {"CURSOR_HOME": str(missing)}, clear=False),
        ):
            try:
                build_native_artifact_inventory([CursorSource()])
            except NativeArtifactInventoryError as exc:
                assert str(exc) == "native_root_not_detected"
            else:
                raise AssertionError("missing explicit Cursor home was accepted")


class TestWindsurfSource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_discover_sessions_finds_json(self):
        """发现 Windsurf 的 JSON 会话文件"""
        from integrations.sources.windsurf_source import WindsurfSource

        source = WindsurfSource()
        ws_dir = Path(self.tmpdir.name) / "workspaceStorage" / "ws-1"
        ws_dir.mkdir(parents=True)
        chat_file = ws_dir / "history.json"
        chat_file.write_text("[]", encoding="utf-8")

        source._override_data_dir = Path(self.tmpdir.name)
        sessions = source.discover_sessions()
        self.assertGreaterEqual(len(sessions), 1)

    def test_parse_turns_from_json(self):
        """解析 Windsurf JSON 聊天记录"""
        from integrations.sources.windsurf_source import WindsurfSource

        source = WindsurfSource()
        session_file = Path(self.tmpdir.name) / "history.json"
        data = [
            {"role": "user", "content": "Explain closures"},
            {"role": "assistant", "content": "A closure is a function..."},
        ]
        session_file.write_text(json.dumps(data), encoding="utf-8")

        turns = source.parse_turns(session_file)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0].user_content, "Explain closures")
        self.assertIn("closure", turns[0].assistant_content)

    def test_experimental_flag_is_reflected_in_source_tags(self):
        """Windsurf 实验源应把保真度标记写入默认 tag 契约。"""
        from integrations.sources.windsurf_source import WindsurfSource

        source = WindsurfSource()

        self.assertIs(source.experimental, True)
        self.assertEqual(source.completeness_capabilities()["source_fidelity"], "experimental")
        self.assertIn("source_fidelity=experimental", source.build_extra_tags(None))

    def test_uninstalled_source_binds_existing_detection_parents(self):
        from core.sync_framework.native_artifact_inventory import (
            build_native_artifact_inventory,
        )
        from integrations.sources.windsurf_source import WindsurfSource

        home = Path(self.tmpdir.name)
        (home / "Library" / "Application Support").mkdir(parents=True)
        with (
            patch("integrations.sources.windsurf_source.sys.platform", "darwin"),
            patch("integrations.sources.windsurf_source.Path.home", return_value=home),
            patch.dict("os.environ", {"WINDSURF_HOME": ""}, clear=False),
        ):
            source = WindsurfSource()
            assert source.data_dir is None
            inventory = build_native_artifact_inventory([source])

        assert inventory.sources[0].session_count == 0
        assert len(inventory.sources[0].root_identity_hashes) == 2

    def test_missing_explicit_home_cannot_fall_back_to_platform_parents(self):
        from core.sync_framework.native_artifact_inventory import (
            NativeArtifactInventoryError,
            build_native_artifact_inventory,
        )
        from integrations.sources.windsurf_source import WindsurfSource

        home = Path(self.tmpdir.name)
        (home / "Library" / "Application Support").mkdir(parents=True)
        missing = home / "configured-windsurf"
        with (
            patch("integrations.sources.windsurf_source.sys.platform", "darwin"),
            patch("integrations.sources.windsurf_source.Path.home", return_value=home),
            patch.dict("os.environ", {"WINDSURF_HOME": str(missing)}, clear=False),
        ):
            try:
                build_native_artifact_inventory([WindsurfSource()])
            except NativeArtifactInventoryError as exc:
                assert str(exc) == "native_root_not_detected"
            else:
                raise AssertionError("missing explicit Windsurf home was accepted")


class TestKimiSource(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_context_file_sort_key_natural_order(self):
        """context_1 < context_2 < context_10 < context.jsonl"""
        from integrations.sources.kimi_source import KimiSource

        source = KimiSource()

        session_dir = Path(self.tmpdir.name) / "session"
        session_dir.mkdir()
        (session_dir / "context.jsonl").write_text("", encoding="utf-8")
        (session_dir / "context_1.jsonl").write_text("", encoding="utf-8")
        (session_dir / "context_2.jsonl").write_text("", encoding="utf-8")
        (session_dir / "context_10.jsonl").write_text("", encoding="utf-8")

        files = sorted(session_dir.glob("context*.jsonl"), key=source._context_file_sort_key)
        names = [f.name for f in files]
        self.assertEqual(
            names, ["context_1.jsonl", "context_2.jsonl", "context_10.jsonl", "context.jsonl"]
        )

    def test_parse_turns_preserve_order(self):
        """多文件合并后 turn_number 单调递增"""
        from integrations.sources.kimi_source import KimiSource

        source = KimiSource()

        session_dir = Path(self.tmpdir.name) / "session"
        session_dir.mkdir()
        # context_1.jsonl: turn 0
        (session_dir / "context_1.jsonl").write_text(
            json.dumps({"role": "user", "content": "hello"})
            + "\n"
            + json.dumps({"role": "assistant", "content": "hi"})
            + "\n",
            encoding="utf-8",
        )
        # context_2.jsonl: turn 1
        (session_dir / "context_2.jsonl").write_text(
            json.dumps({"role": "user", "content": "world"})
            + "\n"
            + json.dumps({"role": "assistant", "content": "earth"})
            + "\n",
            encoding="utf-8",
        )
        # context_10.jsonl: turn 2
        (session_dir / "context_10.jsonl").write_text(
            json.dumps({"role": "user", "content": "foo"})
            + "\n"
            + json.dumps({"role": "assistant", "content": "bar"})
            + "\n",
            encoding="utf-8",
        )

        turns = source.parse_turns(session_dir / "context.jsonl")
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0].user_content, "hello")
        self.assertEqual(turns[1].user_content, "world")
        self.assertEqual(turns[2].user_content, "foo")


if __name__ == "__main__":
    unittest.main()
