# OpenCode cross-session Raw reconciliation design

**Status:** Design approved by the user on 2026-07-13; awaiting written-spec
review before implementation.

## Objective

Repair the Phase 1 OpenCode capture incident without deleting observed Raw
bytes, rewriting immutable revisions, restoring a broad database snapshot, or
making an unsupported claim about source history that is no longer present.

The immediate defect was a SQLite source adapter that selected a native session
through a mutable discovery-order queue.  The daemon reconciler is free to
parse sessions in a different order, so it could bind a message from one
OpenCode session to another session's Raw identity.  The parser itself is fixed
in commit `36646d12`; this design closes the persisted-data and downstream
visibility portion of that incident.

## Observed production boundary

The read-only incident audit on 2026-07-13 established the following facts
without exporting transcript bodies:

- the current OpenCode source exposes 40 sessions and 267 exact
  `session_id × native message_id` identities;
- all 267 current identities have a Raw row under the same session;
- 257 additional Raw rows have a native message identity that still exists in
  the current source but belongs to a different session;
- no current identity is missing from Raw, and no parsed current session is
  empty.

The last category is deterministic evidence of cross-session misattribution,
not a heuristic based on text similarity, timestamps, or queue ordering.  Raw
rows whose native identity is no longer observable must remain outside this
incident set: they may be legitimate historical evidence from a source that has
since changed.

## Non-negotiable constraints

- Never delete a `raw_turns` row, immutable revision, provenance edge, or
  historical source record as part of this repair.
- Never use `eligible_delete`, retention scoring, or a cross-session identity
  alias as a substitute for a capture-integrity decision.
- Never construct a claim from plaintext history.  Reports, receipts, tests,
  and Desktop documentation may contain counts, stable IDs where the existing
  contract permits them, hashes, error codes, and timestamps only.
- Never quarantine a row merely because it is absent from the current source.
  Quarantine requires a current native identity that proves another session is
  the owner.
- Keep direct Raw/revision lookup available for forensic audit.  The change
  affects normal discovery, projection, and acceptance paths, not evidence
  preservation.
- Do not run a general daemon or any LLM/Agent backend as part of this repair.

## Alternatives considered

### A. Native-contract quarantine plus exact-source re-backfill (recommended)

Use the existing append-only `NativeRawContractLedger` to record a
`nonconforming` observation for each provably cross-session logical Raw event.
Default current-data consumers exclude a logical event whose latest native
contract observation is nonconforming.  The exact current OpenCode source is
then re-backfilled through the official `RawEventStore` path, preserving prior
revisions and restoring the correct current revision for every observable
native event.

This preserves evidence, scopes the correction to proven mismatches, and uses
the current source identity contract rather than inventing a parallel Raw
validity mechanism.

### B. Restore a pre-incident database snapshot

Rejected.  A full restore would discard legitimate local activity that occurred
after the snapshot and would not itself prove the restored source/session
relationship.

### C. Delete or lifecycle-purge the 257 rows

Rejected.  Deletion violates the immutable-evidence contract, while assigning
`eligible_delete` would misuse a retention state and make later purge behavior
unsafe.

## Design

### 1. Deterministic incident classifier

Add one formal reconciliation entry point for the OpenCode incident.  It must:

1. instantiate `OpenCodeSource` and require its session-aware
   `parse_session(SessionInfo)` implementation;
2. build the current set of `(canonical_session_id, native_event_id)` pairs;
3. read only native-identity Raw metadata and classify each row as exactly one
   of `exact_pair`, `cross_session_native_identity`, or
   `unobserved_native_identity`;
4. fail closed if current parsing produces duplicate identities, a row lacks a
   native identity/current revision, an expected pair is missing, or an
   unobserved identity would be included in an apply set;
5. emit only counts and a deterministic receipt hash in dry-run output.

The classifier may quarantine only `cross_session_native_identity`.  It does
not infer ownership from content, and it does not touch legacy rows without a
current native identity proof.

### 2. Append-only effective quarantine

The reconciliation `--apply` mode requires a caller-supplied backup directory.
It creates a SQLite backup through the SQLite backup API, verifies
`PRAGMA integrity_check`, and then opens one transaction.

For every provable incident row, it records an append-only native contract
observation against the current revision with:

- `contract_state=nonconforming`;
- stable error code `cross_session_native_identity`;
- a stable reconciliation/incident identifier and support-manifest hash.

It then refreshes the existing ledger's effective state.  This preserves the
Raw row and every revision, lowers the effective current status to `partial`,
and records the result in the existing contract ledger instead of adding a
cross-session alias or a second state owner.

The script writes a content-free receipt beside the verified backup.  Apply is
idempotent: re-running it records no duplicate effect and must report the same
candidate receipt hash unless the native/source evidence changed.

### 3. Safe default visibility policy

Extend the existing native Raw contract ledger with one shared, fail-closed
predicate for a logical event whose latest native contract observation is
nonconforming.  Apply that predicate to normal current-data paths:

- `RawEventStore.list_current_headers()` and current logical-event lookup;
- the Raw projection source query;
- the strict Raw projection fidelity denominator and its reverse check;
- lifecycle refresh/query paths that select current discoverable evidence.

Direct `get_turn(event_id)`, revision headers, revisions, and contract
observation history remain readable so an authorized auditor can inspect the
preserved evidence and its nonconforming receipt.  A missing ledger table or an
unreadable latest observation is a verification failure, not permission to
surface the event.

### 4. Exact-source correction and projection

After quarantine, invoke the existing
`scripts/backfill_raw_event_store.py --source opencode` against production
Raw.  It uses the repaired `parse_session` seam and `RawEventStore.upsert_turn`
to update only the 267 exact current identities; differing values become new
immutable revisions rather than destructive rewrites.

Republish the Raw projection using the normal lossless settings.  The projector
removes only its own stale publisher chunks after a durable new journal; it
does not move user notes.  The strict fidelity auditor uses the same visibility
predicate, so its denominator represents only normal, conforming current Raw.

## Failure handling and rollback

- A failed dry-run or source/Raw cardinality mismatch blocks apply.
- A backup failure, integrity failure, transaction error, or receipt-hash
  inconsistency rolls back the transaction and leaves the prior current state
  unchanged.
- Existing nonconforming observations are never silently removed.  Any future
  reversal requires a separate explicit reconciliation with new native evidence
  and its own append-only conforming observation.
- Projection failure keeps the previous published chunk until its atomic
  replacement succeeds; strict fidelity remains failing until publication is
  correct.

## Verification plan

The implementation must add focused tests for:

1. reverse-order SQLite parsing and SyncEngine session-aware dispatch;
2. classifier separation of exact, cross-session, and unobserved identities;
3. apply backup/integrity/transaction behavior and idempotent receipt output;
4. retention of the original Raw row/revision and visibility of its
   nonconforming observation;
5. exclusion from default headers, event lookup, projection, strict fidelity,
   and lifecycle candidates while direct forensic reads remain available;
6. exact-source backfill producing correct current revisions without deleting
   the quarantined evidence;
7. full relevant unit/integration tests, source-support manifest audit, Raw
   projection fidelity audit, and the Phase 1 challenger pass.

## Completion criteria

This incident is closed only when the exact-source audit reports zero
cross-session rows visible through normal current-data paths, zero missing
observable identities, zero unobserved identities in the applied set, a
verified backup plus content-free reconciliation receipt, a successful
lossless projection/fidelity pass, and committed code/document evidence at the
current repository commit.
