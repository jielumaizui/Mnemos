# ROOT-20260710-010 Recap Consumption Outbox Design

## Problem and success contract

Finalized recaps currently persist a list of target labels, but most labels do not have a production dispatcher or durable receipt. A recap may therefore appear finalized or consumed before its promised effects exist. Recap feedback is also append-only only at the API edge: downstream correction failures are swallowed while the top-level call reports success.

ROOT-010 is closed only when:

- every requested target maps to a registered canonical consumer or is rejected before the plan is accepted;
- every required canonical consumer has a durable command and receipt;
- a recap becomes `consumed` only after all required receipts commit or reach an explicit, auditable intentional-skip terminal state;
- partial failure, retry, stale processing, restart and duplicate finalize are visible and idempotent;
- inaccurate, irrelevant and outdated feedback durably corrects or suppresses every previously committed effect;
- production history is migrated only from provable evidence, never inferred from a target label alone.

## Chosen architecture

`RetrospectiveConsumptionRouter` remains the public orchestration seam, but durable state and dispatch live behind a deep `RecapConsumptionLedger`. The ledger stores one append-only plan revision, canonical target commands and per-target receipts. The router may synchronously attempt the first drain so callers get immediate progress, while a daemon service retries pending or retryable receipts after restart.

The registry preserves the originally requested labels for audit and maps them to these canonical effects:

| Requested label | Canonical effect | Production meaning |
|---|---|---|
| `wiki_search`, `context_aware_search` | `knowledge_retrieval` | The committed recap page has a durable retrieval/index receipt. |
| `preflight`, `guard`, `policy_patch` | `policy_patch` | A bounded PolicyPatch is proposed, or an explicit no-patch receipt records why no patch is legal. |
| `follow_up` | `follow_up` | A durable follow-up schedule exists and can be cancelled. |
| `persona` | `persona` | A recap-derived persona signal is recorded with source event identity. |
| `scheduler` | `scheduler` | Skip/defer scheduling state is durably applied. |
| `scoring` | `scoring` | Skip/feedback scoring effect is recorded with source event identity. |

Aliases do not create duplicate effects. The plan records requested labels, canonical target, required status and mapping reason. Unknown targets are rejected instead of becoming inert required labels.

## State model and idempotency

Plans use `pending`, `processing`, `partial`, `retryable_failed`, `consumed`, `superseded` and `dead`. Receipts use `pending`, `processing`, `committed`, `intentional_skip`, `retryable_failed` and `dead`.

The stable identities are:

- `plan_id`: recap id plus immutable plan revision hash;
- `command_id`: plan id plus canonical target;
- `receipt_id`: command id plus consumer version;
- `correction_event_id`: stable recap feedback event id;
- correction receipt: correction event id plus affected command id.

The same finalized recap and plan revision reuse the same identities. A processing lease may be reclaimed only after timeout and only for idempotent handlers. Aggregate `consumed` is derived from all required receipts; it is never set independently.

## Handler behavior

Each handler returns a typed result: committed, intentional skip, retryable failure or dead failure. A no-patch result is legal only when the policy rules reject the recap and the receipt records the reason; handler absence is never an intentional skip.

Handlers must write their effect and source identity atomically in the target store where possible. Cross-database work is reconciled through the receipt, not hidden with `try/except`. Existing stores are extended with source event identity and idempotent lookup rather than wrapped by a parallel shadow implementation.

The daemon registers one `recap_consumption` service that drains pending/retryable commands. Request paths may call the same drain method but do not own an alternate implementation.

## Feedback correction

`recap_feedback` creates a durable feedback command and correction outbox. `accurate` and `useful` record positive outcomes. `inaccurate`, `irrelevant` and `outdated` enumerate all committed effects for the recap and create required correction receipts:

- policy patches are suppressed with reason and source feedback id;
- follow-ups are cancelled;
- persona and scoring receive compensating signals rather than destructive history edits;
- retrieval effects are marked superseded/review-required through the existing trusted page lifecycle; the original recap page and evidence remain auditable.

The API returns complete only after all required correction receipts commit. Partial or failed correction is visible and retryable. Duplicate identical feedback is idempotent; conflicting terminal feedback is rejected unless it explicitly supersedes the previous feedback event.

## Migration and recovery

Before schema changes, the production recap database and every target store touched by migration are backed up with SQLite online backup and verified with `PRAGMA integrity_check`.

Existing rows are migrated conservatively:

- a finalized recap page or existing policy patch can seed a committed receipt only when its source recap id is exact;
- a stored target label without a target-store effect becomes unknown/pending, not committed;
- missing historical plans are not invented from task status;
- duplicate or obsolete plan revisions remain append-only and are superseded, not deleted.

The reconciliation command defaults to dry-run, reports proposed mappings and unknowns, backs up before apply and is idempotent on a second dry-run.

## Public response contract

Finalize and status responses expose `plan_id`, `plan_status`, `target_statuses`, `required_receipt_count`, `terminal_receipt_count`, `consumed_at`, `retryable`, `failed_targets` and `effect_evidence`.

Feedback responses expose `feedback_event_id`, `correction_status`, `correction_receipts`, `supersedes_ref`, `effect_evidence` and failed/retryable targets. A top-level success flag may mean the command was durably accepted, but `terminal=true` and `status=complete` require all required receipts.

## Test and review plan

Implementation proceeds in vertical slices:

1. Finalize creates a pending plan and cannot consume without required receipts.
2. Registry rejects unknown targets and deduplicates aliases.
3. Each canonical handler produces an idempotent receipt through the public finalize/status path.
4. A consumer failure yields partial/retryable state; retry and restart commit only missing receipts.
5. Feedback correction suppresses/cancels/compensates every committed effect and exposes partial failures.
6. Production migration dry-run, apply, second dry-run and integrity checks prove no guessed history.

Named integration tests are `test_recap_consumption_runtime_e2e.py` and `test_recap_feedback_correction.py`. Focused tests are followed by the related suite, full Quick, local gates, an independent deep review of state transitions and direct database/runtime verification. No test weakening, handler allowlist, caller bypass, soft success or budget relaxation is acceptable.
