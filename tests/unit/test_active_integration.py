import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestActiveIntegrationHelpers(unittest.TestCase):
    def test_codex_toml_upsert_replaces_existing_mnemos_server(self):
        from integrations.active import codex_mcp_configured, upsert_codex_mcp_server

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            path.write_text(
                '[mcp_servers.mnemos]\ncommand = "old"\nargs = ["old.py"]\n\n[desktop]\nfollowUpQueueMode = "steer"\n',
                encoding="utf-8",
            )

            self.assertTrue(upsert_codex_mcp_server(path))
            text = path.read_text(encoding="utf-8")

            self.assertEqual(text.count("[mcp_servers.mnemos]"), 1)
            self.assertIn("mnemos_cli.py", text)
            self.assertIn("[desktop]", text)
            self.assertTrue(codex_mcp_configured(path))

    def test_kimi_hooks_upsert_preserves_other_hooks(self):
        from integrations.active import kimi_hooks_configured, upsert_kimi_hooks

        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "config.toml"
            wrapper = Path(td) / "mnemos_wrapper.py"
            wrapper.write_text("from integrations.active_bridge import main\n", encoding="utf-8")
            config.write_text(
                'model = "moonshot"\n\nhooks = [\n    { command = "echo ok", event = "SessionStart" },\n]\n',
                encoding="utf-8",
            )

            self.assertTrue(upsert_kimi_hooks(config, wrapper))
            text = config.read_text(encoding="utf-8")

            self.assertIn('command = "echo ok"', text)
            self.assertIn(f"python3 {wrapper} --session-start", text)
            self.assertIn(f"python3 {wrapper} --session-end", text)
            self.assertTrue(kimi_hooks_configured(config, wrapper))

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
            self.assertIn("instructions", data)
            self.assertTrue(opencode_mcp_configured(path))
            self.assertTrue(opencode_policy_configured(path))

    def test_marked_policy_block_is_idempotent(self):
        from integrations.active import active_policy_text, marked_block_installed, upsert_marked_block

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "AGENTS.md"
            path.write_text("# Existing\n", encoding="utf-8")

            self.assertTrue(upsert_marked_block(path, active_policy_text("codex")))
            self.assertTrue(upsert_marked_block(path, active_policy_text("codex")))
            text = path.read_text(encoding="utf-8")

            self.assertEqual(text.count("BEGIN MNEMOS_ACTIVE_POLICY"), 1)
            self.assertTrue(marked_block_installed(path))


if __name__ == "__main__":
    unittest.main()
