# Agent Integration Guides

Mnemos currently uses three integration paths:

- Adapter-based active access for agents that have runtime adapters, currently including Claude, Kimi, and Crush.
- MCP-only active access for Codex, Hermes, Kiro, OpenCode, and OpenClaw.
- Passive local session sources under `integrations/sources/`, including Kiro.

`mnemos setup` and `mnemos agent install` install both adapter hooks and MCP-only active access by default. `scripts/auto_setup.py` remains a compatibility wrapper for first deployment, so new automation should prefer `mnemos setup` and only call the script for advanced/debug flows. When local config is broken, run `mnemos doctor repair [agent]` or `mnemos doctor repair-all --json` to reinstall the static active pieces. Repair does not grant content access or create a runtime receipt.

For MCP-only targets, `hooks_installed=false` is expected. MCP config, Active Policy, passive source fidelity, and cognitive evidence determine static conformance; they do not by themselves prove runtime full power.

Agent Kit currently checks these targets: `codex`, `claude`, `hermes`, `opencode`, `openclaw`, `crush`, `kiro`, and `kimi`.

The executable acceptance matrix for these targets lives in
`tests/fixtures/agent_acceptance_samples/manifest.json`; the field contracts
are documented under `docs/acceptance/` and enforced by
`python3 scripts/verify_acceptance_contracts.py`.

Users do not need all targets installed. `mnemos agent kit` treats missing
agents as `not_installed`/N/A. Installed targets first pass static conformance:
active workflow access, Mnemos MCP, Active Policy, a full-fidelity passive
source, and the required cognitive evidence declarations. Runtime `full_power`
then requires explicit content authorization, a recent authenticated canonical
health roundtrip, and the fixed synthetic-safe sample returned under
`runtime_probe_contract`. Missing, stale, malformed, unauthorized, or check-set
mismatched receipts fail closed; the receipt stores no sample or user content.
Its v3 payload does store a content-free `runtime_canary_hash`. After the host
transcript reaches canonical Raw, the independent attestor must find the exact
structured probe call and its server-generated receipt result in the same
frozen source generation with the same native tool-call ID. The receipt writer
reruns that bounded read-only Raw/cursor verification and never persists a
caller-supplied verdict. Visible-text copies, unrelated tool results, forged
well-shaped hashes, and prior v1/v2 receipts cannot promote a host to full power.

Full-power evidence requirements:

- All installed targets: `visible_text`, `tool_calls`, `tool_results`, and
  `source_fidelity=full`.
- Codex, Claude, Hermes, OpenCode, Kiro, and Kimi: reasoning/thinking evidence
  if the host exposes it. Private encrypted reasoning is recorded only as
  summary/metadata evidence.
- Codex, Crush, Kiro, and Kimi: attachments, media, or file-context evidence
  where the host exposes it.

Memory, transcript, and dedupe contract:

- Host-agent memory switches are treated as prompt-context controls, not as the
  Mnemos passive capture source. Turning host memory on can change what the
  model sees and therefore what appears in future replies; turning it off can
  remove that context from future replies. In both cases Mnemos still captures
  the local session transcript/DB/trajectory written by the host.
- Every source must declare `memory_scope`, `host_memory_default`,
  `host_memory_effect`, `transcript_kind`, and `compression` in
  `source_capabilities`, so `mnemos agent kit` can report whether the source is
  native raw transcript, SQLite rows, JSONL events, trajectory artifacts, or a
  fallback/derived corpus.
- Sync uses `canonical_session_id + turn_number + content_hash` for framework
  dedupe. Source-specific aliases and path variants are stored as metadata/tags,
  so the same conversation discovered through multiple files or directories is
  merged into one canonical session instead of being ingested twice.
- Split sessions must be reassembled in the source parser before SyncEngine:
  e.g. Kimi `context*.jsonl` plus `agents/main/wire.jsonl`, OpenCode/Crush
  SQLite message+part tables, Kiro JSONL plus related state files, and OpenClaw
  trajectory artifacts before legacy corpus fallback.

Current MCP-only guides:

- [Codex](codex.md)
- [Crush](crush.md)
- [Hermes](hermes.md)
- [Kiro](kiro.md)
- [Kimi](kimi.md)
- [OpenCode](opencode.md)
- [OpenClaw](openclaw.md)

External note-service bridges are not required. These agents should use the current source adapters directly.
