# COG-038 canonical feedback attribution design

**Status:** approach A and the all-entry-point scope were approved by the user
on 2026-07-18. The implementation and hermetic verification are complete in the
current worktree; final independent review, production schema/history migration,
post-migration strict audit, and commit-bound documentation evidence are still
required before `CLOSED_ROOT_VERIFIED` may be claimed. COG-037 is closed at
`47d532d0`; COG-048, global maintainability/zombie closure, and release
certification remain separate boundaries.

The implementation keeps this specification as the governing contract. It adds
the canonical reaction/attribution owner, full-revision correction state,
atomic supersession of unexecuted commands, reciprocal domain receipts,
fail-closed principal/scope binding, real recap compensation, independent
history/audit denominators, and object-level provenance quarantine. Legacy
scorer and reflection readers explicitly exclude quarantined source classes;
history is never promoted to active reaction, outcome, preference, policy,
persona, reinforcement, or training truth.

## Scope and completion boundary

This slice makes one deep feedback-attribution owner over the existing
`CognitiveStateStore`. It accepts every formal feedback entry point, records an
append-only `UserReactionEvent`, keeps objective `OutcomeMeasurement` evidence
separate, computes a versioned attribution disposition, and issues exactly one
typed command/receipt per eligible downstream target. A correction must
supersede the current event and revoke, compensate, or suppress every previously
committed effect before a replacement effect can become active.

The root is closed only when all current feedback entry points use this owner:

- predictive push feedback from `IntelligenceApplicationService.push_feedback`;
- delivery feedback and presentation/cooldown state;
- Context Search click, open, ignore, and later behavioral observations;
- adaptive scorer feedback and training-evidence admission;
- trust, policy, persona, belief/reflection, and optimizer proposal paths; and
- explicit corrections, late objective outcomes, and replay after partial
  consumer failure.

This slice does not treat reaction as objective task truth, train a general
model, or claim that feedback attribution proves causality. COG-037 remains the
owner of prediction-linked objective `OutcomeMeasurement`; COG-048 remains the
owner of trustworthy training admission and training governance. COG-038 may
produce an attributed, receipt-bound training-evidence proposal, but it cannot
write a training label or update model state directly.

The user selected object-level provenance migration for history. Existing rows
may be inventoried, backed up, linked by exact primary/foreign/source-event
identity, and quarantined. They may not be semantically promoted to active
reaction, objective outcome, causal attribution, preference, reward, correction,
or training truth.

## Current-state evidence

The code already registers skeletal names for `mnemos.user_reaction_event.v1`,
`mnemos.outcome_measurement.v1`, and `mnemos.cognitive_update_receipt.v1`.
`OutcomeMeasurement` has been hardened by COG-037, but the reaction contract
still contains only broad delivery/principal/interaction fields and has no deep
owner, attribution state machine, correction fanout, materiality gate, migration
tool, or strict audit.

The current legacy flow has several independent writers:

- `FeedbackEventLedger` keys a row by `(delivery_event_id, principal_id)` and
  returns `feedback_action_conflict` for a different later action instead of
  appending a correction.
- `IntelligenceApplicationService.push_feedback()` directly fans the same
  action into penalty, `OutcomeRecorder`, adaptive scorer, delivery, and trust.
- `OutcomeRecorder.record_outcome()` maps a reaction to a label, derives
  `expected_score=0.85/0.15`, and writes scorer, Layer5, cognitive shift,
  optimizer, and `cognitive_outcomes` projections. `_label_for_action()` treats
  every action not present in its negative set as positive.
- `FeedbackSignalRouter.record_signal()` runs the negative-trust bridge even
  when `INSERT OR IGNORE` reports `created=false`, so a replay can produce a new
  trust effect.
- `KnowledgeDeliveryRouter.record_outcome()` stores caller-provided label and
  confidence, then creates another feedback signal.
- Context Search writes click/ignore directly to `ground_truth_signals` and then
  calls `OutcomeRecorder`, so interface behavior becomes both ground truth and
  an alleged objective outcome.
- reflection consumers accept implicit feedback at confidence `>=0.5` and can
  trigger Layer5/policy behavior without the unified attribution and correction
  contract.

The daemon was confirmed stopped and the repository was clean at
`47d532d0`. Read-only production inspection found:

| Legacy surface | Current rows relevant to the inventory |
| --- | ---: |
| `delivery_events.feedback_events` | 0 |
| `delivery_events.feedback_receipts` | 0 |
| `delivery_events.cognitive_outcomes` | 94 |
| `delivery_events.outcome_feedback_events` | 14 |
| `delivery_events.outcome_projection_receipts` | 70 |
| `feedback_signals.feedback_signals` | 58 |
| `mnemos.search_sessions` | 297 |
| search click/ignore rows in `ground_truth_signals` | 297 |
| source-attributed push/search rows in `scorer_training_queue` | 96 |
| `reflections.layer5_experiences(type='outcome_feedback')` | 96 |
| `reflections.cognitive_shifts(shift_type='outcome_feedback')` | 96 |
| push/search rows in `rule_weight_optimizer.rule_outcomes` | 96 |
| push/search rows in `rule_weight_optimizer.optimize_log` | 96 |
| all `scorer_feedback_events` awaiting source classification | 331 |
| canonical `user_reaction_event` revisions/heads | 0 / 0 |
| canonical `outcome_measurement` revisions/heads | 0 / 0 |
| canonical `cognitive_update_receipt` revisions/heads | 0 / 0 |

These are table-level source counts, not a deduplicated migration denominator.
Some tables contain unrelated scoring or recap history, and some rows are
multiple projections of the same legacy reaction. The reconciliation tool must
derive the reviewed object set from exact database/table/primary-key/schema
identity and publish its own inventory and object-manifest hashes. This design
does not add the counts above into a fabricated total.

The 94 legacy `cognitive_outcomes` include push feedback, search click, and
dialog reminder rows. They are operational history, not COG-037 objective
measurements. The 58 feedback signals include 44 rows without a source-event
identity. None may become an active canonical reaction or measured outcome by
matching subject text, action text, timestamps, labels, confidence, or table
proximity.

## Chosen architecture

Add one deep owner in `core/cognitive/feedback_attribution.py`, backed only by
the canonical `CognitiveStateStore` and `CognitiveStateUnitOfWork`. Focused
support modules may hold immutable models, materiality rules, target registry,
and receipt verification, but they must not create another database or mutable
owner.

The public API is cohesive and system-owned:

```text
FeedbackAttributionStore.record_reaction(input, principal) -> ReactionReceipt
FeedbackAttributionStore.correct_reaction(input, principal) -> CorrectionReceipt
FeedbackAttributionStore.record_objective_outcome(outcome_revision, principal) -> AttributionReceipt
FeedbackAttributionStore.reconcile_subject(subject_ref, now) -> AttributionReceipt
FeedbackAttributionStore.process_command(command_id) -> TargetDispositionReceipt
FeedbackAttributionStore.replay_pending(limit) -> ReplayBatchReceipt
FeedbackAttributionStore.verify(reaction_revision_id, principal) -> FeedbackVerification
```

Public callers provide observed facts and exact source references. They cannot
choose canonical IDs, polarity, attribution confidence, materiality,
independence, eligible targets, command identity, effect status, compensation
status, or training eligibility.

### Rejected approaches

Extending only `FeedbackEventLedger` in `delivery_events.db` is rejected because
it would make a delivery projection the cognitive owner and leave search,
reflection, scorer, and objective outcomes outside the state contract.

Keeping all current writers and adding a coordinating wrapper is rejected
because duplicate target effects, replayed negative trust, and competing receipt
owners would remain possible beneath the wrapper.

Closing only predictive push first is rejected because Context Search currently
writes direct ground truth and reflection/scorer paths still fan out without
causal attribution. That narrower result would not meet COG-038's all-entry-point
acceptance denominator.

## Canonical object contracts

### `mnemos.user_reaction_event.v1`

The existing inactive schema label is strengthened before any canonical
production reaction exists. Every active payload contains:

- `schema_version`, system-owned `reaction_id`, `revision_state`, immutable
  input hash, source event identity, observed/recorded timestamps, and exact
  supersedes/correction refs;
- principal and CognitiveAccess refs, project/session scope, source channel,
  interaction kind, and explicit-vs-implicit authority class;
- exact subject, decision, prediction, action, delivery, display, and search
  session/result refs where applicable; absent refs use a typed unavailable
  reason rather than a blank guessed identity;
- a typed signal vector that records only observed interaction facts, such as
  `accepted`, `ignored`, `dismissed`, `inaccurate`, `outdated`, `opened`,
  `clicked`, `dwell_observed`, or `silence_window_closed`;
- evidence refs and hashes, observation window, interface/exposure facts,
  competing-cause evidence, and a source-completeness declaration;
- attribution method/version/code/spec hash and conservative disposition; and
- downstream eligibility, exclusion reasons, required target set, and
  correction state.

Reaction identity is a chain, not one mutable row per principal/delivery. For a
single delivered item, an exact replay of the current action returns the current
revision. A different action requires `supersedes_event_id` naming the latest
reaction. A stale, missing, cross-principal, cross-subject, or non-current target
fails closed. Search interactions use the exact search-session/result/exposure
identity and may append multiple independently observed interactions; they are
not collapsed by text equality.

The contract has no generic numeric reward, expected score, actual label, or
objective-success field. Unknown actions are rejected as
`unsupported_reaction_kind`; they never default positive or negative.

### `mnemos.feedback_attribution_record.v1`

Add one separately versioned attribution object because attribution can change
as independent evidence accumulates without mutating the original observation.
It contains:

- canonical subject/scope and the exact current reaction and objective-outcome
  revision refs;
- an immutable input-set hash and ordered evidence-independence keys;
- attribution method/version/code/spec/config hashes;
- evidence class, materiality decision, observation count, distinct session and
  exposure counts, time-span proof, competing causes, and uncertainty;
- disposition: `record_only`, `proposal_eligible`, `objective_only`,
  `correction_pending`, `compensation_pending`, `superseded`, or
  `rejected`;
- the fixed target registry version, each target's eligibility/exclusion reason,
  and command refs; and
- exact supersedes/correction lineage.

The attribution record does not claim identified causality. `proposal_eligible`
means only that the evidence is strong enough to ask a domain owner and trusted
gate to consider a change.

Adding `feedback_attribution_record` requires a canonical cognitive-state schema
version bump. `core/cognitive/state_schema.py` remains the only DDL/version/hash
owner. Existing v2 databases fail closed until an explicit, backed-up schema
reconciliation installs the registered v3 signature; constructors cannot add the
new type or rewrite the registry on startup. The schema migration and the
history migration use separate reviewed plans and backups so a history failure
cannot disguise a schema failure.

### `CognitiveUpdateReceipt` public contract

The existing `mnemos.cognitive_update_receipt.v1` object type is already used as
a generic control revision by the data-ownership tombstone path. COG-038 does
not silently reinterpret or replace that persisted object. Instead, the public
feedback `CognitiveUpdateReceipt` is a deterministic typed DTO assembled from
the attribution revision, canonical outbox command,
`cognitive_state_effect_receipts`, `cognitive_data_consumptions`, and the
independently verified target-local receipt.

It contains the attribution revision/hash, target ID and target command hash,
material DecisionTrace/action refs when a state change is allowed, before/after
hashes, target-local effect receipt, reciprocal canonical refs, terminal
disposition, and any superseded/neutralized effect refs. It is not a second
persisted receipt owner.

Permitted terminal dispositions are:

- `committed_effect`: a proposal passed its domain/trusted/material-action gate
  and the target returned a reciprocal effect receipt;
- `proposal_committed`: the proposal itself was durably accepted, while a later
  domain decision remains separate;
- `intentional_skip`: the target registry proves the input was ineligible and
  before/after hashes are equal;
- `suppressed`: an earlier effect remains durably prevented from future use;
- `revoked`: an earlier reversible effect was removed with reciprocal proof;
- `compensated`: an irreversible earlier effect received the exact compensating
  effect and receipt; or
- `failed_terminal`: a permanent contract or target failure is preserved for
  audit and does not masquerade as success.

The DTO disposition is derived, not caller-selected. `intentional_skip`,
`revoked`, and `failed_terminal` map to the same canonical effect-receipt status.
`committed_effect`, `proposal_committed`, `suppressed`, and `compensated` use
canonical status `committed` plus an exact command `effect_kind`, changed
before/after hashes, and the matching target-local reciprocal receipt. This
keeps the existing state-schema receipt status authority intact while making the
domain result unambiguous.

Transport acknowledgement, a local receipt row, or caller-provided before/after
hashes cannot self-sign a committed effect.

## Signal taxonomy and learning gate

The owner uses a fixed `mnemos.feedback_materiality.v1` policy. Callers cannot
override the class, thresholds, weights, or target eligibility.

### Explicit correction

`inaccurate` and `outdated` are single-event high-priority proposal inputs only
when they come from the exact authorized user principal, bind the latest subject
and delivery/display identity, and contain an exact correction target. They may
trigger correction/suppression proposals, but they are not objective outcome or
automatic policy/persona/belief updates.

### Explicit preference reaction

`accept`, `ignore`, and `dismiss` record explicit interface reactions. They can
prove exposure to the exact item, but not usefulness, task success, durable
preference, trustworthiness, or prediction accuracy. A single event is
`record_only` unless it is an exact correction under the rule above.

### Weak behavioral observation

`opened`, `clicked`, `read`, dwell, repeated query, no-click, and silence are
weak observations. They never enter objective outcome or ground truth directly.
Weak-only proposal eligibility requires all of the following default global
conditions:

- at least three distinct canonical reaction events;
- at least two distinct session or exposure identities;
- observations spanning at least 24 hours; and
- no unresolved correction, access revocation, or evidence conflict.

The effective policy and its hash enter every attribution identity. Configuration
is global and registry-owned; there is no per-caller or per-operation override.
Meeting the threshold creates a proposal only. It does not create a training
label or direct domain update.

### Objective outcome

Only a COG-037 `OutcomeMeasurement` revalidated through `TaskResultOracle` can
carry objective metric truth. A reaction may prove presentation/exposure and can
be referenced by a prediction maturity decision, but its reaction value cannot
make a prediction `measured`.

## Fixed downstream target registry

The versioned registry defines these target classes:

- `delivery_state`: presentation/cooldown or explicit suppression preference;
- `trust_proposal`: evidence for a trust correction, never a direct score
  decrement;
- `policy_proposal`: bounded policy-patch proposal through the existing trusted
  and material-action gate;
- `persona_proposal`: scoped persona-preference proposal;
- `belief_correction_proposal`: exact belief correction/dispute proposal;
- `reflection_evidence`: attributed reflection evidence without automatic
  cognitive shift; and
- `training_evidence`: COG-048 input proposal, not a scorer label or model
  update.

Every attribution record evaluates the complete seven-target registry. Each
target must reach one typed terminal disposition. An ineligible target writes
`intentional_skip`; it is not omitted. Aliases cannot execute the same target
effect twice. A target not present in the registry fails before the attribution
record commits.

Domain owners remain authoritative. The feedback owner creates commands and
verifies receipts; it does not write policy, persona, belief, trust, scorer,
reflection, or delivery state directly.

## Atomic data flow

### Initial reaction

1. The application adapter resolves the server principal, CognitiveAccess,
   current subject, and exact delivery/display/search identity.
2. The owner normalizes only a registered reaction kind, derives the canonical
   reaction identity, and checks for exact replay or required supersession.
3. It creates the immutable reaction revision and a new attribution revision
   from the complete current evidence set.
4. One `CognitiveStateUnitOfWork` commits both revisions, one canonical feedback
   `CognitiveDataEvent` envelope listing both revision IDs, and one target
   command per registry target attached to the attribution revision before any
   downstream effect.
5. Target workers claim commands by stable command ID. Each target either
   commits a reciprocal proposal/effect receipt or a typed intentional skip.
6. The feedback owner verifies target-local state and commits the existing
   canonical effect/consumption receipt pair. It then returns the deterministic
   `CognitiveUpdateReceipt` DTO. Exact replay returns the same revision, command,
   persisted receipts, and DTO.

A crash before the unit-of-work commit leaves no reaction or target command. A
crash after commit leaves pending commands. A crash after target effect but
before canonical receipt is reconciled from the exact target-local receipt; the
effect is never run again merely because transport acknowledgement was lost.

### Objective outcome

The owner accepts only an existing canonical OutcomeMeasurement revision whose
Raw authority, TaskResultOracle issuance, subject, metric, window, attribution,
and reciprocal projection receipt revalidate. It appends a superseding
attribution revision and new eligible target commands. It never copies a legacy
`cognitive_outcomes.label`, `ground_truth_signals`, or reaction field into the
OutcomeMeasurement contract.

### Correction and compensation

1. A correction must name the latest reaction event and exact correction target.
2. The owner appends the corrected reaction and a
   `correction_pending` attribution revision; it never updates the old row.
3. The owner enumerates every canonical committed effect/proposal receipt
   derived from the superseded attribution and creates one deterministic
   revoke, compensate, or suppress command per effect.
4. Replacement target commands remain blocked until all required correction
   commands are terminal with reciprocal proof.
5. A partial failure leaves `compensation_pending`. Restart replays only missing
   commands. Exact correction replay creates no new event, proposal, effect, or
   compensation.
6. When all old effects are neutralized, the owner appends the final corrected
   attribution revision and may issue the replacement target set.

An irreversible training or optimizer effect cannot be deleted from history.
It requires a compensating record and must remain excluded from future active
training/model rebuilds. COG-048 will define the model-level application of that
exclusion, while COG-038 must produce and verify the correction receipt now.

Conflicting corrections without an exact latest-event supersedes ref are
rejected. Concurrent corrections use a head precondition; exactly one commits.

## Adapter cutover and removal of duplicate fanout

`IntelligenceApplicationService.push_feedback()` becomes a thin adapter. It
calls `record_reaction()` once and returns the canonical reaction,
attribution, command, and terminal receipt status. It no longer calls penalty,
`OutcomeRecorder`, adaptive scorer, delivery, or trust inline.

`KnowledgeDeliveryRouter.record_feedback()` becomes a target consumer for an
authorized `delivery_state` command. Its feedback-signal/trust bridge is removed.
`record_outcome()` no longer accepts reaction-derived labels; objective outcome
must enter through the COG-037 measurement seam.

`OutcomeRecorder` becomes a compatibility adapter with two explicit paths:

- reaction-like input is normalized into `record_reaction()` and never derives
  label, expected score, shift, optimizer outcome, or objective outcome;
- objective input requires an already committed OutcomeMeasurement and delegates
  to `record_objective_outcome()`.

The old ambiguous `record_outcome(source, subject, action, ...)` signature must
fail closed for new callers once every repository caller is migrated. A static
audit prevents new direct use.

`FeedbackSignalRouter` becomes a read-only legacy view or compatibility re-export
to the canonical owner. It does not own `feedback_signals.db` for new writes and
cannot call the trust scorer.

`core.scoring.feedback_channel.record_feedback_signal()` becomes a
`training_evidence` target adapter. It cannot call `AdaptiveScorerV2.feedback`
unless a COG-048 admission receipt exists; before COG-048 it returns a durable
proposal/intentional-skip receipt.

Context Search may continue recording operational session fields such as opened
or ignored timestamps, but only as a target projection from the canonical
reaction command. It removes direct `insert_ground_truth()` and direct
`OutcomeRecorder` calls. Search telemetry and training truth remain separate.

Reflection consumers may consume only an attributed `reflection_evidence`
command. Implicit feedback cannot directly create a cognitive shift, policy
patch, persona change, or scorer update. Existing domain-specific trusted and
material-action gates remain required.

`DialogDecisionPush` and other direct `FeedbackSignalRouter` callers also switch
to `record_reaction()` or a registry target command; no trust/policy surface may
retain a private signal fanout. The already append-only recap correction owner
keeps its domain event chain, but any bridge from recap feedback into scorer,
trust, policy, persona, or reflection must consume the canonical attribution
command and may not replay the recap event as a second reaction.

After all callers switch, the old mutable owners and duplicated fanout code are
removed. Compatibility modules may retain read-only parsers/re-exports for
historical access, but cannot silently create schemas, write legacy tables, or
fall back to the global `~/.mnemos` path in isolated tests.

## Object-level provenance migration

Add `scripts/reconcile_feedback_attribution_history.py`. Dry-run is the default
and must be safe while current writers exist. Apply/restore require a reviewed
inventory, an explicit backup directory, a stopped daemon, and an exclusive
feedback migration barrier honored by every application/MCP/CLI feedback
entrypoint.

Daemon shutdown alone is not enough: an MCP server or foreground CLI can still
write a feedback database. Apply therefore enables the canonical migration
barrier first, verifies that all feedback entry points fail closed with
`feedback_migration_in_progress`, obtains an exclusive per-database lock, and
then freezes the inventory. If any writer advances a source database revision or
WAL snapshot, apply aborts before target mutation.

The reviewed inventory has three source domains:

1. `delivery_feedback`: delivery feedback/event/receipt, feedback signal,
   operational cognitive-outcome, and outcome-projection rows;
2. `scoring_search`: search session interaction, matching search click/ignore
   ground-truth rows, source-attributed scorer training, and scorer feedback
   rows; and
3. `reflection_optimizer`: outcome-feedback Layer5/shift rows and source-matched
   rule outcome/optimizer rows.

Every object identity contains source database class, table, primary key, schema
fingerprint, stable source-event/foreign-key refs when present, row hash, and the
exact projection-link set. Sensitive subject/query/evidence/metadata bytes enter
only the content hash and sealed local backup, not the public report. This Root
does not add or claim a new database-encryption or key-management layer.

Apply creates one `historical_unattributed_feedback` quarantine object per exact
source object. Exact legacy edges may be preserved as provenance edges, but
multiple projections are not collapsed into a single invented reaction. Apply
does not create active reaction heads, attribution heads, objective measurements,
target commands, training evidence, corrections, or compensation.

The tool writes a content-addressed inventory hash and object-manifest hash,
uses the SQLite backup API for every source/target database, verifies
`PRAGMA integrity_check`, and records source/backup/target logical snapshot
hashes. Restore consumes only the sealed manifest and must reconstruct the exact
preimage. Reapply and second apply must converge with zero duplicate quarantine
objects. Inventory drift, unknown schema, missing primary identity, source
mutation, backup failure, active writer, orphan projection, or integrity failure
aborts or rolls back without guessing.

## Error handling and replay

Malformed identity, unknown reaction kind, stale correction, ACL drift, source
scope mismatch, target-registry drift, code/spec/config hash drift, missing
DecisionTrace for a material target, or non-reciprocal target receipt is a
permanent typed failure. It cannot be retried indefinitely or converted to
success by an allowlist.

Only explicit transient SQLite lock/busy and declared transport-unavailable
conditions are retryable. A retry preserves the same command ID and input hash.
Permanent consumer errors become `failed_terminal`; a correction can supersede
them only through the normal append-only chain.

Command replay is bounded but complete. Batches use stable keyset pagination,
not a fixed first-page limit. Tests must prove exact convergence above 10,000
reaction, target, and compensation commands.

Read-only health/status/audit paths do not initialize schemas, create directories,
advance heads, write metrics, or repair rows. Constructors validate an existing
canonical schema before any other DDL and fail closed on unknown legacy layout.

## Independent strict audit

Add `scripts/audit_feedback_attribution.py --strict --json`. It must open source
and target stores read-only and independently recompute identities, payload
hashes, evidence independence, materiality, target eligibility, current heads,
command hashes, target-local state, reciprocal receipts, correction coverage,
and historical manifest coverage.

The strict report must expose at least these acceptance metrics, all zero:

- `feedback_without_subject`;
- `unknown_action_default_positive`;
- `reaction_used_as_objective_ground_truth`;
- `duplicate_training_effect`;
- `replayed_negative_trust_effect`;
- `auto_update_from_weak_single_signal`;
- `committed_effect_without_attribution`;
- `reaction_target_effect_duplicate`;
- `correction_without_latest_supersedes`;
- `correction_effect_without_neutralization_receipt`;
- `replacement_effect_before_compensation_complete`;
- `target_receipt_reciprocity_gap`;
- `feedback_owner_bypass`;
- `legacy_feedback_object_uncovered`;
- `historical_quarantine_promoted_active`;
- `active_feedback_without_current_attribution`;
- `feedback_schema_registry_mismatch`; and
- `feedback_migration_barrier_bypass`.

The report also publishes exact denominators for formal entry points, reaction
revisions/heads, attribution revisions/heads, complete-registry target commands,
terminal dispositions, correction commands, neutralized effects, legacy source
objects, quarantine objects, and active objective outcomes. Counts are derived
from the independent object set, not copied from the implementation report.

The audit is added to local gates, pre-commit, CI, full-score required gates,
and the document/contract acceptance assets. A passing target audit does not
hide unrelated global maintainability, zombie, security, runtime, or release
failures.

## Test and verification matrix

### Contract and unit coverage

- every registered reaction kind and rejection of unknown/default-positive;
- exact replay, required supersedes, stale/cross-principal/cross-subject
  correction, and concurrent head conflict;
- explicit correction, explicit preference, weak behavior, and objective
  measurement separation;
- weak threshold boundaries: 2/3 events, 1/2 sessions, 23/24 hours, duplicate
  exposure, unresolved conflict, and config hash change;
- target-registry completeness, alias dedupe, intentional skip, and unknown
  target rejection;
- payload/hash/access-control/source-authority tamper for all three canonical
  object types; and
- objective OutcomeMeasurement revalidation through the fixed COG-037 oracle.

### End-to-end entry-point coverage

- predictive `accept`, `ignore`, `dismiss`, `inaccurate`, and `outdated`;
- Context Search open/click/ignore/no-click/silence without direct ground truth;
- reflection explicit and implicit feedback without direct shift/policy update;
- late objective outcome after a prior reaction;
- one reaction observed by multiple old adapters creates one canonical event and
  one target command per registry target;
- a single weak event creates only record/skip receipts and no material effect;
- an eligible proposal still passes DecisionTrace, trusted gate, and target-local
  reciprocal receipt; and
- isolated paths use injected state/delivery/scoring/reflection/optimizer
  databases and never fall back to production.

### Crash, correction, and replay coverage

Inject failure before/after reaction revision, attribution revision, outbox
command, target claim, target effect, target-local receipt, canonical receipt,
correction plan, each compensation target, and replacement activation. Cold
restart must replay only the missing step. Exact replay adds zero rows/effects.

Cover one irreversible scorer/optimizer history effect, one reversible delivery
state effect, and one suppressible trust/policy/persona/belief proposal. The
correction is complete only when every prior effect has the correct terminal
neutralization receipt.

Run terminal/proposal/compensation replay with more than 10,000 commands and
prove no first-page starvation or duplicate effect.

### Migration coverage

- fresh, partially initialized, and all current legacy schema variants;
- unrelated scorer/recap/rule rows excluded by exact source classification;
- rows without source-event refs retained as separate unattributed objects;
- duplicate-looking projections preserved by primary identity without semantic
  collapse;
- inventory/object-manifest drift, active writer, WAL advance, missing PK,
  corrupt JSON/metadata, unknown schema, orphan projection, and backup failure;
- apply failure rollback, sealed restore, reapply, second zero-change replay,
  source/backup/target integrity, and logical snapshot equivalence; and
- canonical active reaction/outcome/update counts remain zero after history-only
  migration.

### Repository verification

Run focused unit/integration tests first, then the complete Quick, Integration,
and Heavy denominators through `HermeticRunEnvironment`. Run strict feedback,
prediction, DecisionTrace, source-authority, schema-registry, trusted-push,
architecture, test-denominator, document-asset, freshness, sensitive-info, and
generated-map gates. Final Quick evidence must report
`outside_write_count=0` and `formal_state_diff=[]`.

Review the final change along both specification and repository-standards axes.
Do not weaken tests, accept new broad exceptions, increase maintainability
budgets, add waivers, preserve a caller-selectable bypass, or claim global
standards/release closure from a COG-038-scoped result.

## Completion criteria

COG-038 is `CLOSED_ROOT_VERIFIED` only when all of the following are proven on
the same committed code tree:

- one canonical feedback owner and zero formal entry-point bypasses;
- every reaction has exact subject/decision/prediction/action/display refs or a
  typed unavailable reason, with no guessed identity;
- reaction and objective outcome are separate at contract, storage, API, audit,
  and training boundaries;
- single weak signals produce no material update;
- every registry target has exactly one terminal typed receipt;
- exact replay adds no reaction, training, trust, policy, persona, belief,
  reflection, optimizer, or delivery effect;
- opposite feedback forms a latest-event correction chain rather than conflict;
- every previously committed effect is revoked, compensated, or suppressed
  before replacement activation;
- the strict audit's complete metric set is zero;
- the reviewed three-domain historical inventory is fully quarantined through
  backup/apply/restore/reapply/replay with uncovered=0 and active promotion=0;
- focused and full test/gate evidence passes with hermetic outside-write and
  formal-state-diff counts zero;
- repo docs, Desktop `mnemos系统图谱`, and the original cognitive audit section
  are updated in place with exact commit, inventory, manifest, backup, test, and
  remaining-release-boundary evidence; and
- code and documentation changes are committed locally.

This closure does not by itself close COG-048 training governance, the remaining
Phase 3 roots, continuous daemon/runtime effects, global maintainability/zombie
debt, full-score certification, or release eligibility.

## 2026-07-18 closure evidence

The production cognitive state ledger was explicitly migrated from the v2
registry to canonical `mnemos.cognitive_state_store.v3` while the daemon and all
Mnemos MCP servers were stopped. The reviewed three-domain legacy inventory was
3,625 objects with inventory hash
`sha256:0b9854759e4ea51696063152c32caea635f18388e212b0f7a55dd53a70569b15`
and object-manifest hash
`sha256:6c307444608e13a6d39330b3b2eb86b983599b1ae1a2c4f7589604a5d490806b`.

The exact production sequence was apply, strict audit, manifest restore,
preimage verification, reapply, and idempotent replay. The final replay reported
`inserted=0`, `existing=3625`; final coverage was `covered=3625`,
`uncovered=0`, `unexpected=0`, `active_promotion=0`, with zero active-head and
active-revision delta. The sealed backup root is
`~/Desktop/Mnemos-migration-backups/COG-038-20260718`; the first
restore-tested manifest hash is
`sha256:13f38f62d68a93cd74757e2baa164adde6fe7b6c37b20b17fdeeee90aabd3891`.
All six source/target databases passed `PRAGMA integrity_check`; the strict
feedback attribution audit had no findings and every metric was zero.

The implementation also closes the final review findings: dialog decisions
fail closed on absent or mismatched proposal session/agent scope; every backup
manifest seals the complete six-class denominator including explicit absent
entries; weak-signal proposal eligibility requires at least three observations,
two independent session/exposure identities, and a 24-hour span; every eligible
proposal is gated by the real trusted decision gate plus exact DecisionTrace and
material terminal proof; and Context Search returns the complete canonical
feedback DTO. The final review follow-up also requires writer and independent
audit resolution of every available decision/prediction/action ref against its
canonical revision or action spec, including prediction principal/project/
session and prediction-to-decision/action binding. Project-scoped weak evidence
may use independent exposure identities when its scope intentionally has no
session ID; an available but nonexistent causal ref fails closed. Both the
specification and repository-standards review axes ended with zero hard
findings; the only remaining judgment item is the reviewability of the large
fixed-point diff. Focused and expanded regression evidence includes 50, 72,
and 235 passing tests, plus the 1,501-object capacity gate. Final
clean-commit hermetic evidence is Quick `6822 passed in 1302.29s` with
environment hash
`04cb169670cbe287264fb271c4a11fde8bc876a5771428bd34ea8054548185b6`,
`outside_write_count=0`, and an empty `formal_state_diff`; Integration passed
350 tests in 167.48s with environment hash
`1db35bced3b284ac62b5eb16401accab8daaa5e71568759bf49f37e75849bfbc`,
and Heavy passed 19 tests in 205.81s with environment hash
`89de8fcb318db3310429795f2766545ff32b45f2451187fca07b9698b07e8fb7`.
A final regression also proves that retired
`outcome_feedback` objects stay quarantined while an ordinary Layer-5 object
with non-empty source lineage remains active. A clock-advance replay regression
also proves that an exact, independently verified domain/material terminal is
returned before constructing another DecisionTrace; replay time cannot change
the immutable command result. This remains a COG-038-scoped closure, not a
release certificate.
