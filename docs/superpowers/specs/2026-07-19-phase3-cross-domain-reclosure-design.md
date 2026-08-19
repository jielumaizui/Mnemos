# Phase 3 cross-domain aggregate reclosure design

## Status and decision

**Status:** approach A was approved by the user on 2026-07-19. This document
closes the cross-domain design gate only. Phase 3 remains
`REOPENED_CROSS_DOMAIN_BLOCKED` until implementation, controlled production
reconciliation, strict aggregate audit, regression suites, independent review,
documentation synchronization, and local commits are complete.

This corrective specification is bound to repository commit
`0dbbd5111a46ff611ab31179e1ced543d459e67f`. It supplements and, where the
contracts conflict, supersedes these earlier Phase 3 specifications:

- `2026-07-17-cog035-belief-revision-design.md`;
- `2026-07-17-cog036-decision-trace-design.md`;
- `2026-07-18-cog038-feedback-attribution-design.md`; and
- `2026-07-19-cog048-training-governance-design.md`.

The earlier single-domain closure evidence remains valid. It does not prove
that the domains compose. The current governing requirement is one fault-safe
canonical chain:

```text
Belief -> Decision -> Prediction -> Outcome -> Feedback -> Training -> Model
```

All Mnemos daemons and MCP servers remain stopped throughout design,
implementation, production reconciliation, and verification unless the user
separately authorizes a restart.

## Confirmed defects

The cross-domain audit reproduced four runtime defects and one acceptance-
evidence defect.

| Severity | Boundary | Confirmed failure |
| --- | --- | --- |
| P0 | Belief -> Decision | A canonical Belief permits `belief_read`, while Decision snapshot construction requests `cognitive_state_read`. Exact-purpose ACL authorization rejects the Belief even for the same principal, project, and scope. |
| P1 | Feedback -> Training | Feedback commands can reach terminal receipts before the synchronous admission call. A crash in that interval leaves no pending work that the current training reconciler owns. |
| P1 | Outcome maturity -> Training | Admission reissues the outcome but does not compare the governance clock with `matured_at` or require the current Prediction head to be terminal and measured. It then writes `maturity=mature` unconditionally. |
| P1 | Outcome correction -> Training/Model | `correct_admission()` has no production caller. A corrected outcome follows normal admission, leaving the superseded sample active and dependent runs/models unstaled. |
| P2 | Admission -> Model evidence | The positive scorer-v2 integration test fabricates 24 admission revisions whose upstream Prediction and Outcome revisions do not exist. Runtime verification does not reject those refs. |

The P0 reproduction returned `candidate_count=1`, `authorized_count=0`, and
`purpose_not_permitted=1`; the sealed Decision contained no Belief refs. The
feedback crash reproduction retained the feedback effect receipt but had zero
pending feedback commands and zero admissions before and after restart
reconciliation. A future-maturity reproduction admitted an Outcome while its
Prediction remained `open`. A correction reproduction left both old and new
samples current and admitted.

The current production training audit remains a valid historical-quarantine
audit: 25,139 of 25,139 legacy objects are covered, active admissions and
models are both zero, and both canonical schemas validate. It is nevertheless
false reassurance for this problem because its formal-producer denominator is
static and it does not measure a feedback-command-to-admission obligation.
Read-only production inspection at design time found zero current
`training_evidence` commands and zero training-admission revisions, so the new
intake reconciliation is expected to be a verified no-op on this production
snapshot rather than a semantic backfill.

## Scope and completion boundary

This repair owns only the contracts required to make the Phase 3 domains
compose:

- object-type-specific authorization for Decision snapshot inputs;
- a durable admission-intake outbox committed with objective attribution;
- restart-safe admission and correction dispatch;
- maturity and current terminal-Prediction enforcement;
- exact correction, exclusion, stale, and rebuild propagation;
- independent upstream-chain verification before a training run;
- removal of fabricated admissions from the positive model test;
- a strict aggregate Phase 3 audit and gate; and
- controlled reconciliation of any pre-contract intake gaps.

It does not broaden generic cognitive read access, infer historical cognition
or training truth, reclassify the 25,139 quarantined objects, restart services,
run a real-API model campaign, repair Phase 4-7 roots, or issue a release
certificate.

## Non-negotiable invariants

1. Single-domain audit success is never sufficient for Phase 3 aggregate
   closure.
2. Decision may read a source only through a code-owned, object-type-specific
   purpose contract. Callers cannot select a purpose or object-type mapping.
3. Purpose translation cannot widen owner, private principal, agent, project,
   session, visibility, consent, sensitivity, or retention boundaries.
4. A feedback attribution that can eventually create a training admission
   commits that admission obligation in the same SQLite transaction as the
   attribution and its target commands.
5. Admission replay accepts no caller-supplied label, feature, eligibility,
   split, principal, scope, maturity, or correction mode.
6. A training intake remains pending with zero admission writes until its full
   feedback target receipt manifest is terminal and valid.
7. An Outcome is admissible only when the governance clock is at or after its
   exact `matured_at`, the Outcome is current, and the current Prediction head
   is terminal `measured` with the exact Outcome binding.
8. The admission feature snapshot remains bound to the immutable pre-outcome
   sealed Prediction revision; the current terminal Prediction is a separate
   lifecycle proof and cannot contribute post-outcome feature bytes.
9. A correction creates no second active sample for the same superseded
   Outcome. It excludes the old sample, stales every dependent run/model, and
   admits only the current corrected Outcome.
10. Every training run independently resolves the complete upstream chain. A
    locally well-formed admission with missing, stale, corrected, tombstoned,
    or mismatched refs is unusable.
11. Deferred work remains durable and replayable. A transient not-yet-mature
    condition is not recorded as success or permanent failure.
12. Exact replay is idempotent at every crash boundary and never duplicates an
    admission, exclusion, run, model, projection, or receipt.
13. Production reconciliation is object-level, hash-bound, backup-first, and
    incapable of fabricating a principal capability, Prediction, Outcome,
    attribution, or label.

## Chosen architecture: approach A

Approach A combines a typed Decision source-purpose contract with a dedicated
durable training-admission consumer. It makes the obligation explicit before
the crash window exists and keeps the training daemon as the only replay
owner.

### Rejected alternatives

**Cross-database synchronous coupling** is rejected. Closing the feedback
receipt in `producer_consumer_ledger.db` and projecting the training sample in
`mnemos.db` cannot be made one atomic SQLite transaction without introducing a
fragile distributed transaction. The current defect is exactly the uncovered
interval between those writes.

**A periodic missing-admission scanner as the only owner** is rejected. It can
be retained as an independent audit/recovery oracle, but a post-hoc scan does
not express the obligation at attribution commit time, does not carry an exact
authorization binding, and can silently turn an unowned handoff into eventual
best effort.

**Adding `cognitive_state_read` to every Belief** is rejected. That mutates the
meaning of existing immutable source ACLs and creates a generic permission
solely to accommodate one consumer. Decision must consume Belief through the
purpose Belief already owns.

## Typed Decision snapshot authorization

### Fixed source contract

`core/cognitive/decision_snapshot_access.py` will own one immutable contract:

```text
cognition_episode  -> cognitive_state_read
belief_revision    -> belief_read
prediction_record  -> prediction_read
calibration_record -> calibration_internal
```

The exact names may be represented as frozen dataclasses/constants, but there
is one code owner and one contract hash. No caller parameter, configuration
key, wildcard, fallback purpose, or “try another purpose” loop is permitted.

`CognitiveStateStore` continues its two-stage authorization: the first query
reads identity plus the compact ACL only; a semantic body is fetched only
after exact-purpose authorization succeeds. The new contract operation returns
typed `AuthorizedCognitiveSource` values containing the revision, object type,
purpose used, ACL hash, and authorization contract hash. It also reports
candidate, authorized, and denied counts per object type so a Belief-specific
denial cannot disappear inside one aggregate count.

### Derived Decision access

The current generic `derive_strictest_cognitive_access()` correctly rejects an
output purpose that is absent from one source. It must not be weakened.
Instead, a separate consumer-bound derivation operation accepts only already
authorized `AuthorizedCognitiveSource` values plus the fixed Decision consumer
contract.

That operation:

- revalidates every source ACL and authorization proof;
- requires one compatible owner/agent/private-principal/project/session/
  visibility/consent context;
- retains the strictest sensitivity, retention, redaction, and lineage;
- derives only the fixed Decision output purpose; and
- includes the source-purpose contract hash and each source ACL hash in the
  snapshot and Decision lineage.

It does not rewrite or add a purpose to any source object. A restricted or
scope-incompatible Calibration remains denied. A same-scope Belief becomes a
real snapshot input through `belief_read`; a cross-scope or unauthorized Belief
remains unreadable.

## Durable feedback-to-training intake

### Admission outbox command

Objective attribution creates one additional `LocalConsumerCommand`:

```text
consumer_id  = governed_training_admission
command_type = admit_governed_training_evidence
```

It is committed by the same `CognitiveStateUnitOfWork` transaction as the
`feedback_attribution_record` and the fixed COG-038 target commands. The
existing `AttributionReceipt.command_ids` continues to identify only COG-038
target commands; a separate exact `training_admission_command_id` identifies
the training obligation so existing feedback processors cannot accidentally
consume it.

The immutable command payload binds:

- attribution object/revision/payload hash;
- exact current Outcome object/revision/payload hash;
- the `training_evidence` target command ID and payload hash;
- the complete required feedback-command ID/payload-hash manifest;
- source owner principal ID and agent derived from the validated attribution
  ACL, not caller metadata;
- source ACL hash, project/session/scope, and source-authority refs;
- correction/supersession refs, when present;
- the admission-intake contract version/hash; and
- the deterministic command key.

The creating method still requires a server-resolved `PrincipalEnvelope` and
verifies it before the transaction. The durable consumer does not accept a
PrincipalEnvelope from a replay caller. It re-resolves identity and scope from
the committed ACL, command, attribution, and feedback receipts. The eventual
admission `authorization_ref` points to this immutable intake authorization,
not to a fabricated or expired caller capability.

### Readiness and consumption

`TrainingGovernanceStore` gains a bounded deep operation:

```text
reconcile_admission_intakes(limit) -> TrainingAdmissionReconciliationReport
```

For each pending `governed_training_admission` command it independently proves
the command identity and waits for the full referenced feedback-command
manifest. The training target must have one valid committed reciprocal domain
receipt; every required correction neutralization must also be terminal.

The intake has three observable outcomes:

- `committed`: the exact admission/correction effects and projections exist,
  then the intake receives one immutable terminal effect receipt;
- `deferred`: maturity or Prediction terminalization is not ready, so the
  command remains pending and no effect receipt or admission revision is
  written; or
- `failed`: immutable contract corruption or an impossible lineage is
  dead-lettered and blocks the strict aggregate audit.

A crash after any admission/exclusion/projection write but before the intake
receipt causes exact replay. Deterministic IDs and current-head checks converge
the same objects before the receipt is committed. A crash before feedback
target completion leaves both target and admission commands durable; feedback
replay completes first and training replay follows.

The daemon service processes admission intakes before sample/run projection
commands and before ready/stale model work. Synchronous entry points may ask the
same owner to process the exact intake for low latency, but they no longer own
admission and cannot make durability depend on the synchronous return path.

## Maturity and current Prediction proof

Admission resolution must revalidate all of the following at execution time:

1. the attribution and Outcome are the current canonical heads;
2. the Outcome binds the exact immutable pre-outcome Prediction revision;
3. the current Prediction head is a terminal revision descended from that
   sealed revision;
4. terminal state is exactly `measured`, exposure is proven, and the terminal
   Outcome ref exactly matches the current Outcome revision and hash;
5. the governance clock is greater than or equal to the parsed timezone-aware
   Outcome `matured_at`;
6. the objective oracle issuance and reciprocal effect receipts still verify;
   and
7. there is no confounded, censored, unknown, corrected-away, or tombstoned
   source in the chain.

The admission schema advances to v2 and stores two Prediction identities:

- `prediction_ref`: the immutable sealed pre-outcome revision used for feature
  extraction; and
- `prediction_terminal_ref`: the current measured terminal revision used only
  as lifecycle/outcome proof.

`temporal_proof.maturity` is derived only after the clock check. The payload
builder cannot accept or hard-code the value. A not-yet-mature or open
Prediction raises a typed retryable-not-ready result before any write.

## Automatic correction, exclusion, stale, and rebuild

The intake dispatcher derives correction mode from the current Outcome's exact
outer and payload lineage. No caller chooses ordinary admission versus
correction.

For a corrected Outcome, replay performs these idempotent phases:

1. prove the corrected Outcome supersedes and corrects exactly one prior
   Outcome in the same Prediction chain;
2. terminally supersede any unconsumed prior admission intake;
3. find zero or one current admission bound to the prior Outcome; more than one
   is invariant corruption;
4. if one exists, append `correction_pending`, project exclusion, and verify
   the old sample is no longer active;
5. mark every dependent run and governed model stale and make active loaders
   reject their heads;
6. admit and project the corrected current Outcome once it is mature and its
   Prediction terminal proof is current; and
7. commit the corrected intake receipt only after all required effects are
   independently readable.

If the prior Outcome was never admitted, the correction admits only the
corrected Outcome and proves the old intake cannot later revive. The training
service then deterministically rebuilds a stale dimension from the complete
current admission manifest. A crash at any numbered phase resumes from the
first missing receipt rather than repeating completed effects.

## Training-run upstream verification

`_verified_current_admissions()` must stop treating a valid admission payload
and projection receipt as sufficient. Before any readiness decision, fit,
holdout read, seal, apply, or model-head update, it independently resolves and
checks:

- the admission is the current admitted head and is not tombstoned;
- the source intake command and terminal receipt are exact;
- the feedback target command, attribution, proposal, Decision/action, and
  reciprocal domain receipt are exact and current where required;
- the Outcome is current and still binds the stored oracle issuance;
- the sealed Prediction exists and the stored terminal Prediction is the
  current measured head for that Outcome;
- correction/supersession lineage has not excluded the sample;
- the admission feature, label, split, temporal, authority, and input hashes
  recompute; and
- the scoring projection and reciprocal sample receipt match the admission.

Any missing or stale upstream revision fails before model bytes or a run
revision are written. This makes fabricated but schema-valid admissions
unusable in production and tests.

## Aggregate audit and gate

Add `scripts/audit_phase3_cognitive_chain.py --strict --json` as an independent
cross-domain owner. It composes, but does not merely echo, the existing strict
audits. Its machine report includes real denominators and at least these
zero-budget metrics:

- `decision_belief_candidate_gap`;
- `decision_source_purpose_contract_gap`;
- `eligible_feedback_without_admission_intake`;
- `terminal_training_target_without_admission`;
- `mature_training_intake_pending_without_reason`;
- `immature_or_open_prediction_admitted`;
- `admission_upstream_revision_gap`;
- `corrected_sample_still_active`;
- `correction_dependent_run_or_model_not_stale`;
- `stale_model_head_active`;
- `training_intake_without_terminal_receipt`; and
- `aggregate_chain_test_denominator_gap`.

The existing training audit must replace the hard-coded formal-producer count
with the actual objective training-intake denominator and must index both
admission-intake and governed-projection consumers. Individual COG-035/036/037/
038/048 audits remain required.

The aggregate audit enters local gates, CI, and full-score required manifests.
Static-only profiles still verify code ownership and gate registration, while
full read-only runtime mode verifies production denominators. No partial or
static-only pass may be described as aggregate runtime closure.

## Controlled reconciliation

Add a dry-run-first reconciliation command for pre-contract objective
attributions:

```text
python3 scripts/reconcile_phase3_training_admission_intakes.py --json
python3 scripts/reconcile_phase3_training_admission_intakes.py \
  --apply --backup-dir <reviewed-dir> --json
```

The tool requires stopped services and uses the SQLite backup API before an
apply. Its exact object identity is the existing objective attribution revision
plus the exact `training_evidence` command/receipt and current Outcome ref. It
publishes inventory and object-manifest hashes, source schema fingerprints,
row counts, proposed command IDs, unresolved reasons, backup integrity/hash,
and replay results.

An object is eligible for a new intake command only when all canonical source
refs, ACL owner/scope, Outcome, target command, and reciprocal receipt can be
recomputed exactly. The tool never invents a PrincipalEnvelope, capability,
Prediction, Outcome, maturity, attribution, label, feature, or correction. An
unprovable object is classified explicitly and remains release-blocking if it
represents an otherwise eligible live obligation.

After apply, a second dry-run must report zero missing intents and zero changes.
State and scoring databases must pass `PRAGMA integrity_check`. The current
production snapshot is expected to produce an empty object manifest because it
contains zero training-evidence commands and admissions; the zero denominator
must still be recorded and reviewed rather than assumed.

## Supplemental closure: historical cognitive-action target state

The aggregate reclosure exposed an older COG-014 contract transition that the
single-domain closure did not migrate. Eighty-eight immutable Observation
target receipts still bind the exact row hash recorded when their distillation
actions committed. Later calibration, ACL, provenance, and schema owners
legitimately evolved those rows, so neither the legacy full-row hash nor the
intermediate v2 projection now equals the original `after_hash`. The original
effect, reciprocal receipt, material-action terminal, and Decision evidence
remain valid historical facts and must not be rewritten.

This is covered by the already approved approach A because it is another
object-level, hash-bound, backup-first provenance migration. The target store
will append an immutable reconciliation record rather than mutate old evidence.

### Observation target-state contract v3

`core.hephaestus.cognitive_action_targets` remains the single projection owner.
For an Observation created by a distillation cognitive action, v3 hashes only
the stable action-owned semantic state:

- object identity, dimension, observation type, value, and unit;
- source type, source path, source action ID, and semantic evidence;
- observed/period timestamps and original creation timestamp; and
- content-source and user-intent signal.

JSON-valued fields are decoded and canonically re-encoded before hashing.
Action-owned timestamps are parsed as ISO-8601, normalized to UTC, and rendered
through one canonical formatter, so `Z`, an equivalent numeric offset, and the
historical naive-UTC representation have one semantic hash; malformed values
fail closed. The projection excludes the complete calibration-owned column set (base prior and
measurement status, posterior confidence, pointers, and source-span bindings),
object ACL, user-maintained notes, update time, and row version. Each excluded
field has a separate lifecycle owner and may change without changing what the
historical cognitive action created. Calibration-owned fields remain governed
by the strict calibration-lineage audit; mutating any included action field
must still fail the strict effect audit.

The expected v3 semantic state is reconstructed only from the immutable action
command and artifact through the same code-owned normalization contract used by
new writes. Matching `source_id` alone is insufficient. If the current row
does not exactly match that expected semantic projection, migration is refused;
the tool never guesses a historical preimage or treats current bytes as proof
of their own origin.

### Append-only reciprocal reconciliation

The canonical target database owns
`cognitive_action_target_state_reconciliations`. One immutable row binds:

- reconciliation, effect, action, target, and target-object identities;
- the original reciprocal-receipt hash and immutable legacy `after_hash`;
- immutable command/effect/artifact hashes and exact v3 expected/current
  semantic-state hash;
- the full target-row hash observed at migration time, retained as a snapshot
  rather than reinterpreted as ownership of later independent-field changes;
- state-projection and migration-contract versions/hashes;
- source inventory and eligible-object manifest hashes; and
- application time and exact migration identity.

Each batch stores the canonical source-inventory JSON as well as the eligible
object manifest. The strict audit decodes both, recomputes the inventory,
object-manifest, and batch-identity hashes, binds each stored full-row snapshot
back to its migration-time inventory entry, and independently re-reads the
immutable command, effect, target receipt, artifact, and current action-owned
target projection. Later calibration/ACL/operator fields are therefore allowed
to evolve, but current semantic drift still fails closed.

Exact primary-key, unique object/effect/action constraints, `NOT NULL`/`CHECK`
signatures, the batch foreign key, and table/event-specific fail-closed
update/delete triggers make the schema and records append-only. A same-column
weakened schema is rejected. New receipts bind v3
directly; an unchanged legacy receipt may still prove itself through its
original exact row hash, but a legacy mismatch has no bypass without an exact
reconciliation.

The migration command is dry-run first, requires both daemon and MCP runtime
quiescence, backs up the action and target databases through the SQLite backup
API, verifies source fingerprints and `PRAGMA integrity_check`, applies one
target transaction, and proves a second run is a no-op. Backup, inventory, or
object-manifest drift aborts before writes. Apply failure rolls back the table
and rows together.

Rejected alternatives are rewriting historical `after_hash` values, which
breaks Decision/material lineage; accepting any current row whose `source_id`
matches, which is self-attestation; and an allowlist or audit skip, which hides
rather than closes the provenance gap.

## TDD and fault-injection strategy

Implementation proceeds one vertical failure at a time, beginning with a
failing public or integration test.

### Decision authorization tests

- Same principal/project/session Belief appears in the sealed Decision
  snapshot through `belief_read`.
- Wrong owner, project, session, agent, consent, visibility, or purpose remains
  denied before body fetch.
- A caller cannot nominate a purpose mapping or add a generic fallback.
- Mixed authorized sources derive the strictest Decision access and bind the
  contract hash.

### Admission handoff tests

- Attribution and admission-intake command commit atomically.
- Crash before target completion, after target receipt, after admission
  revision, after sample projection, and before intake receipt all converge
  exactly once after restart.
- The training daemon, not `OutcomeRecorder`, owns replay.
- Missing or forged command, principal binding, target receipt, or manifest
  fails closed.

### Maturity and correction tests

- Before `matured_at`, the intake stays pending with zero admission/sample
  writes; at the boundary it admits exactly once.
- An open, censored, confounded, unknown, stale, or mismatched Prediction never
  admits.
- Corrected Outcome automatically excludes the old sample, stales dependent
  run/model state, admits the replacement, and triggers deterministic rebuild.
- Crashes at every correction phase replay only missing effects.
- Tombstoned or superseded prior intake cannot resurrect.

### Canonical model-chain tests

The positive scorer-v2 test must build enough examples entirely through public
canonical APIs: Decision, sealed Prediction, material effect, mature measured
Outcome, Feedback attribution, admission intake, sample projection, run, and
model. Direct insertion of admission revisions is forbidden.

The dataset must satisfy readiness with real deterministic subject-group split
assignment and both label classes. Mutation tests delete or stale one upstream
Prediction, Outcome, attribution, command, receipt, or terminal ref and prove
the run fails before any new run/model write.

The test denominator includes the happy path, each crash boundary, maturity,
correction, tombstone, stale model, rebuild, ACL denial, and fake-upstream
negative case.

## Implementation and verification sequence

1. Add failing Belief-to-Decision cross-domain tests and implement typed source
   authorization.
2. Add failing admission-outbox/restart tests and commit the intake with
   objective attribution.
3. Add failing maturity/current-terminal tests and harden admission schema and
   resolution.
4. Add failing public-correction tests and wire automatic exclusion/stale/
   rebuild.
5. Replace fabricated model fixtures and harden runtime upstream verification.
6. Add reconciliation tooling and its backup/restore/idempotence tests.
7. Add the aggregate strict audit and integrate all required gates.
8. Run focused tests, changed tests, existing strict domain audits, aggregate
   audit, Quick, Integration, and Heavy in hermetic isolated profiles.
9. Run independent Standards and Spec review, repair every actionable finding,
   and rerun affected evidence.
10. With services still stopped, perform reviewed production dry-run/apply if
    required, post-apply dry-run, integrity checks, and runtime aggregate audit.
11. Reconcile historical cognitive-action target states under the supplemental
    v3 contract, then rerun COG-014 and the aggregate Phase 3 audit together.
12. Synchronize repo docs, Desktop `mnemos系统图谱`, and the governing Desktop
    audit document in place; then create local implementation/documentation
    commits.

## Acceptance criteria

Phase 3 may return to aggregate closed only when all of the following are true:

- a canonical same-scope Belief appears in a sealed Decision snapshot and all
  negative ACL cases remain denied;
- every eligible objective attribution has exactly one durable admission
  intake and every mature committed intake has exactly one current admission or
  one explicit blocking terminal failure;
- crash/restart at every handoff and correction boundary converges exactly once;
- no admission exists before maturity or without a current measured terminal
  Prediction and exact current Outcome;
- every corrected old sample is excluded, every dependent run/model is stale,
  and the replacement/rebuild lineage is complete;
- a training run rejects every nonexistent, stale, mismatched, corrected-away,
  or tombstoned upstream ref;
- the positive model test contains no fabricated admission or upstream
  revision;
- all aggregate zero-budget metrics are zero with non-hard-coded denominators;
- production reconciliation is reviewed, backup-bound when non-empty,
  idempotent, and leaves database integrity `ok`;
- every evolved legacy cognitive-action target is either still provable by its
  original exact hash or has one exact append-only v3 reconciliation, while
  action-owned semantic tampering remains detectable;
- focused, Quick, Integration, Heavy, all strict Phase 3 audits, and both review
  axes pass on the same clean implementation commit; and
- repo and Desktop documents state aggregate closure without claiming a global
  release certificate.

## Prohibited shortcuts

- Adding `cognitive_state_read` to Belief solely to make Decision tests pass.
- Trying multiple read purposes until one succeeds.
- Caller-selectable purpose, maturity, terminal state, correction, split,
  feature, label, principal, or bypass flags.
- Keeping synchronous admission as the only handoff owner.
- Treating a periodic scanner, log line, proposal label, or table row count as
  a durable receipt.
- Marking future maturity as mature or terminalizing a Prediction inside the
  admission payload builder.
- Admitting both old and corrected Outcomes and filtering one only during fit.
- Staling a run without invalidating the active model head and rebuild path.
- Accepting schema-valid admissions without resolving their upstream objects.
- Replacing fake admission IDs with different fake IDs or weakening the
  readiness denominator.
- Fabricating production principal capabilities or semantic truth during
  reconciliation.
- Weakening tests, expanding audit budgets/baselines, or relabeling partial
  diagnostics as aggregate closure or release certification.
