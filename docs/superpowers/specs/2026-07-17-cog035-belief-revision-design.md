# COG-035 canonical BeliefRevision design

**Status:** implemented and root-verified on 2026-07-17. This closes COG-035
only; COG-036/037/038/048 and release certification remain open.

## Scope and non-goals

This slice makes `CognitiveStateStore` the only canonical owner of current and
historical beliefs. It must answer, for one authorized caller, what Mnemos
currently believes, why, what contradicts it, where and when it is valid, and
how the current revision replaced earlier revisions.

It does not implement DecisionTrace, ValueContext, PredictionLedger, feedback
attribution, or model training. Those remain COG-036, COG-037, COG-038, and
COG-048. It also does not rename Observation, Reflection, Wiki frontmatter, or
CognitiveGraph relations into beliefs.

## Chosen module and seam

Add a deep `BeliefRevisionStore` module at
`core/cognitive/belief_revision.py`. Its implementation uses the existing
`CognitiveStateStore` unit of work; it owns no second database or mutable head.
Callers and tests use three interface operations:

1. `revise(command, principal) -> BeliefRevisionReceipt` appends or
   idempotently reuses one revision and emits committed projection commands.
2. `explain(query, principal, narrowing, now) -> BeliefExplanation` authorizes
   on the compact ACL header before hydrating the claim or evidence body.
3. `list_active(query, principal, narrowing, now) -> tuple[BeliefExplanation]`
   returns only current, non-expired, non-deprecated authorized beliefs.

Expiry is evaluated from the immutable revision's valid-time interval. It
removes a revision from active retrieval without declaring it false or erasing
history. A later explicit revision may supersede the expired head.

### Rejected approaches

- Putting belief rules directly in `CognitiveStateApplicationService` would be
  a shallow module: identity, conflict, validity, authorization, projection,
  and explanation rules would leak into callers.
- Adding `beliefs` and `belief_history` tables would create a second canonical
  owner beside `cognitive_state_revisions` and make atomic correction and
  deletion impossible to prove.
- Treating CognitiveGraph relations or Wiki frontmatter as canonical beliefs
  would preserve mutable projections and would not supply revision lineage.

## System-owned identity

The module preserves the non-empty visible claim exactly as submitted. It
derives a separate comparison form with Unicode NFKC, trimming, and whitespace
collapse, then computes:

- `claim_id = sha256(canonical_claim)`;
- `belief_id = sha256(scope_type, scope_id, claim_id)`.

Callers cannot choose or override either identifier. The same exact claim in a
different project or session is a different belief. Supporting and opposing
evidence for the same scoped claim therefore converge on one revision chain.

## Revision payload

`mnemos.belief_revision.v1` requires:

- `belief_id`, `claim_id`, visible `claim`, and `claim_kind`;
- system-derived `stance`;
- exact sorted `supporting_evidence`, `opposing_evidence`, and
  `withdrawn_evidence` refs;
- `confidence_method`, nullable `confidence`, and structured `uncertainty`;
- `valid_from`, optional `valid_until`, and `invalidation_conditions`;
- an `admission_refs` object with optional proposal/journal IDs and a required
  deterministic `projection_effect_id` computed before revision commit;
- `supersedes_revision_id`, optional `correction_of_revision_id`, and the
  canonical object ACL inherited from the authorized source.

The outer `CognitiveStateRevision` remains the authority for scope, source
event/revision/content hashes, evidence hash, admission state, redaction, and
revision number. Payload and outer fields must agree.

`confidence=None` with method `unscored` is distinct from numerical zero.
Numerical confidence is accepted only with a non-empty method, denominator or
measurement refs, and a value in `[0, 1]`. The module never assigns a uniform
historical confidence and never treats source authority as truth probability.

## State and conflict rules

The module derives stance from the active evidence sets:

- support only: `supported`;
- opposition only: `refuted`;
- support and opposition: `disputed`;
- neither after an authorized correction: `unknown`;
- an explicit authorized `disposition=deprecated` command produces
  `deprecated`.

A caller cannot submit a stance. New evidence is merged with the current
revision under `BEGIN IMMEDIATE`; an expected-head mismatch fails before any
event, revision, head, or outbox row commits. Replaying the same semantic
command returns the existing receipt and creates no revision.

Opposing evidence can never silently overwrite a supported head. It produces
a current `disputed` revision that retains both evidence sets. Evidence can be
withdrawn only by a correction command that references the current revision,
the exact evidence refs, and an authoritative correction evidence ref. The
old revision remains immutable and addressable through history.

The canonical head table enforces one current revision per belief. Active
retrieval additionally excludes `historical_candidate`, quarantined,
tombstoned, deprecated, and expired revisions.

## Authorization and privacy

Every revision derives its ACL from an authorized source envelope; callers
cannot supply a replacement ACL in the belief payload. Scope, owner, purpose,
consent, sensitivity, retention, redaction, and source ACL lineage are
validated before commit.

Read queries first select revision identity plus `access_control`. Full claim,
evidence, uncertainty, and revision history are fetched only after
`authorize_cognitive_access` succeeds for `belief_read`. Missing or malformed
ACLs fail closed. A private belief cannot cross principal, agent, project, or
session scope.

## Projection and effect receipts

`revise` commits the data event, immutable revision, current head, and local
outbox atomically. The required projection command is
`project_belief_revision` for the `cognitive_graph` consumer.

The consumer reads the committed revision, verifies payload and ACL hashes,
and projects a rebuildable node/relation view. It cannot mutate canonical
belief state. Success requires an exact effect receipt with before/after hash
and projection identity; retry is idempotent. A failed or missing projection
receipt leaves the command pending and prevents root verification.

Correction, tombstone, and expiry views must suppress stale projected heads.
The projection retains historical revision nodes marked non-current and
preserves every supersedes/correction edge.

## Legacy migration

Add `scripts/reconcile_belief_revision_candidates.py`. Dry-run is the default.
It inventories legacy Wiki pages, CognitiveGraph relations, Reflections, and
profile assertions using exact source identifiers and hashes, but never
infers a belief ID, stance, confidence, or revision relationship from prose.

Apply requires a stopped daemon, an explicit backup directory, and the exact
`inventory_hash` from the reviewed dry-run; source drift fails before backup
or write. Eligible
legacy rows are written only to the existing cognitive-state migration
quarantine as `unverified_candidate` records. They do not create heads or
enter active retrieval. The report is replayable, content-hash bound, and
includes before/after counts plus SQLite integrity evidence. Rollback restores
the verified backup rather than deleting canonical revisions.

## Strict audit and tests

Add `scripts/audit_belief_revision_lineage.py --strict --json`. Its hermetic
matrix covers first revision, exact replay, same-direction evidence,
support/opposition conflict, scope forks, expiry, explicit correction,
supersede, restart replay, projection retry, ACL denial, and migration
quarantine.

The strict report independently verifies:

- `active_without_evidence=0`;
- `multiple_current_revision=0`;
- `unresolved_silent_conflict=0`;
- `belief_acl_leak=0`;
- `unresolved_projection_effect=0`;
- `historical_candidate_active=0`;
- every active explanation contains current revision, support and opposition,
  exact scope, validity interval, confidence method, uncertainty, and revision
  lineage.

Fault injection after event, revision, head, outbox, projection, and receipt
boundaries must either roll back or remain non-terminal. Tests must use the
same public module interface as production callers; no direct fixture writes
count as success evidence.

## Verification evidence

- `audit_belief_revision_lineage.py --strict --json` passed 19/19 behavior
  contracts; all 14 lineage, ACL, conflict, validity, projection, replay, and
  migration metrics were zero against the canonical live schema.
- Focused BeliefRevision, projection, migration, application, daemon, state
  store, and CognitiveGraph ACL regression passed 67 tests.
- The isolated Quick suite passed 6,511 tests plus 15 subtests. Its hermetic
  manifest recorded environment hash
  `356a9e7e68055e0a14759ebb2b4b10d6580e19b2511c3e79e71133ecdc090947`,
  `outside_write_count=0`, and `formal_state_diff=[]`.
- The reviewed production inventory contained 41,125 exact objects: 3,393
  Wiki pages, 36,784 CognitiveGraph relations, and 948 Reflections. Its hash
  was
  `sha256:f27d20c946d88a0036b96ed3a94925bf3cb219cc2d63d0013aaf60e954308663`.
  The first apply inserted 41,125 quarantined candidates with zero active
  head/revision delta; replay inserted zero and returned 41,125 existing
  candidates. Both SQLite backups and the target integrity check were `ok`.
- The COG-043 strict ACL/deletion audit and trusted-push static scan remained
  green. The BeliefRevision audit is required by local, pre-commit, CI, and
  full-score gate manifests.
- Global maintainability and zombie release-closure debt remains non-zero and
  is not represented as a COG-035 or release pass.

## Completion boundary

COG-035 is closed only when the strict audit is green, focused tests and the
isolated Quick suite pass, generated architecture/event documents are synced,
and the governing Desktop audit entry is updated in place. Closure of this
root does not certify later Phase 3 roots or the release.
