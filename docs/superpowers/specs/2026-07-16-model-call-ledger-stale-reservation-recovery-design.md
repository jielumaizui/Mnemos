# Model Call Ledger Stale Reservation Recovery Design

Date: 2026-07-16
Status: implemented and verified

## Problem

A provider request can cross the durable dispatch barrier and then lose its
settlement transition when a daemon instance exits. After the existing
five-minute grace period, `ModelCallLedger.reserve()` detects that dispatched
reservation as stale but only raises an invariant error. One row from a
previous daemon instance can therefore block every later model reservation
indefinitely.

The embedding indexer amplifies that control-plane failure. It catches the
typed ledger exception through broad `RuntimeError` handlers, falls back from
batch embedding to individual calls, and then retries missing vectors while
building the backend. Each chunk emits a full traceback. The production
incident made PID 84300 CPU-bound without reaching the SiliconFlow provider.

The current effective daily cap is `10.0`. The current Wiki contains 3,386
pages, 81,045 chunks, and 48,722,042 UTF-8 content bytes. At the configured
SiliconFlow fallback input price of `0.0005` per 1,000 tokens, one full rebuild
has a content-only estimate of `24.361021` before canonical request framing.
The existing cap cannot accommodate one current rebuild.

## Considered approaches

### Selected: conservative automatic recovery plus a typed build circuit breaker

Atomically convert stale dispatched reservations to `incurred_unknown` at the
original reserved cost, then continue the new reservation. Propagate
`ModelCallLedgerError` through all chunk fallback seams and handle it once at
the index-build boundary. Raise the configurable global daily-cap default and
the current local configured value to `50.0`.

This preserves worst-case cost accounting, restores liveness, prevents log
amplification, and provides enough headroom for one current full rebuild plus
about `25.64` of content-cost capacity for request framing, later daily
increments, and other model calls.

### Rejected: release or delete the stale reservation

Releasing, deleting, or refunding a dispatched reservation would assert that
the provider incurred no cost. A daemon crash cannot prove that fact, so this
would undercount spend and weaken the dispatch barrier.

### Rejected: dynamic or effectively unlimited rebuild budget

An operation-specific bypass or a very high global ceiling would reduce the
chance of a rebuild being interrupted, but it would also let a genuine retry
loop incur unbounded cost. A configured `50.0` value is simple, reviewable,
and matches the user's requirement of at most one planned full rebuild per
day with remaining room for incremental work.

## Selected design

### 1. Atomic stale-reservation recovery

`LedgerLifecycle.reserve()` will recover stale dispatched reservations inside
the same `BEGIN IMMEDIATE` transaction used for the new reservation. Recovery
runs after runtime schema and persisted-data validation and before overrun and
budget checks.

Only rows satisfying all of these conditions are eligible:

- `lifecycle_state = 'reserved'`;
- `request_dispatched = 1`;
- `dispatched_at` is a valid canonical timestamp;
- `dispatched_at` is at least 300 seconds old.

Each eligible row transitions to:

- `lifecycle_state = 'incurred_unknown'`;
- `actual_cost = reserved_cost`;
- `actual_total_tokens = reserved_input_tokens + reserved_output_tokens`;
- `error_code = 'stale_dispatched_reservation_recovered'`;
- `settled_at = <current canonical UTC time>`.

The transition retains `request_dispatched=1`, the original reservation,
price snapshot, attribution, and timestamps. It writes no refund. All eligible
rows are recovered atomically; a row-count mismatch or validation failure
rolls back the transaction and preserves the current fail-closed behavior.

Fresh dispatched reservations remain untouched. Malformed timestamps remain
schema-integrity failures. `incurred_overrun`, invalid pricing, frozen
subjects, and exhausted budgets remain blocking conditions. Recovery restores
liveness without turning any other invariant into a soft failure.

The new error code will be added to the reviewed safe-error-code registry. A
single summary warning may report the recovered row count; it must not contain
provider response text, model input, subject values, or a traceback.

### 2. Embedding-index circuit breaker

`ModelCallLedgerError` is a control-plane failure, not a per-chunk embedding
failure. The index manager will import and recognize the typed base error at
every broad fallback seam:

- batch embedding fallback;
- individual embedding fallback;
- HNSW missing-vector recovery;
- memory-backend missing-vector recovery.

The typed error will propagate to `build_index()`, which will emit one concise
warning without `exc_info`, abort the current build, and return a structured
result with `status="blocked"` and `reason="model_call_ledger"`. It will not
save metadata or an index for the failed build. Existing metadata and vectors
remain available for the next healthy run.

Provider transport and content-level failures retain their existing fallback
behavior. This change is intentionally limited to ledger control-plane errors
so it does not remove useful recovery for isolated embedding failures.

### 3. Cost headroom

`model_call_ledger.daily_cost_cap` remains the sole runtime configuration key
for the global pre-dispatch ceiling. `50.0` is the new default and the selected
local value, not a fixed business-rule constant. An explicit value in the
runtime JSON configuration overrides the default, so a later adjustment does
not require a code change. A running daemon is not required to hot-reload this
setting; a controlled restart applies a newly persisted value.

The canonical default will change from `10.0` to `50.0` in:

- `core/config.py`;
- the lifecycle fallback used during reservation;
- the reporting fallback used by health inspection;
- `config/config.example.yaml`;
- `config/config.example.json`.

The effective local runtime configuration will also be set to `50.0` without
altering unrelated or sensitive configuration fields. The cap remains a hard
pre-dispatch global ceiling. A missing key uses the default; explicit null,
negative, non-finite, or malformed values remain invalid. Zero remains the
most restrictive valid ceiling, not an opt-out switch. There is no
caller-selectable bypass.

Reservation and health paths must resolve the value through the injected
configuration on every operation. They may use `50.0` only as the missing-key
default; they must not special-case that number or expose an operation-level
override. The effective resolution contract is:

1. an explicit `model_call_ledger.daily_cost_cap` in the runtime configuration;
2. otherwise the canonical `50.0` default.

The persisted form remains:

```json
{
  "model_call_ledger": {
    "daily_cost_cap": 50.0
  }
}
```

At the measured corpus size, `50.0` is roughly twice the content-only cost of
one full rebuild. The remaining headroom covers canonical framing, daily
incremental indexing, and other normal model calls while retaining a bounded
failure limit.

### 4. Health and audit semantics

After recovery, `stale_inflight_model_call_entry_count` returns to zero. The
recovered rows remain visible as `incurred_unknown`, so unverified provider
usage and its conservative cost are not hidden. Health may remain degraded
until those unknown-usage records are accepted or reconciled through an
explicit future workflow; liveness does not imply verified metering.

No schema migration, database rebuild, row deletion, cost refund, or fabricated
provider receipt is part of this change.

## Verification

The existing stale-reservation regression will be changed from asserting a
permanent block to asserting automatic recovery. Tests must prove:

- a stale dispatched reservation becomes `incurred_unknown`;
- reserved cost and attribution are conserved exactly;
- the reviewed recovery error code and terminal timestamp are written;
- a new reservation succeeds in the same transaction;
- a fresh dispatched reservation is not recovered;
- multiple stale rows recover atomically;
- malformed state and unresolved overruns still fail closed;
- the recovered cost still participates in run and daily budget checks.

An index-manager regression will use a fake client that raises
`ModelCallLedgerInvariantError` with many chunks. It must prove:

- only the initial embedding attempt occurs;
- no per-chunk embedding attempts occur;
- exactly one safe summary warning is emitted without a traceback;
- the result is `blocked/model_call_ledger`;
- existing metadata and index files are unchanged.

Configuration tests will assert the `50.0` default and YAML/JSON example
parity. A non-default injected value must be honored consistently by both
reservation enforcement and health reporting, proving that `50.0` was not
hard-coded into runtime behavior. The original temporary red harness must
turn green. Targeted ledger, provider-boundary, index-manager, health,
configuration, and audit tests run before the repository quick suite.

## Rollout and operational boundary

The Mnemos daemon remains stopped during implementation and verification. The
change will not rebuild an index, mutate the live stale row, or restart the
daemon without a later explicit rollout step. Once the repaired daemon is
started, the first new reservation performs the conservative recovery before
any provider dispatch.

The repository currently contains substantial unrelated uncommitted work,
including an independent `core/embeddings/index_manager.py` change. The
implementation must preserve that diff and stage only files changed for this
repair.

## Acceptance criteria

The repair is complete when all of the following are true:

- the deterministic stale-reservation harness is green;
- a stale row cannot permanently block later reservations;
- no ledger error can create batch-to-chunk traceback amplification;
- cost accounting never refunds an uncertain dispatched call;
- the default and current local daily cap are `50.0`, while a later explicit
  configuration value is honored without a code change;
- targeted tests and the quick suite pass;
- the daemon remains stopped unless separately authorized to restart.
