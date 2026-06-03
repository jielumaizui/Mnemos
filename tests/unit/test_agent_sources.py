"""
AgentSource 解析测试 — Aider / Gemini CLI / Cursor / Windsurf

覆盖：
- discover_sessions 能发现会话文件
- parse_turns 能正确解析为 Turn 列表
"""

import sys
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest


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
        history_file.write_text("""#### /message
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
""", encoding="utf-8")

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
            json.dumps({"role": "model", "content": "Sure, AI encompasses machine learning, deep learning, and more."}),
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
        self.assertEqual(names, ["context_1.jsonl", "context_2.jsonl", "context_10.jsonl", "context.jsonl"])

    def test_parse_turns_preserve_order(self):
        """多文件合并后 turn_number 单调递增"""
        from integrations.sources.kimi_source import KimiSource
        source = KimiSource()

        session_dir = Path(self.tmpdir.name) / "session"
        session_dir.mkdir()
        # context_1.jsonl: turn 0
        (session_dir / "context_1.jsonl").write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n" +
            json.dumps({"role": "assistant", "content": "hi"}) + "\n",
            encoding="utf-8"
        )
        # context_2.jsonl: turn 1
        (session_dir / "context_2.jsonl").write_text(
            json.dumps({"role": "user", "content": "world"}) + "\n" +
            json.dumps({"role": "assistant", "content": "earth"}) + "\n",
            encoding="utf-8"
        )
        # context_10.jsonl: turn 2
        (session_dir / "context_10.jsonl").write_text(
            json.dumps({"role": "user", "content": "foo"}) + "\n" +
            json.dumps({"role": "assistant", "content": "bar"}) + "\n",
            encoding="utf-8"
        )

        turns = source.parse_turns(session_dir / "context.jsonl")
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0].user_content, "hello")
        self.assertEqual(turns[1].user_content, "world")
        self.assertEqual(turns[2].user_content, "foo")


if __name__ == "__main__":
    unittest.main()
