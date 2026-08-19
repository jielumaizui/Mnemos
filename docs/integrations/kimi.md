# Kimi Integration

Kimi uses adapter-based active access plus passive session discovery.

Configuration files:

- Native config: `~/.kimi/config.toml` or the current Kimi Code home.
- Active policy: `~/.kimi/MNEMOS_ACTIVE.md` when using the legacy Kimi home.
- Passive source candidates: `KIMI_CODE_HOME`, `KIMI_HOME`, `~/.kimi-code`,
  and `~/.kimi`.

Passive capture supports both legacy Kimi session archives and newer Kimi Code
wire logs:

- Legacy: `~/.kimi/sessions/**/context.jsonl`
- Wire-only: `~/.kimi-code/**/wire.jsonl` or `~/.kimi/**/wire.jsonl`

Full-power evidence requirements:

- visible conversation text
- tool calls and tool results
- `think` / thinking content when the host exposes it
- file, image, media, or attachment blocks
- full-fidelity local source files

Install or repair:

```bash
python3 mnemos_cli.py agent install kimi
python3 mnemos_cli.py agent doctor kimi
python3 mnemos_cli.py agent kit kimi
```

Troubleshooting:

- If Kimi is installed but not detected, confirm either `kimi`/`kimi-code` is in
  `PATH`, `~/.kimi-code` exists, or `~/.kimi/sessions` exists.
- If passive history is missing, start Kimi once and complete a short turn so
  it writes `context.jsonl` or `wire.jsonl`.
- If reasoning is missing, check whether the selected Kimi mode/model exposes
  thinking in the local session files.

Official references:

- https://platform.kimi.ai/docs/guide/kimi-cli-support
- https://www.kimi.com/code/docs/kimi-code-cli/configuration/data-locations.html
