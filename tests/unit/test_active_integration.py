import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestActiveIntegrationHelpers(unittest.TestCase):
    def setUp(self):
        from core.agent_kit.authorization import (
            AgentAuthorizationStore,
            InMemoryMCPLaunchCredentialStore,
        )

        self._temp_dir = tempfile.TemporaryDirectory()
        self._authorization_store = AgentAuthorizationStore(
            Path(self._temp_dir.name) / "agent_authorization.db"
        )
        self._launch_secrets = InMemoryMCPLaunchCredentialStore()
        self._authorization_store_patch = patch(
            "integrations.active.AgentAuthorizationStore",
            return_value=self._authorization_store,
        )
        self._launch_store_patch = patch(
            "integrations.active.MCPLaunchCredentialStore",
            return_value=self._launch_secrets,
        )
        self._authorization_store_patch.start()
        self._launch_store_patch.start()

    def tearDown(self):
        self._launch_store_patch.stop()
        self._authorization_store_patch.stop()
        self._temp_dir.cleanup()

    def test_kimi_hook_command_uses_current_interpreter(self):
        from integrations.active import _kimi_hook_command

        command = _kimi_hook_command("/tmp/mnemos_wrapper.py", "--session-start")

        self.assertIn(sys.executable, command)
        self.assertFalse(command.startswith("python "))

    def test_codex_toml_upsert_replaces_existing_mnemos_server(self):
        from integrations.active import codex_mcp_configured, upsert_codex_mcp_server

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text(
                '[mcp_servers.mnemos]\ncommand = "old"\nargs = ["old.py"]\n\n[desktop]\nfollowUpQueueMode = "steer"\n',  # noqa: E501
                encoding="utf-8",
            )

            self.assertTrue(upsert_codex_mcp_server(path))
            text = path.read_text(encoding="utf-8")

            self.assertEqual(text.count("[mcp_servers.mnemos]"), 1)
            self.assertIn("mnemos_cli.py", text)
            self.assertIn("[desktop]", text)
            self.assertTrue(codex_mcp_configured(path))

    def test_kimi_hooks_upsert_preserves_other_hooks(self):
        from integrations.active import _kimi_hook_command, kimi_hooks_configured, upsert_kimi_hooks

        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.toml"
            wrapper = Path(td) / "mnemos_wrapper.py"
            wrapper.write_text("from integrations.active_bridge import main\n", encoding="utf-8")
            config.write_text(
                'model = "moonshot"\n\nhooks = [\n    { command = "echo ok", event = "SessionStart" },\n]\n',  # noqa: E501
                encoding="utf-8",
            )

            self.assertTrue(upsert_kimi_hooks(config, wrapper))
            text = config.read_text(encoding="utf-8")

            self.assertIn('command = "echo ok"', text)
            self.assertIn(_kimi_hook_command(str(wrapper), "--session-start"), text)
            self.assertIn(_kimi_hook_command(str(wrapper), "--session-end"), text)
            self.assertTrue(kimi_hooks_configured(config, wrapper))

    def test_kimi_hooks_configured_ignores_python_path_drift(self):
        from integrations.active import kimi_hooks_configured

        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.toml"
            wrapper = Path(td) / "mnemos_wrapper.py"
            wrapper.write_text("from integrations.active_bridge import main\n", encoding="utf-8")
            config.write_text(
                "[[hooks]]\n"
                'event = "SessionStart"\n'
                f'command = "/old/venv/bin/python {wrapper} --session-start"\n'
                "\n"
                "[[hooks]]\n"
                'event = "SessionEnd"\n'
                f'command = "/old/venv/bin/python {wrapper} --session-end"\n',
                encoding="utf-8",
            )

            self.assertTrue(kimi_hooks_configured(config, wrapper))

    def test_kimi_hooks_upsert_migrates_legacy_and_table_array_conflict(self):
        from integrations.active import _kimi_hook_command, kimi_hooks_configured, upsert_kimi_hooks

        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.toml"
            wrapper = Path(td) / "mnemos_wrapper.py"
            wrapper.write_text("from integrations.active_bridge import main\n", encoding="utf-8")
            # Simulate the real-world conflict: both [[hooks]] and hooks = [...]
            config.write_text(
                "[[hooks]]\n"
                'event = "SessionStart"\n'
                f'command = "python3 {wrapper} --session-start"\n'
                "\n"
                "[[hooks]]\n"
                'event = "SessionEnd"\n'
                f'command = "python3 {wrapper} --session-end"\n'
                "\n"
                "hooks = [\n"
                f'    {{ command = "python3 {wrapper} --session-start", event = "SessionStart" }},\n'  # noqa: E501
                f'    {{ command = "python3 {wrapper} --session-end", event = "SessionEnd" }},\n'
                "]\n",
                encoding="utf-8",
            )

            self.assertTrue(upsert_kimi_hooks(config, wrapper))
            text = config.read_text(encoding="utf-8")

            # Legacy ``hooks = [...]`` array must be removed.
            self.assertNotIn("hooks = [", text)
            # Native [[hooks]] entries must remain.
            self.assertEqual(text.count("[[hooks]]"), 2)
            self.assertIn(_kimi_hook_command(str(wrapper), "--session-start"), text)
            self.assertIn(_kimi_hook_command(str(wrapper), "--session-end"), text)
            self.assertNotIn(
                f'command = "python3 {wrapper} --session-start"',
                text,
            )
            self.assertNotIn(
                f'command = "python3 {wrapper} --session-end"',
                text,
            )
            self.assertTrue(kimi_hooks_configured(config, wrapper))

            # Idempotent: running again does not duplicate hooks.
            self.assertTrue(upsert_kimi_hooks(config, wrapper))
            text2 = config.read_text(encoding="utf-8")
            self.assertEqual(text2.count("[[hooks]]"), 2)

    def test_json_mcp_upsert_uses_current_mnemos_cli(self):
        from integrations.active import json_mcp_configured, upsert_json_mcp_server

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mcp.json"
            self.assertTrue(upsert_json_mcp_server(path))
            data = json.loads(path.read_text(encoding="utf-8"))

            spec = data["mcpServers"]["mnemos"]
            self.assertIn("mnemos_cli.py", spec["args"][0])
            self.assertEqual(spec["args"][1:], ["mcp", "serve"])
            self.assertTrue(json_mcp_configured(path))

    def test_kiro_mcp_upsert_uses_actual_cli_registry_and_timeout(self):
        from integrations.active import (
            KIRO_MCP_TIMEOUT_MS,
            kiro_mcp_configured,
            upsert_kiro_mcp_server,
        )

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings" / "mcp.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")

            self.assertTrue(upsert_kiro_mcp_server(path))
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(data["theme"], "dark")
            spec = data["mcpServers"]["mnemos"]
            self.assertIn("mnemos_cli.py", spec["args"][0])
            self.assertEqual(spec["timeout"], KIRO_MCP_TIMEOUT_MS)
            self.assertTrue(kiro_mcp_configured(path))

    def test_opencode_config_upsert_writes_mcp_and_instructions(self):
        from integrations.active import (
            opencode_mcp_configured,
            opencode_policy_configured,
            upsert_opencode_config,
        )

        with tempfile.TemporaryDirectory() as td, patch("pathlib.Path.home", return_value=Path(td)):
            path = Path(td) / "opencode.json"
            path.write_text(
                '{\n  // keep user config\n  "theme": "dark",\n}\n',
                encoding="utf-8",
            )

            self.assertTrue(upsert_opencode_config(path))
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(data["theme"], "dark")
            self.assertEqual(data["mcp"]["mnemos"]["type"], "local")
            self.assertIn("mnemos_cli.py", data["mcp"]["mnemos"]["command"][1])
            self.assertEqual(data["mcp"]["mnemos"]["timeout"], 60000)
            self.assertIn("instructions", data)
            self.assertTrue(opencode_mcp_configured(path))
            self.assertTrue(opencode_policy_configured(path))

    def test_opencode_config_upsert_removes_stale_mnemos_policy_path(self):
        from integrations.active import upsert_opencode_config

        with tempfile.TemporaryDirectory() as td, patch("pathlib.Path.home", return_value=Path(td)):
            path = Path(td) / "opencode.json"
            stale_policy = Path(td) / "missing" / "MNEMOS_ACTIVE.md"
            user_instruction = Path(td) / "user.md"
            user_instruction.write_text("keep", encoding="utf-8")
            path.write_text(
                json.dumps(
                    {
                        "instructions": [
                            str(stale_policy),
                            str(user_instruction),
                            "plain user instruction",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(upsert_opencode_config(path))
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertNotIn(str(stale_policy), data["instructions"])
            self.assertIn(str(user_instruction), data["instructions"])
            self.assertIn("plain user instruction", data["instructions"])
            self.assertTrue(
                any(
                    str(item).endswith("active_policy/MNEMOS_ACTIVE.md")
                    for item in data["instructions"]
                )
            )

    def test_marked_policy_block_is_idempotent(self):
        from integrations.active import (
            active_policy_text,
            marked_block_installed,
            upsert_marked_block,
        )

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "AGENTS.md"
            path.write_text("# Existing\n", encoding="utf-8")

            self.assertTrue(upsert_marked_block(path, active_policy_text("codex")))
            self.assertTrue(upsert_marked_block(path, active_policy_text("codex")))
            text = path.read_text(encoding="utf-8")

            self.assertEqual(text.count("BEGIN MNEMOS_ACTIVE_POLICY"), 1)
            self.assertTrue(marked_block_installed(path))

    def test_active_policy_drives_user_visible_application_layer(self):
        from integrations.active import (
            _build_lightweight_preflight,
            active_policy_text,
            render_active_context,
        )

        policy = active_policy_text("codex")

        self.assertIn("check_pending_recaps", policy)
        self.assertIn("predictive_push", policy)
        self.assertIn("If Mnemos materially changes your plan", policy)
        self.assertIn("do not spam", policy.lower())

        with patch(
            "integrations.active._run_preflight_with_timeout",
            return_value="## KIA Checklist\n\n- ok",
        ):
            context = render_active_context("codex", "/tmp/project", "继续修复")

        self.assertIn("check_pending_recaps", context)
        self.assertIn("predictive_push", context)

        preflight = _build_lightweight_preflight("codex", "/tmp/project", "继续修复")
        self.assertIn("## Active Tooling", preflight)
        self.assertIn("check_pending_recaps", preflight)
        self.assertIn("predictive_push", preflight)
        self.assertIn("## User-Visible Behavior", preflight)
        self.assertIn("urgent or force-open", preflight)


class TestCrushActiveIntegration(unittest.TestCase):
    def test_crush_mcp_upsert_uses_crush_schema(self):
        from core.agent_kit.authorization import (
            AgentAuthorizationStore,
            InMemoryMCPLaunchCredentialStore,
        )
        from integrations.active import crush_mcp_configured, upsert_crush_mcp_server

        with tempfile.TemporaryDirectory() as td:
            store = AgentAuthorizationStore(Path(td) / "agent_authorization.db")
            secrets = InMemoryMCPLaunchCredentialStore()
            path = Path(td) / "crush.json"
            path.write_text(
                '{\n  "$schema": "https://charm.land/crush.json",\n  "providers": {}\n}\n',
                encoding="utf-8",
            )

            self.assertTrue(
                upsert_crush_mcp_server(
                    path,
                    authorization_store=store,
                    credential_store=secrets,
                )
            )
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(data["$schema"], "https://charm.land/crush.json")
            self.assertEqual(data["mcp"]["mnemos"]["type"], "stdio")
            self.assertIn("mnemos_cli.py", data["mcp"]["mnemos"]["args"][0])
            self.assertTrue(
                crush_mcp_configured(
                    path,
                    authorization_store=store,
                    credential_store=secrets,
                )
            )

    @patch("pathlib.Path.home")
    def test_crush_policy_install_writes_crush_md(self, mock_home):
        from integrations.active import install_agent_policy, is_agent_policy_installed

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            mock_home.return_value = td_path
            self.assertTrue(install_agent_policy("crush"))

            crush_md = td_path / ".config" / "crush" / "CRUSH.md"
            self.assertTrue(crush_md.exists())
            self.assertIn("MNEMOS_ACTIVE_POLICY", crush_md.read_text(encoding="utf-8"))
            self.assertTrue(is_agent_policy_installed("crush"))


class TestActiveBridgeKimiFallback(unittest.TestCase):
    def test_normalize_kimi_content_handles_string_and_blocks(self):
        from integrations.active_bridge import _normalize_kimi_content

        self.assertEqual(_normalize_kimi_content("hello"), "hello")
        self.assertEqual(
            _normalize_kimi_content(
                [{"type": "text", "text": "a"}, {"type": "think", "think": "b"}]
            ),
            "a\nb",
        )
        self.assertEqual(_normalize_kimi_content(["x", "y"]), "x\ny")

    @patch("pathlib.Path.home")
    def test_kimi_fallback_reads_latest_context_jsonl(self, mock_home):
        from integrations.active_bridge import _read_kimi_fallback_session

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            sessions_dir = td_path / ".kimi" / "sessions" / "ws1" / "sess-123"
            sessions_dir.mkdir(parents=True)
            jsonl = sessions_dir / "context_1.jsonl"
            jsonl.write_text(
                json.dumps({"role": "user", "content": "hi"})
                + "\n"
                + json.dumps({"role": "_checkpoint", "id": 1})
                + "\n"
                + json.dumps({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
                + "\n",
                encoding="utf-8",
            )
            mock_home.return_value = td_path

            sid, messages = _read_kimi_fallback_session()
            self.assertEqual(sid, "sess-123")
            self.assertEqual(len(messages), 2)
            self.assertEqual(messages[0], {"role": "user", "content": "hi"})
            self.assertEqual(messages[1], {"role": "assistant", "content": "ok"})

    @patch("integrations.active_bridge._read_kimi_fallback_session")
    @patch("integrations.active_bridge._enqueue_session")
    def test_session_end_uses_kimi_fallback_when_no_env_messages(self, mock_enqueue, mock_fallback):
        from integrations.active_bridge import main

        mock_fallback.return_value = ("sess-fallback", [{"role": "user", "content": "u"}])
        mock_enqueue.return_value = "queued-sid"

        with patch("sys.argv", ["active_bridge", "kimi", "--session-end", "--working-dir", "/tmp"]):
            main(default_agent="kimi")

        mock_fallback.assert_called_once()
        mock_enqueue.assert_called_once_with(
            "kimi", "/tmp", [{"role": "user", "content": "u"}], session_id="sess-fallback"
        )


if __name__ == "__main__":
    unittest.main()
