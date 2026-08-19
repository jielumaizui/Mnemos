# Crush Integration

Crush uses adapter-based active access plus a local SQLite passive source.

Configuration files:

- MCP config: `~/.config/crush/crush.json`
- Active policy: `~/.config/crush/CRUSH.md` or the configured Crush policy file
- Passive source: `./.crush/crush.db` in the active project, with
  `CRUSH_HOME` / `CRUSH_DATA_DIR`, `~/.crush`, and `~/.config/crush` as
  fallbacks

Passive capture reads the Crush database in read-only mode and reconstructs:

- visible user/assistant text from `messages.parts`
- session tree metadata from `sessions.parent_session_id`
- tool calls from `tool_call` parts
- tool results from `tool_result` parts
- file-context evidence from `read_files`

Full-power evidence requirements:

- `visible_text=true`
- `tool_calls=true`
- `tool_results=true`
- `attachments=true` via `read_files`
- `source_fidelity=full`

Install or repair:

```bash
python3 mnemos_cli.py agent install crush
python3 mnemos_cli.py agent doctor crush
python3 mnemos_cli.py agent kit crush
```

`agent doctor` and `agent kit` read the Crush MCP config and Active Policy
through the shared diagnostics provider. A configured MCP server plus installed
policy makes the active workflow visible even when installation evidence is
reported separately from the Crush binary/database checks.

Troubleshooting:

- If Crush is installed but not detected, confirm either `crush` is in `PATH`
  or `crush.db` exists under the current project's `./.crush` directory.
- If tool evidence is missing, complete one Crush turn that calls a tool and
  rerun `mnemos agent kit crush`.

Official references:

- https://github.com/charmbracelet/crush
- https://charmbracelet-crush.mintlify.app/
