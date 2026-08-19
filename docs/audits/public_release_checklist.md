# Public Release Checklist

This checklist converts the generic-version audit notes into current Mnemos
release checks. Items marked obsolete are intentionally not restored.

## Storage Backend Release Checks

- Current storage backend: `core/sync_framework/storage_backend.py`, `integrations/backends.py`.
- Public config examples: `config/config.example.json`, `config/config.example.yaml`,
  and `config/.env.example`; regenerate them with `python3 scripts/generate_config_examples.py`
  and gate releases with `python3 scripts/verify_config_examples.py --strict` so public
  samples stay at 100% DEFAULT_CONFIG/env coverage.
- Obsolete: generic external note-service sync wrappers and any retired HTTP token flow.
- Check: public docs and runtime config must not require an external note-service server, port, or token.
  Top-level `memos` config is a stale key removed by
  `mnemos migrate apply config.stale_keys.v1 --json`; `mnemos doctor config --strict --json`
  must report no `legacy.config_stale_keys` and `security.secret_inventory.evidence.plaintext_count=0`
  before release. Secret inventory findings may include paths and lengths, never token values.
- SQLite disk budget health: `mnemos health --json` must include `mnemos.sqlite_disk_budget.v1`
  through `checks.sqlite_disk_budget`. Release readiness requires no over-budget `.db-wal`, Mnemos temp,
  snapshot, or `raw_events.db` findings. WAL checkpoint and stale temp cleanup may be repaired with
  `python3 scripts/repair_sqlite_disk_budget.py --apply --wal --temp`; snapshot pruning and raw-event
  deletion require explicit user confirmation and must not be automated.
- Shareable diagnostics: default `mnemos doctor` text, `health --json`, `doctor config --strict --json`,
  `scripts/verify_installation.py --json`, `mnemos_cli.py distill status`, and
  `scripts/e2e_probe.py --dry-run --no-api` must not contain
  real API URLs, local absolute paths, or unredacted `env:` / `keyring:` / `keyref:` identifiers.
  Raw values are allowed only for private local debugging through `--unsafe-debug` / `--show-paths`.
- Keyring/env fallback: `python3 mnemos_cli.py secrets doctor --json` must emit
  `mnemos.keyring_doctor.v1` with `secret_inventory_plaintext_count=0`. A release can ship with
  `keyring.available=false` only when the deployment explicitly sets
  `security.accept_env_secret_fallback=true`; otherwise install/authorize the active Python
  keyring backend or migrate model secrets to `keyring:` / `keyref:` references.
- Repository sensitive literals: `python3 scripts/audit_repo_sensitive_literals.py --strict`
  must pass before release. It scans tracked and non-ignored untracked text so source, tests,
  docs, and newly created files cannot carry copyable provider-shaped fake keys, local home
  paths, or plaintext credential literals. Redaction fixtures should use runtime concatenation
  or `DUMMY_CREDENTIAL_*` sentinel values.
- Release privacy/security gate: `python3 scripts/audit_release_privacy_security.py --strict --json`
  must pass before release. It aggregates strict security audit, strict config doctor, health
  security/privacy findings, docs sensitive audit, repo sensitive literal audit, and health/config/
  distill/E2E diagnostic
  redaction scan into `mnemos.release_privacy_security.v1`. Releases are blocked by non-empty
  `blocking_findings`; agents must follow `repair_actions` and re-run the gate instead of treating
  individual green subcommands as sufficient.
  The nested security report must be `mnemos.security_audit.v2`, pass its canonical validator,
  preserve typed blocking/warning findings, and satisfy `ok == (blocking_count == 0)` together with
  a matching process exit code. An `errors` list, count, status, or return-code contradiction is
  itself release-blocking.
- Relation evidence schema gate: `python3 scripts/audit_schema_registry.py --strict --json`
  must show exactly one production DDL owner and a canonical registered columns/defaults/FK/index
  hash for every initialized `knowledge_graph.db`. Migration-required, unknown, missing-index,
  damaged-registry, NULL/blank evidence type, or hash drift blocks release; run the explicit
  backed-up reconciliation instead of restoring constructor-local `IF NOT EXISTS` DDL.

## Agent Ingestion

- Current source registry: `core/sync_framework/registry.py`.
- Current source adapters: `integrations/sources/`.
- One-shot migration/backfill: `mnemos sync backfill`.
- Obsolete: old live sync modules that write to an external note service directly.
- Check: import/migration writes through `SyncEngine` or `CaptureService`, not external note-service APIs.

## Watchers And Heartbeat

- Current trigger framework: `core/sync_framework/triggers.py`.
- Stat-only watcher: `core/sync_framework/file_watcher.py`.
- Agent path watcher: `core/sync_framework/agent_path_watcher.py`.
- Heartbeat JSON: `daemon/heartbeat.py`, surfaced by `core/ops/health_check.py`.
- Check: watcher defaults are configurable and disabled unless explicitly enabled.

## Distillation And Quality

- Current distillation engine: `core/hephaestus/distillation_engine.py`.
- Fragment merge: `core/hephaestus/fragment_merger.py`.
- Quality gate: `core/hephaestus/quality_gate.py`.
- Model-call ledger: `core/telemetry/model_call_ledger/`, its static compatibility seam `core/telemetry/prompt_call_log.py`, and `docs/MODEL_CALL_LEDGER.md`.
- Obsolete: old `deferred_distill.py`, `incremental_distiller.py`, and stale truncation marker scripts.
- Check: every direct billable provider call reserves the complete request before dispatch and settles from a provider usage receipt; the canonical ledger must not persist personal information, API keys, payment-card data, passwords, raw prompt/response text, caller error text, or a preview. It keeps local opaque references, subject attribution, and operational metadata only. Run `python3 scripts/audit_model_call_ledger.py --json`; this static audit does not prove a real local database has been migrated or restored. The standalone `scripts/reconcile_model_call_ledger.py` is diagnostic-only: direct `--apply` has no registry-issued capability and remains zero-write blocked. Legacy prompt stores, a missing subject-attribution schema, an unverified receipt, an orphan/missing/tampered recovery target or SQLite sidecar, or a non-zero model-call-ledger health gap block release health. When legacy owners exist, retain reviewed plan/apply/noop/backup-integrity/restore-drill evidence separately; sealed-v3 manifest and normal SQLite backup/hash/lock evidence are local recovery-correctness records, not a release certificate on their own.

## Trusted Vault Mutations

- Authority: `core/trust/vault_mutation_service.py`, `core/trust/formal_markdown.py`, and `core/trust/markdown_update.py`.
- Check: run `python3 -m core.trust.static_scan`; v4 must report unknown=0, stale_registry=0, known_bypass=0, and no registry entry may claim guarded/trusted-writer authority.
- Check: formal write/delete/move receipts bind target/content/expected-existing hash; moves also bind source/source hash. Enforce-mode mutation tests must prove the source remains unchanged and no target page appears.

## Scoring And Signals

- V2 scorer: `core/scoring/adaptive_scorer_v2.py`.
- Domain scorer catalog: `core/scoring/scorers/domain_scorers.py`.
- Feedback channel: `core/scoring/feedback_channel.py`.
- Application signal detectors: `core/app/application_signal_detectors.py`.
- Obsolete: generic V1 `adaptive_scorer.py` as the main scoring path.
- Check: training and clustering are disabled by default in public config.

## Persona

- Persona signal store: `core/persona/psyche.py`.
- Contextual persona strategy: `core/persona/contextual_strategy.py`.
- Calibration and timeline: `core/persona/calibration_cli.py`, `core/persona/evolution_timeline.py`.
- Check: persona data carries scope/context, strategy injection can be disabled, and skill suggestions are report-only by default.

## CLI And Docs

- Main CLI: `mnemos_cli.py`, `core/cli/commands/`.
- Product setup lifecycle: `mnemos setup`, `mnemos upgrade`, `mnemos uninstall`, `mnemos doctor repair-all`.
- Compatibility wrappers: `setup.sh`, `setup.bat`, `scripts/auto_setup.py`, `context_search.py`, `blindspot_discovery.py`, `predictive_push.py`, `build_embedding_index.py`, `index_manager.py`.
- Ops docs: `docs/OPS_MANUAL.md`.
- Agent docs: `docs/integrations/`.
- Check: README recommends new CLI commands; wrappers are labeled compatibility only.

## Audit Commands

```bash
python3 scripts/audit_gate_hermeticity.py --suite diagnostics --strict --json --output-dir /tmp/mnemos-diagnostics-hermetic
python3 scripts/run_tests.py quick
python3 scripts/run_tests.py integration
python3 scripts/run_tests.py system
python3 scripts/run_tests.py heavy
python3 scripts/run_full_score_gates.py --strict --real-api
python3 scripts/verify_full_score_certificate.py /tmp/mnemos-full-score-release/full_score_gates.json
python3 scripts/check_maintainability_budget.py --closure --strict --json
python3 scripts/check_zombie_code_policy.py --closure --strict --json
python3 scripts/ci_ratchet.py --closure --strict --json
python3 scripts/audit_document_asset_manifest.py --strict --desktop-mode required --json
python3 scripts/audit_inventory.py
python3 scripts/arch_dependency_graph.py --check
python3 mnemos_cli.py health --json
python3 scripts/audit_wiki_quality_contract.py --strict
python3 scripts/wiki_lint.py --summary --json --budget
python3 scripts/generate_config_examples.py
python3 scripts/verify_config_examples.py --strict
python3 scripts/audit_install_upgrade_contract.py --strict
python3 scripts/e2e_install_probe.py --tmp-home
python3 scripts/e2e_upgrade_probe.py --tmp-home --preserve-existing
python3 verify_installation.py --json
```

Release validation runs must use one absent or empty sandbox root. The environment manifest must have a non-empty `environment_hash`, no default API credential keys, `outside_write_count=0`, and `formal_state_diff=[]`; all stdout/stderr/report paths must be descendants of `sandbox_root`. Only the explicit full-score `--real-api` mode may inherit model credentials. Health, status, distill status, verify, and golden checks are read-only by default; `scripts/verify_installation.py --write-probes` is the only permission-probe mode and must create an exclusive unique file. Do not clear formal runtime ledgers, raise pending budgets, or add test-only bypasses to make release gates green.

Release eligibility additionally requires `mnemos.full_score_gates.v2`, canonical 62-gate manifest expected=selected=executed, `omitted_gate_ids=[]`, `certifying=true`, `release_eligible=true`, all required receipts passed, a clean full Git commit, and a successful independent certificate verifier. The verifier must also find the exact contracts declared by `docs/acceptance/phase5_required_full_score_gates.json`: `contracts.persona_runtime_effectiveness`, `contracts.blindspot_asset_boundaries`, and `contracts.phase5_failure_contracts`. The last gate must validate frozen baseline failure evidence and the PH5-031 `1/0/0/0` acceptance counts. Maintainability and zombie residual debt must both be zero; a valid time-bounded acceptance is sufficient only for the development profile and remains non-certifying. The vulture whitelist and committed baseline must both be zero. `docs.asset_manifest.strict` must run with required Desktop mode and report repo Markdown, Prompt/schema, and Desktop `unverified=0`; `contracts.cognitive_action_effects` must prove real target effects, reciprocal receipts and zero lineage/hash gaps; `contracts.cognitive_calibration_lineage` must prove derived-source dedupe, replayable records and projection identity with zero gaps; `contracts.cognitive_event_dispatch` and `contracts.evidence_graph_direction` must prove durable episode fan-out and canonical evidence direction; `contracts.cognitive_search` must pass its frozen ACL-first retrieval benchmark; `model_call_ledger.static` must report no unledgered direct provider boundary, but cannot substitute for actual ledger health or—where legacy sources exist—reviewed `execution_plan_hash` apply, clean/noop recheck, backup integrity, and sealed-v3 restore evidence. CI `--desktop-mode skip` cannot substitute for release evidence. `--only`, any skip selector, mock API, dirty worktree, legacy v1 report, missing/tampered stdout/stderr artifact, an internally self-consistent but non-authoritative manifest, or an omitted Phase 5 required gate is non-certifying.
