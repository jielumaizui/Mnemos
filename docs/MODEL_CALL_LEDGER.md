# Model Call Ledger

`core.telemetry.model_call_ledger.ModelCallLedger` is the single durable owner for billable model-call accounting. `core.telemetry.prompt_call_log` is a static compatibility re-export only: existing callers resolve the same objects without a second persistence path, forwarding branch, or runtime fallback. `PromptCallLog` is retired and fails closed on construction. Historical observations can enter only through the controlled, backup-gated reconciliation capability; no public runtime import or post-hoc logging API exists.

## Canonical contract

`RuntimePaths.model_call_ledger_db` resolves the only production database: `database_dir/model_call_ledger.db`. It contains `model_call_runs`, `model_call_entries`, one opaque run reference in `model_call_run_subjects`, exact per-entry opaque subject-reference sets in `model_call_entry_subjects`, durable freeze bindings, and daily/run spend tombstones. Callers must not create `prompt_calls`, `prompt_call_log`, or another model-cost store. A live provider path validates the complete SQLite contract (columns, defaults, keys, FK cascades, indexes, attribution, and no raw prompt/response columns) and fails closed rather than performing an implicit upgrade.

For each direct provider request, the boundary must:

1. Canonicalize the complete provider-visible request and reserve its UTF-8 byte **upper bound** before it sends the request. UTF-8 byte length is conservative for token reservation: a caller cannot reserve only a prompt fragment or under-report a fractional/token-truncated input.
2. Mark the reservation dispatched immediately before the provider call.
3. Settle the entry only from a provider-metered token observation plus request/usage ID, measured latency, provider/model, price snapshot, cache state, and retry attempt. The durable row stores opaque local references for external request/usage IDs, never their source strings. Unknown, partial, non-finite, negative, or unapproved-zero prices fail before dispatch; an explicit zero price requires both exact configured pricing and `model_call_ledger.allow_explicit_zero_price=true`.
4. Release only a request that never dispatched. If a dispatched request fails or has no provable usage receipt, preserve its conservative incurred reservation instead of refunding it. If actual provider cost exceeds the reservation, record `incurred_overrun` at the actual amount and block later reservations until an operator resolves it.
5. Disable SDK retries and HTTP redirect following at the provider boundary. A 3xx response is a failed dispatched request, not permission to issue a second billable POST under one reservation.

The run and daily caps use effective cost: settled actual cost after refund, or the conservative reservation for in-flight/unverified calls. Deletion and retention move removed spend into daily and still-live-run tombstones, so neither privacy deletion nor retention can reset a cap.

Direct chat completion, embedding, rerank, multimodal extraction, distillation extraction/judging/correction/merge, freshness redistillation, intent routing, reflection, and explicitly requested installation smoke calls all use this boundary. `scripts/audit_model_call_ledger.py --json` scans production `core/` and `scripts/` provider sinks with a fail-closed AST control-flow/path proof: every reachable sink path must prove reserve, dispatch, settlement or explicit handoff, pre-dispatch release, and post-dispatch incurred-cost preservation.

## Privacy and lifecycle

The ledger stores an opaque local input reference, not visible prompt text, response text, a preview, image data, or a base64 request payload. The intentionally narrow privacy scope is to redact or not persist personal information, API keys, payment-card data, passwords, raw prompt/response content, and caller error text. Caller run identifiers are normalized to local opaque IDs before persistence; reconciliation removes old raw run IDs rather than accepting them at runtime. Persisted failure data is an audited error-category allowlist, never caller text or exception messages. Operation and cache state are reviewed finite enums. Provider/model are constrained operational labels while routing and are persisted only as local metadata references; unsafe legacy metadata and external usage/request IDs are likewise reduced to opaque references. `model_call_run_subjects` supplies only a run-level budget root; exact deletion authority is `model_call_entry_subjects`, which can hold several subject references for one batched request. A run root is never promoted into a fabricated entry map during migration. This is a local data-minimization contract: it does not claim that third-party provider logs, unrelated copies, or historical backups are erased.

The receipt guard is an application-level local accounting boundary: normal callers and public APIs cannot fabricate a settleable observation through the supported interfaces. It makes no broader isolation or device-security claim.

The supported terminal states are `settled`, `released`, `usage_unverified`, `incurred_unknown`, `incurred_overrun`, and `legacy_observed`. Historical prompt telemetry is imported only as `legacy_observed`: it has no fabricated provider usage or settled cost.

Retention uses `storage.retention_days.model_call_ledger`; the default daily ceiling is `model_call_ledger.daily_cost_cap`, and the explicit-zero-price switch defaults to `false`. Maintenance, health, privacy inventory, retention, and deletion all resolve the same `RuntimePaths` owner. Freeze is a durable provider-dispatch barrier at run creation, reservation, and dispatch; deletion blocks while a matched request is dispatched but unresolved. An apply-mode retention or subject deletion closes its own SQLite descriptor, checkpoints WAL, requires `journal_mode=DELETE` and `secure_delete=ON` before it releases entry/run cells; dry-run changes neither rows nor journal configuration. After freeze, snapshot, and explicit confirmation, `DataOwnershipManager.delete()` physically removes only exact attributed entries and then empty runs; its proof remains `partially_deleted` until other data domains obtain their own physical deletion adapters. The SQLite cleanup proof is limited to the active ledger database, its journal/WAL handling, and released SQLite cells. It is not a device-level forensic-erasure claim for filesystem snapshots, Time Machine, copied files, provider-side records, or other data domains; a normal local migration backup is intentionally retained for recovery until its separately chosen retention policy expires.

## Health evidence

`python3 mnemos_cli.py health --json` exposes `checks.model_call_ledger`. It is a bounded read-only check: absent paths, databases, or tables are reported as uninitialized/blocked rather than being created, migrated, or populated during health. The separate `checks.heartbeat` projection is likewise content-free and bounded; it is evidence about daemon liveness, not permission for a health command to mutate ledger state. A fully reconciled ledger requires all of these values:

- `billable_calls_without_ledger=0`
- `billable_request_without_reservation=0`
- `settled_cost_without_provider_usage=0`
- `sensitive_prompt_preview=0`
- `model_call_storage_path_count=1`
- `health_ledger_path_mismatch=0`
- `unverified_provider_usage=0`
- `reservation_cost_overrun=0`
- `subject_attribution_schema_missing=0`
- `entry_subject_attribution_schema_missing=0`
- `privacy_dispatch_schema_missing=0`
- `metered_usage_receipt_schema_missing=0`
- `unattributed_model_call_run_count=0`
- `unattributed_billable_entry_count=0`
- `unrecoverable_run_tombstone_history_disposition=0`
- `runtime_schema_gap_count=0`
- `invalid_daily_cost_cap=0`

An absent ledger is clean only when no retired store exists. Any remaining `prompt_calls`, `prompt_call_log`, or `prompt_call_stats` table degrades health and keeps the migration signal visible.

## Historical migration and recovery

The registered COG-018 path is deliberately two-phase and default-read-only. It is an operator contract; this document does **not** assert that any production database has already been migrated or restored.

```bash
# 1. Read-only plan. Record the COG item execution_plan_hash for review.
python3 mnemos_cli.py migrate plan --json

# 2. Stop the daemon, then apply the exact reviewed plan. Add either discard
# flag only when the reviewed plan requires that explicit disposition.
python3 mnemos_cli.py daemon stop
python3 mnemos_cli.py migrate apply database.model_call_ledger.v1 \
  --execute-wrapped --expected-plan-hash "<execution_plan_hash>" --json

# Optional, only for a reviewed plan that explicitly requires it:
#   --discard-unattributable-legacy
#   --discard-unrecoverable-run-tombstone-history

# 3. Verify the second plan is clean/noop and audit direct provider boundaries.
python3 mnemos_cli.py migrate plan --json
python3 scripts/audit_model_call_ledger.py --json

# 4. Recovery is also two-phase. Inspect the sealed v3 manifest first; only
# an explicit apply + wrapped execution restores the normal local backup.
python3 mnemos_cli.py migrate rollback database.model_call_ledger.v1 \
  --recovery-manifest "<MNEMOS_DIR>/.../mcl-recovery-<id>.json" --json
python3 mnemos_cli.py migrate rollback database.model_call_ledger.v1 \
  --recovery-manifest "<MNEMOS_DIR>/.../mcl-recovery-<id>.json" --apply --execute-wrapped --json
```

For a non-clean apply, the registry passes the reviewed `execution_plan_hash` through as the reconciler's exact expected plan hash. A missing or mismatched hash is a zero-write `blocked` result. A clean state returns zero-write `noop` before it needs an expected hash, migration ledger, configuration write, or backup. The apply path requires the daemon to be inactive, makes a normal local SQLite backup, checks backup integrity, re-plans before mutation to catch source drift, and records a sealed `mnemos.model_call_ledger_recovery.v3` recovery manifest. Here “sealed” means a local restoration-correctness record: it binds the reviewed migration, verified pre/postimages, journal progress, and recovery inputs. The ordinary SQLite backup, SHA-256 checks, and runtime lock are likewise local recovery-correctness mechanisms. Rollback accepts only the sealed v3 recovery form after validating its migration binding, append-only journal completion/interruption state, and postimage; legacy or tampered manifests are rejected.

`scripts/reconcile_model_call_ledger.py` is a supported standalone diagnostic wrapper around `core.migrations.model_call_ledger_reconcile`; it can be invoked from outside the repository, but it grants no mutation authority. Direct `--apply` lacks the registry-issued execution capability and therefore remains zero-write `blocked`. The only public mutation path is the registered `mnemos migrate apply database.model_call_ledger.v1 --execute-wrapped ...` command above.

Reconciliation preserves distinct physical legacy rows even when their non-content metadata matches; a repeated exact source database/table/row identity fails closed instead of being silently collapsed. A missing entry-level subject map is coverage-unknown: legacy observations require an explicit reviewed discard, while nonlegacy billable entries fail closed for manual repair. If the plan reports unrecoverable cascading run-tombstone history, it remains a release blocker unless the separately reviewed explicit discard option is supplied. The recovery manifest selects the targets actually in scope for the reviewed plan and binds each selected database plus its durable SQLite sidecars. A missing, unexpected, orphaned, stale, or tampered target/sidecar fails closed; it is never silently ignored. Any post-backup structural or metadata drift blocks cleanup and requires a new plan plus backup. Cleanup of retired SQLite tables uses `journal_mode=DELETE` and `secure_delete=ON` only after the backup/recovery evidence is available; that is local SQLite hygiene, not a claim that all historic copies have been forensically erased.

## Recorded local COG-018 evidence

On 2026-07-14, the isolated full Quick suite completed with `6156 passed, 15 subtests`. A local operator drill then stopped the daemon, applied the registered migration, verified ledger health and the plan, restored from the newly produced sealed-v3 manifest, re-applied the registered migration, and verified the final plan and ledger health again. This is real local migration/recovery evidence for COG-018; it is not a full-repository release certificate.
