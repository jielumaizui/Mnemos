# COG-043 remaining domains: object-level provenance migration

**Status:** `CLOSED_ROOT_VERIFIED` on 2026-07-17. This closes COG-043, not the
remaining Phase 3 roots or the release certificate.

## Scope

This slice closes the missing physical/tombstone owners for the three remaining
COG-043 domains:

- append-only `action_ledger`;
- generic EventBus metadata in `events.db`;
- adaptive scoring samples and derived scorer models.

It does not relabel legacy records from strings, JSON payloads, target names,
or session-like values. A record without a typed provenance envelope remains
`unattributed`, is never used as proof of a scoped deletion, and is reported
as an unresolved legacy object.

## Shared object contract

Every newly written provenance-addressable object carries the existing typed
`CognitiveAccess` envelope. The storage sidecar records its canonical hash and
indexed, hash-only selectors for exact `agent`, `project`, `session`, and
declared object scope. The sidecar is created in the same SQLite transaction as
the domain object.

Deletion accepts only a typed `DataSubjectRef`. It selects object IDs through
the selector index, records a hash-only receipt, changes the object to a
tombstoned/redacted state or physically removes it, runs an after-oracle, and
keeps legacy uncertainty explicit. A scope cannot become `verified` by a
zero-target retry after a partially completed attempt.

## Domain behavior

### ActionLedger

The immutable evidence row is retained, because the ledger's append-only
contract is itself an operational proof. A deletion appends an immutable
tombstone containing only the action ID, subject hash, and record hash.
`ActionLedger.recent()` becomes a redacted projection for tombstoned entries:
no target, actor, evidence references, rollback reference, or verification body
is returned. Future reads and replay through the public facade are therefore
blocked while proof integrity remains auditable.

### EventBus metadata

Each persisted trace receives a provenance sidecar. A typed deletion tombstones
the trace before deleting its event/dead-letter payload, handler outputs, trace
claim, and deferred-key rows. Event recovery, refill, dispatch, and dead-letter
replay all check the tombstone before deserializing or handing an event body to
a consumer. A tombstoned trace ID cannot be republished.

### Scoring

Training samples, ground-truth rows, and identified feedback receive object
provenance. A scoped deletion physically removes only linked samples and
feedback, then invalidates every affected model through exact sample lineage.
Models without complete lineage are conservatively invalidated rather than
claimed safe. Future training may rebuild only from remaining samples; neither
model blobs nor Bayesian aggregate state may remain a deletion-success proof
without a fresh rebuild.

## Migration and safety boundaries

- Existing rows are not inferred or backfilled from free-form content.
- Unattributed rows remain functional legacy data only where existing runtime
  compatibility requires it, but they never make a deletion receipt verified.
- `all:all` may tombstone or delete every object in a domain because that scope
  is exact; narrower scopes never sweep unattributed rows.
- The EventBus deletion owner uses secure delete, a WAL checkpoint, and a fresh
  after-oracle before terminal success.
- The ActionLedger is intentionally a tombstone-only owner; its immutable
  source row is never mutated or deleted.

## Acceptance evidence

Focused tests must prove:

1. a typed session provenance matches only its exact domain object;
2. missing provenance is reported as unresolved, never guessed;
3. a tombstoned ActionLedger row has no readable body through the facade;
4. event payloads and replay paths cannot revive a tombstoned trace;
5. scoring deletion removes only linked samples and invalidates derived models;
6. DataOwnership composes all three results without upgrading partial or
   legacy-unknown effects to `verified`.

The strict COG-043 audit consumes a separate hermetic physical-effect matrix.
Its object denominator includes all eight persistent scoring object families,
and its fault denominator includes all 15 DataOwnership domains and six
required Wiki projection consumers.

## Implementation record (2026-07-17)

Implemented after the user approved object-level provenance migration:

- ActionLedger now has a sidecar, append-only tombstone, tombstone-gated
  projection, and replay prevention.
- EventBus persists provenance for pending and direct dead-letter paths;
  recovered events preserve their sidecar through later dead-letter moves.
- Scoring covers direct sample/search/feedback/prompt writes and derived model
  and Bayesian state lineage. Bayesian feedback/state persistence is atomic;
  deletion-resume rehydrates tombstoned target IDs before its after-oracle.
  Ground-truth updates preserve the physical object ID and reject immutable
  ACL conflicts before changing the body. Bayesian aggregates accept another
  source only when its authorization boundary matches the existing aggregate;
  a cross-scope source rolls back the feedback row, sidecar, state, and
  in-memory prior together.
- `DataOwnershipManager` composes the three owners without treating legacy
  `unattributed` rows as a scoped deletion success.

Verification:

- the hermetic physical-effect and fault-injection matrix is `28 passed`;
- the scoring/feedback focused regression is `153 passed`;
- `python3 scripts/audit_cognitive_acl_propagation.py --strict --json` returns
  `ok=true`, zero ACL/default-public/cross-scope/pre-body/lineage/physical
  effect/residual gaps, and empty missing-object and unimplemented-domain sets;
- the final isolated Quick run is `6482 passed, 15 subtests passed`, with
  `outside_write_count=0`, `formal_state_diff=[]`, and environment hash
  `9fd17d8e32427e88c75b8ce9da33a889f5a2a21a11ea8b735446eedccb12e131`.

These results close the COG-043 root. They do not claim that the remaining
Phase 3 roots or the repository release certificate are complete.
