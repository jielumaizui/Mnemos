# Agent Runtime Capability Receipts Design

## Problem

Agent Kit v1 called static installation, policy, MCP schema, and source capability checks `full_power`. That allowed all eight installed hosts to report full power while none had content authorization or a recent runtime proof. MCP `health_check` also used a shallow application-facade implementation instead of the CLI canonical health report.

## Contract

- Static integration facts produce `conformance_ok`; missing installations are N/A.
- Runtime `full_power` requires static conformance, `user_authorized` or `shadow_enabled`, a recent authenticated canonical-health roundtrip, a valid fixed synthetic-safe completeness sample, and a receipt younger than 24 hours.
- MCP and CLI health share one 30-check snapshot and order-sensitive `health_check_ids_hash`.
- The health handshake is valid for five minutes and is bound to the server-derived host principal.
- The probe accepts only fixed sentinel text, one paired `health_check` tool call/result, and exact completeness metadata. Persistent tables never store the sample or user content.
- The canonical v3 receipt payload stores a SHA-256 `runtime_canary_hash` over the fixed sample and canonical health hash. Evaluation recomputes that canonical hash; prior payload generations and different well-formed hashes fail closed.
- Full power additionally requires an independent canonical Raw verifier to find the exact structured `agent_runtime_probe` call and the call-ID-matched server result containing the same receipt ID and canary hash inside one frozen source generation. The persistence owner reruns this bounded read-only verification and does not accept caller-shaped evidence. Visible-text self-signing is not evidence.
- Missing, stale, malformed, unauthorized, or check-set-mismatched evidence fails closed. A new failed probe invalidates the previous success.
- `agent repair` only repairs static integration; it cannot grant authorization or mint runtime evidence.

## Ownership

- `core/agent_kit/report.py`: conformance/runtime aggregation and public Agent Kit v2 schema.
- `core/agent_kit/runtime_receipts.py`: exact probe contract, health handshake, durable receipt/canary validation, freshness, and fail-closed states.
- `core/agent_kit/source_capture_verification.py`: independent frozen-generation Raw denominator and structured runtime-canary verification.
- `core/ops/health_contract.py`: canonical check identity and hash.
- `core/ops/health_check.py`: the canonical snapshot consumed by CLI and MCP; Agent Kit runtime is strict.
- `integrations/agora.py`: binds health observations and probes to the authenticated MCP principal.
- `agent_authorization.db`: authorization plus content-free health/runtime receipt metadata.

## Verification

- Eight-host synthetic-safe capture-to-Raw acceptance, authorization denied, missing/stale health handshake, stale receipt, malformed sample, wrong canonical hash, old check-set, legacy payload, visible-text self-signing, mismatched call ID, bounded JSON decoding, missing Raw call/result, caller-shaped evidence rejection, and successful same-generation canary tests.
- MCP principal binding and MCP/CLI canonical health parity tests.
- Agent repair regression ensures runtime-unverified hosts are not reinstalled.
- Full Quick: `5577 passed, 15 subtests passed`.
- `python3 scripts/run_local_gates.py`: all gates passed.
- Production migration backup: `~/.mnemos/backups/root011-agent-runtime-20260711-135733`; source and backup `PRAGMA integrity_check=ok`; both new tables started with zero rows.
