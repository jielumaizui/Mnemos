# OpenClaw Integration

OpenClaw uses MCP-only active access plus passive session discovery.

Mnemos reads OpenClaw's native trajectory files first and falls back to the
older daily corpus only when no native trajectory exists.

Configuration files:

- MCP config: `~/.openclaw/openclaw.json`
- Active policy: `~/.openclaw/MNEMOS_ACTIVE.md`
- Full-fidelity passive source: `~/.openclaw/agents/*/sessions/*.trajectory.jsonl`
- Legacy fallback: `~/.openclaw/workspace/memory/.dreams/session-corpus/*.txt`

Install or repair:

```bash
python3 mnemos_cli.py agent install openclaw
python3 mnemos_cli.py agent doctor openclaw
```

Expected status:

- `mcp✓`
- `policy✓`
- `[mcp-only]`
- `conformance_ok=true` when native trajectory files and static integration requirements pass
- `status=full_power` only after content authorization plus a recent canonical health/runtime-probe receipt

Troubleshooting:

- If MCP is missing, rerun `agent install openclaw`.
- If policy is missing, ensure `~/.openclaw/MNEMOS_ACTIVE.md` exists and contains the Mnemos active policy block.
- If passive history is missing, confirm OpenClaw is writing trajectory files under `~/.openclaw/agents/*/sessions/`.
