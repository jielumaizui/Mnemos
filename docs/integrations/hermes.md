# Hermes Integration

Hermes uses MCP-only active access plus passive session discovery.

Configuration files:

- MCP config: `~/.hermes/config.yaml`
- Active policy: `~/.hermes/MNEMOS_ACTIVE.md`
- Passive source candidates: `~/.hermes/sessions`, `~/.hermes`

Install or repair:

```bash
python3 mnemos_cli.py agent install hermes
python3 mnemos_cli.py agent doctor hermes
```

Expected status:

- `mcp✓`
- `policy✓`
- `[mcp-only]`

Troubleshooting:

- If MCP is missing, rerun `agent install hermes`.
- If policy is missing, ensure `~/.hermes/MNEMOS_ACTIVE.md` exists and contains the Mnemos active policy block.
- If passive history is missing, confirm Hermes writes JSON sessions under `~/.hermes/sessions`.

