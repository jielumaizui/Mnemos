# Codex Integration

Codex uses MCP-only active access plus passive session discovery.

Full-power requirements:

- Active: Mnemos MCP server configured in `~/.codex/config.toml`.
- Policy: Mnemos Active Policy block present in `~/.codex/AGENTS.md`.
- Passive: rollout JSONL under `~/.codex/sessions` or `~/.config/codex/sessions`.
- Evidence: visible text, `function_call`, `function_call_output`, reasoning
  summary/encrypted metadata, and file/media attachment blocks when present.

Codex Memory is an active context feature, not a passive rollout parser
replacement. Enabling `[features] memories = true` lets Codex use persistent
memory across sessions, but Mnemos still validates the local rollout evidence
chain separately.

Configuration files:

- MCP config: `~/.codex/config.toml`
- Active policy: `~/.codex/AGENTS.md`
- Passive source candidates: `~/.codex`, `~/.config/codex`

Install or repair:

```bash
python3 mnemos_cli.py agent install codex
python3 mnemos_cli.py agent doctor codex
```

Expected status:

- `mcp✓`
- `policy✓`
- `[mcp-only]`
- `tool_calls=true`
- `tool_results=true`
- `reasoning=summary_or_encrypted`
- `attachments=available`

Troubleshooting:

- If MCP is missing, rerun `agent install codex`.
- If policy is missing, ensure `~/.codex/AGENTS.md` contains the Mnemos active policy block.
- If passive history is missing, confirm Codex is writing sessions under one of the passive source candidates.

Official reference:

- https://developers.openai.com/codex/memories
