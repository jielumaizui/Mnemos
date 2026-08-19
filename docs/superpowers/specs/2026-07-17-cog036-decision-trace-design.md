# COG-036 canonical DecisionTrace and material-action design

**Status:** approved for implementation on 2026-07-17. This specification
closes the design gate only. COG-036 remains open until the implementation,
production migration, strict audit, tests, documentation, and local commit are
all complete.

## Scope and completion boundary

This slice makes `CognitiveStateStore` the only canonical owner of runtime
decisions and the values and state that governed them. It must prove, before a
material action starts:

- which task, goal, constraints, evidence, beliefs, profile revisions, and
  policy revisions were consumed;
- which value sources were active, how fixed precedence resolved conflicts,
  and whether each value changed the selection;
- which candidates were actually compared and why non-selected candidates
  were rejected;
- which model, tool, and prompt specifications participated;
- which candidate was selected, who or what approved it, what outcome was
  expected, and when it should be evaluated; and
- which exact action command was authorized and which terminal effect receipt
  later closed it.

The slice covers outward delivery, formal Markdown and non-Markdown writes,
policy and persona updates, scheduler creation and execution, auto-healing,
and any other action that changes durable state or later system behavior.
Unknown action classes are material by default and fail closed.

It does not save private chain of thought, claim that a routed delivery was
displayed, implement calibrated predictions, or reinterpret feedback as an
objective outcome. Presentation acknowledgement remains COG-039;
PredictionRecord and calibration remain COG-037; reaction/outcome attribution
remains COG-038; trustworthy learning remains COG-048.

## Current-state evidence

The existing `CognitiveStateApplicationService.record_decision()` is a
placeholder. A caller can currently choose `decision_id` and submit generic
`value_context.payload`, `snapshot.payload`, and `decision.payload` objects.
The service links the three generic revisions and commits them, but it does not
derive identities, compile fixed value precedence, freeze the exact consumed
state, enforce head consistency, authorize an action before execution, or
require a reciprocal terminal effect.

The production inventory inspected on 2026-07-17 contained three legacy
decision/action domains:

| Source | Object identity | Inspected count |
| --- | --- | ---: |
| `action_ledger.db.action_ledger` | `action_id` | 1,966 |
| `delivery_events.db.delivery_events` | `event_id` | 2,226 |
| `trusted_push.db.formal_cognitive_mutations` | `event_id` | 142,676 |
| **Total** | exact source primary key | **146,868** |

These schemas do not contain an action-before canonical state snapshot,
ValueContext revision, candidate set, or same-transaction DecisionTrace
receipt. Metadata or a final reason string cannot repair that absence. The
current production rows therefore cannot be promoted to live DecisionTrace
revisions and must migrate object by object as `historical_incomplete`.
Counts are evidence for the inspected snapshot, not a hard-coded migration
denominator; apply must bind a newly reviewed inventory hash.

## Chosen architecture

Add one deep module, `core/cognitive/decision_trace.py`. It owns the decision
domain rules while persisting all canonical revisions through the existing
`CognitiveStateStore` and `CognitiveStateUnitOfWork`. It introduces no second
decision database and no mutable action ledger.

The module exposes two cohesive interfaces:

1. `DecisionTraceStore` compiles and atomically seals ValueContext,
   CognitiveStateSnapshot, DecisionTrace, the semantic event, and local action
   commands.
2. `MaterialActionCoordinator` resolves an already committed command into a
   typed permit, validates the permit at the sink before an effect, and records
   an exact reciprocal terminal receipt afterward.

The public application facade delegates to this module. ActionLedger,
DeliveryRouter, Trusted Push journals, FormalCognitiveMutationJournal, and
subsystem databases remain projections or effect owners; none may store a
second canonical DecisionTrace.

### Rejected approaches

- Extending only the existing application method would leave DeliveryRouter,
  Trusted Push, PolicyPatch, Persona, Scheduler, and Auto-heal able to bypass
  the facade.
- Requiring an ActionLedger evidence string would be post-hoc logging. It
  cannot prove that the decision preceded the action and does not cover formal
  writes that never enter ActionLedger.
- Giving each subsystem its own decision table would create multiple owners,
  inconsistent value precedence, and irreconcilable replay semantics.
- Preserving a compatibility mode that accepts missing permits would make the
  activation marker cosmetic and is explicitly forbidden.

## Domain types and public operations

`core/cognitive/decision_trace.py` defines immutable command and receipt types:

- `DecisionCandidate`
- `ValueSourceInput`
- `DecisionTraceCommand`
- `MaterialActionSpec`
- `DecisionSealReceipt`
- `MaterialActionPermit`
- `MaterialActionTerminal`
- `MaterialActionReceipt`

The authoritative operations are:

```text
DecisionTraceStore.seal(command, principal) -> DecisionSealReceipt
DecisionTraceStore.verify(decision_revision_id, principal) -> DecisionVerification
MaterialActionCoordinator.authorize(command_id, executor_id) -> MaterialActionPermit
MaterialActionCoordinator.record_terminal(permit, terminal) -> MaterialActionReceipt
```

The existing application facade keeps `build_cognitive_state()` and
`record_decision()`, but `record_decision()` accepts the typed semantic command
rather than caller-authored canonical payloads. Internal action consumers use
the coordinator operations. Callers cannot supply decision, snapshot,
ValueContext, action, effect, revision, or content-hash identities.

## System-owned identity and replay

The caller supplies an idempotency key, an authorized source envelope, scoped
task input, candidate content, and requested action semantics. The system
derives:

- `decision_id` from scope, source revision/content hash, idempotency key, and
  the canonical semantic input;
- `value_context_id` from scope and the code-owned value-context kind;
- `snapshot_id` from the canonical snapshot payload hash;
- candidate IDs from canonical candidate content;
- action IDs from decision identity, action type, owner, target binding, and
  input hash; and
- effect IDs and command IDs from the committed decision revision and action
  identity.

Exact replay returns the original receipt and creates no revision, head,
event, command, or effect row. Reusing an idempotency key with different
semantic content raises an immutable conflict. A model, tool, prompt, source,
candidate, value, target, or expected-outcome change produces a different
semantic identity rather than silently reusing the old decision.

## `mnemos.value_context.v1`

ValueContext is an immutable scoped revision compiled by system code. It uses
the fixed precedence contract `mnemos.value_precedence.v1`:

1. safety, permission, and privacy hard constraints;
2. the user's current explicit goal;
3. project constraints;
4. evidence-backed, scope-matching preferences; and
5. cost and convenience.

Callers cannot reorder categories or upgrade a source. Unknown or detached
preferences cannot outrank an explicit user goal. A lower-precedence item can
refine a higher-precedence item only when it does not conflict with it.

The payload contains:

- system-owned ValueContext ID, scope, revision, and precedence contract;
- `valid_from` and optional `valid_until`;
- one structured item per consumed value with category, normalized
  constraint, exact source ID/revision/content hash, source-authority ID,
  evidence refs, scope, valid time, and `changed_decision`;
- detected conflicts, winning and losing item refs, and a structured
  disposition code;
- exact consumed refs and their independently recomputable hash;
- the superseded ValueContext revision, when one exists; and
- the strictest derived CognitiveAccess envelope.

The outer CognitiveStateRevision remains authoritative for source, evidence,
scope, ACL, supersedes lineage, payload hash, and revision number. Payload and
outer fields must agree. Missing source revisions, expired values, malformed
ACLs, or unresolved hard-constraint conflicts fail before any write.

## `mnemos.cognitive_state_snapshot.v1`

`build_cognitive_state()` remains a zero-write, ACL-first read model. At
decision start, `DecisionTraceStore.seal()` independently rebuilds that state
inside the decision path and freezes a full canonical snapshot rather than
accepting a caller-provided hash.

The snapshot payload contains:

- task, goal, constraints, scope, and source completeness;
- the full authorized consumed revision entries for evidence, active beliefs,
  profile, and policy, including object type/ID, revision ID, schema version,
  readable payload, payload hash, evidence hash, and ACL hash;
- exact belief, profile, policy, and ValueContext revision refs;
- the head preconditions observed while building the snapshot;
- a canonical state hash; and
- the strictest access envelope inherited from every consumed source.

`snapshot_id` is content addressed. The stored `snapshot_hash` is recomputed
from the canonical payload with its self-hash field excluded. Reading a
snapshot must hydrate the full payload and reproduce both the payload hash and
snapshot hash. A dangling hash is never accepted as evidence.

To close the read/commit race, `CognitiveStateUnitOfWork.commit()` gains typed
expected-head preconditions. After `BEGIN IMMEDIATE` and before any insert, it
checks that every consumed current revision is still the current head. Drift
rolls back the entire decision; the caller must rebuild rather than seal a
stale snapshot.

## `mnemos.decision_trace.v1`

The DecisionTrace payload contains:

- task, goal, constraints, scope, and decision state;
- snapshot ID, snapshot revision ID, and snapshot hash;
- ValueContext ID, revision ID, and payload hash;
- exact belief, profile, and policy revision refs;
- at least two genuinely evaluated candidates for an approved material
  decision, with system-derived candidate IDs;
- support and opposition evidence refs per candidate;
- structured rejection reasons for every non-selected candidate;
- hard-constraint violations and the ValueContext item refs that caused them;
- exact model provider/model/route, tool name/version, prompt ID/hash, output
  schema/hash, and relevant effective configuration hash;
- selected candidate ID, expected outcomes, evaluation window, and explicit
  uncertainty;
- structured approval mode, decision, actor/ref, and timestamp; and
- one or more typed MaterialActionSpec objects with owner, executor, action
  type, target binding, input hash, rollback contract, and expected effect.

A `no_action` candidate may be present only when it was actually evaluated in
the live decision input. It may not be fabricated during historical
migration. A selected candidate with any unresolved safety, permission, or
privacy violation is invalid regardless of preference or cost. A rejected
decision has no executable action command and is terminal as rejected; it is
not counted as a decision missing an action effect.

The payload stores structured facts and compact public rationale codes. It
must not contain hidden reasoning, scratchpad text, model chain of thought, or
unbounded prompt transcripts.

## Atomic seal and failure behavior

One `BEGIN IMMEDIATE` transaction commits, in order:

1. the ValueContext revision and head;
2. the content-addressed snapshot revision;
3. the DecisionTrace revision;
4. the CognitiveDataEvent envelope; and
5. one local `execute_material_action` command per selected action.

Each action receives a unique consumer ID derived from owner and action ID so
multiple actions for one decision do not collapse into one consumer-pair
terminal. The event's intended consumer set must exactly equal the command
consumer set.

Failure injection points are required after the expected-head check, each of
the three revisions, the event, and every outbox command. Any exception before
commit leaves zero new semantic revision, head, event, or outbox row. No
cross-database target participates in a fake distributed transaction.

## Material-action permit and effect closure

`MaterialActionCoordinator.authorize()` reads only a committed
`execute_material_action` command. It verifies that:

- DecisionTrace, snapshot, and ValueContext revisions all exist and their
  hashes recompute;
- the command is bound to the decision revision, action ID, owner, executor,
  action type, exact target, input hash, and effect ID;
- fixed-precedence hard constraints are satisfied;
- the command has no terminal receipt; and
- activation is enabled for the sink.

It returns an immutable `mnemos.material_action_permit.v1`. Every material sink
must validate this permit immediately before effect execution. A permit cannot
be reused for a different target, body, executor, action, or effect.

After execution, the sink records one terminal receipt bound to the command:

- `committed` requires exact before and after hashes plus independent effect
  evidence;
- `failed_terminal` requires a failure code, unchanged-state oracle or
  rollback evidence, and the attempted effect ref;
- `rejected` and `revoked` prove that the effect did not run;
- `dead_letter` is terminal only after the retry budget is exhausted; any
  later attempt requires a new, explicit decision that supersedes the failed
  decision rather than silently reopening or replacing its action identity;
  and
- `intentional_skip` requires an explicit approved skip reason and evidence.

The receipt adds reciprocal refs to the cognitive consumption ledger. A crash
after the target effect but before the receipt leaves the command pending.
Recovery asks the idempotent target for the exact effect ID and records the
observed before/after state; it must not execute an unbounded duplicate.

## Activation and sink coverage

Activation is a durable canonical marker, not a caller flag. Fresh stores are
strict by default. An existing store without the marker reports
`migration_required` and refuses material actions; it never falls back to a
legacy permissive path. The reviewed reconciliation apply writes the marker
in the same target transaction as the historical inventory receipts, so there
is no unguarded activation gap.

After activation, the following central seams fail closed without a valid
permit:

- `KnowledgeDeliveryRouter` for an outward deliver command; the router may not
  claim native presentation, and the adapter owns the later transport effect;
- `TrustedVaultMutationService` plus all typed Markdown commit helpers;
- `KnowledgeVaultWriter` for approved trusted proposals;
- `PolicyPatchStore` for activation, suppression, or behavior-changing
  feedback;
- the Persona persistence owner for profile version and formal persona writes;
- Chronos task creation and each scheduled step execution;
- `AutoHealingOrchestrator` before invoking an auto-fix handler;
- `ActionLedger.record()` for material action types; and
- `FormalCognitiveMutationJournal.record()` for material non-Markdown
  mutations.

ActionLedger and FormalCognitiveMutationJournal validate the already committed
decision/action binding; they never synthesize a DecisionTrace after the fact.
Pure observation or diagnostic records are non-material only when their
canonical type proves they cannot mutate durable user/system state. Unknown
types are material. There is no config, environment variable, test-only flag,
expiry registry, or caller-selectable bypass.

An AST-based strict audit owns the exact production sink denominator. It must
prove that every material sink is permit dominated and that every permit
originates from the canonical coordinator. Directory markers, comments,
generic helper names, and same-file allowances do not count.

## Three-domain object-level migration

Add `scripts/reconcile_decision_trace_history.py`. Dry-run is the default. It
inventories the three legacy source tables using source database identity,
schema fingerprint, table, primary key, immutable content hash, created time,
and any existing typed provenance envelope. It does not parse targets, reason
strings, metadata prose, or actor names to invent scope, candidates, values,
predictions, or approval.

Each exact legacy object is written to the existing cognitive-state migration
quarantine with:

- target type `decision_trace`;
- status/reason `historical_incomplete`;
- source database/table/primary key;
- source schema fingerprint and object content hash;
- exact available provenance refs; and
- a deterministic migration identity.

No migrated row creates a ValueContext, snapshot, DecisionTrace revision,
active head, action command, or terminal effect. A legacy row can be recognized
as already linked only when it references an independently existing canonical
pre-action DecisionTrace and all hashes resolve; the migration still does not
reconstruct that trace.

Apply requires:

- a stopped daemon verified from the process-control owner;
- an explicit backup directory;
- the exact reviewed dry-run `inventory_hash`;
- unchanged source schema fingerprints, row identities, and content hashes;
- SQLite integrity checks for all sources and the target;
- a SQLite backup of the only modified target database before write; and
- an exact before/after report including activation marker state.

Source drift fails before backup or write. The second apply returns zero
insertions and all objects as existing. Rollback restores the verified target
backup; it never deletes individual canonical rows heuristically. Production
source databases are read-only to this migration.

## Strict audit

Add `scripts/audit_decision_trace_effects.py --strict --json` with report schema
`mnemos.decision_trace_effect_audit.v1`. The auditor independently rebuilds
DecisionTrace, ValueContext, snapshot, command, and effect denominators without
calling the implementation's success predicate.

The governing metrics must all be zero:

- `decision_without_action_terminal`
- `action_without_decision`
- `decision_without_value_context`
- `value_context_revision_missing`
- `decision_snapshot_unresolvable`
- `snapshot_hash_mismatch`
- `value_ref_missing`

The strict report also fails on:

- material sink bypass;
- action/effect/target/executor permit mismatch;
- action timestamp or commit-order evidence preceding its decision;
- hard-constraint override;
- unresolved head-precondition drift;
- multiple terminal receipts for one command;
- non-reciprocal action/effect evidence;
- historical-incomplete objects entering an active head;
- active stores without the strict activation marker; or
- decision payloads containing prohibited private-reasoning fields.

The audit is read-only in empty, legacy, production, and hermetic environments.
Missing stores report `not_initialized`; they do not create directories, DDL,
or metrics.

## Test matrix

Focused tests use the public module/application seams and cover:

1. a multi-candidate approved decision and exact replay;
2. safety, permission, and privacy hard-constraint vetoes;
3. explicit user goal versus conflicting scoped preference;
4. project constraint versus cost/convenience;
5. human approve and human reject;
6. model, tool, prompt, schema, and effective-config version changes;
7. action success, terminal failure, rollback, revoke, skip, and dead letter;
8. restart replay and concurrent same-idempotency-key execution;
9. snapshot deletion, payload tampering, and hash mismatch;
10. missing or foreign ValueContext and consumed revision refs;
11. permit replay against a different target, executor, body, or effect;
12. crash injection at every expected-head/revision/event/outbox boundary;
13. crash after target effect but before receipt plus idempotent recovery;
14. ACL denial before payload hydration and strictest-ACL inheritance;
15. strict activation at every named material sink; and
16. three-domain dry-run/apply/replay/source-drift/backup/rollback migration.

Tests must not weaken existing trusted-push receipts, use a caller-controlled
bypass, fabricate historical candidates, or rely on a real model, credential,
network, home directory, or production database.

## Gate and documentation integration

Once implementation evidence is green, add the strict audit to local gates,
pre-commit, CI, and full-score under a dedicated
`contracts.decision_trace_effects` gate. Update the application contracts,
architecture/event generated documents, OPS manual, CHANGELOG, document asset
manifest, Desktop `00–10` current claims, generated `86–99` facts, and the
governing COG-036 section in place.

Root verification requires focused tests, the strict audit, isolated Quick,
the relevant integration suite, document/sensitive/freshness audits, source
database and target integrity checks, migration replay zero-change, and a
clean local commit. Global maintainability, zombie, real-API, or release
certificate debt remains separate and must not be represented as a COG-036 or
release pass.

## Completion criteria

COG-036 is closed only when every active material action can be traced backward
to a committed, readable, hash-recomputable DecisionTrace, snapshot, and
ValueContext and forward to exactly one terminal action effect; every named
metric is zero; every production material sink is strict; the reviewed
historical inventory is quarantined without invented cognition; all required
tests and gates pass; all formal documents are synchronized; and the verified
tree is locally committed.
