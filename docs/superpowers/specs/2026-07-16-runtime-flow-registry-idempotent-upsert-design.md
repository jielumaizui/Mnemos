# Runtime Flow Registry Idempotent Upsert Design

Date: 2026-07-16
Status: approved for implementation

## Problem

Daemon startup re-registers the 24 canonical adaptive runtime flows. The current
`INSERT OR REPLACE` statement deletes an existing `runtime_flow_registry` row
before inserting its replacement. Once `runtime_flow_events` or
`runtime_flow_receipts` reference that row with `ON DELETE RESTRICT`, a normal
restart fails with `sqlite3.IntegrityError: FOREIGN KEY constraint failed`.
The production database is internally consistent; startup is attempting an
invalid parent-identity replacement.

## Selected design

Use SQLite `INSERT ... ON CONFLICT(flow_id) DO UPDATE` so the existing parent
identity is updated in place. Mutable contract fields and `updated_at` follow
the canonical matrix. `registered_at` remains the original registration time.
Existing events, receipts, generation IDs, and idempotency keys remain byte-for-
byte unchanged.

Rejected alternatives:

- Rebuild or clear the ledger: destroys runtime evidence.
- Disable foreign keys or skip bootstrap: hides the invariant violation and
  makes future restarts unsafe.

## Failure and transaction semantics

Each flow registration remains one SQLite transaction. Validation runs before
the write. Any SQL or constraint failure rolls back that flow without touching
its children. No production-data migration is required because the schema and
current rows already pass `PRAGMA foreign_key_check`.

## Verification

Add a regression that registers a flow, writes one production event and one
terminal receipt, then re-registers the same flow with changed contract fields.
The test must prove:

- registration succeeds;
- `registered_at` is preserved and `updated_at` advances;
- mutable registry fields are refreshed;
- event and receipt rows are unchanged;
- `PRAGMA foreign_key_check` remains empty.

Then run the runtime-ledger unit and integration suites, the original foreground
daemon reproduction, background `daemon start/status`, health reduction, Agent
Kit conformance, and an authenticated Mnemos MCP call after the Codex-hosted
server reloads its repaired capability reference.
