# OpenCode Integration

OpenCode uses MCP-only active access plus passive session discovery.

Configuration files:

- MCP and policy config: `~/.config/opencode/opencode.json`
- Passive source candidates: OpenCode local data directory discovered by `integrations/sources/opencode_source.py`

Install or repair:

```bash
python3 mnemos_cli.py agent install opencode
python3 mnemos_cli.py agent doctor opencode
```

Expected status:

- `mcp✓`
- `policy✓`
- `[mcp-only]`

Troubleshooting:

- If MCP is missing, rerun `agent install opencode`.
- If policy is missing, inspect `~/.config/opencode/opencode.json`.
- If passive history is missing, run `python3 mnemos_cli.py agent detect` and check the passive source path.

