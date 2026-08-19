# Kiro Integration

Kiro uses MCP-only active access plus passive session discovery.

`mnemos agent install` writes:

- MCP server config: `~/.kiro/settings/mcp.json`
- Active policy block: `~/.kiro/MNEMOS_ACTIVE.md`

The MCP server entry points to the local Mnemos CLI:

```bash
/path/to/mnemos/.venv/bin/python /path/to/mnemos/mnemos_cli.py mcp serve
```

The Kiro CLI registry stores the launch timeout in milliseconds. Mnemos writes
`60000` so a healthy cold local health handshake is not mistaken for a failed
MCP start.

Passive capture reads Kiro CLI JSONL sessions from `~/.kiro/sessions/cli/*.jsonl`.
It captures text, `toolUse`, `toolResult`, thinking/reasoning blocks, and
file/media attachment blocks when the local Kiro session file exposes them.
Kiro's CLI setting `chat.showThinking` controls whether thinking is visible in
the host UI/session surface; when Kiro hides it, Mnemos cannot invent that
reasoning evidence.

To repair the integration:

```bash
mnemos doctor repair kiro
mnemos agent kit kiro
```

Expected full-power source capabilities:

- `visible_text=true`
- `tool_calls=true`
- `tool_results=true`
- `reasoning=true`
- `attachments=available`
- `source_fidelity=full`

Official references:

- https://kiro.dev/docs/cli/reference/settings/
- https://kiro.dev/docs/cli/experimental/thinking/
