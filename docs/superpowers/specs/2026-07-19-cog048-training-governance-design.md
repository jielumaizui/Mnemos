# COG-048 trustworthy training admission and governance design

## Status and decision

This specification closes the final Phase 3 root, COG-048. It is bound to the
repository state after COG-038 closure commit
`5974460e9f32dc02215b83c7e84d6f3352f2a193` and to the user-approved migration
choice made on 2026-07-19: **Scheme A, exact object-level quarantine followed by
a governed cold start**.

COG-043 was revalidated before this design. Its strict ACL/deletion audit still
reports `ok=true`, with complete object/domain denominators and zero ACL,
pre-body authorization, deletion-effect, projection, or resurrection gaps.
COG-048 must preserve those guarantees.

The root is closed only when a training example can enter an active model or
Bayesian/rule optimizer state through one canonical admission owner, and only
when that example binds an action-before-outcome PredictionRecord to a mature,
independently verified OutcomeMeasurement. A UserReaction is evidence or a
weak-evidence proposal; it is never an admitted feature or objective ground
truth.

## Problem statement

The old scorer stack still contains a self-confirming path even though COG-038
retired reaction fanout at the public feedback seams:

- `AdaptiveScorerV2.feedback()` converts `FeedbackV2.expected` into a label;
- `enqueue_training_sample()` writes a fixed expected score as both queue
  priority and weak ground truth;
- `insert_ground_truth()` accepts a caller-provided label without a prior
  PredictionRecord or mature OutcomeMeasurement;
- `_get_training_samples()` joins rows by session and even falls back to any
  same-session ground truth;
- `refresh_bayesian_priors_from_ground_truth()` trusts the same legacy table;
- `process_training_queue()` and Chronos consume those rows without an
  admission receipt;
- application search, Wiki read/write, guard, and distillation callers still
  enqueue fixed values such as `0.7`, `0.8`, `0.2`, or post-hoc actual scores;
  and
- the rule optimizer accepts post-hoc labels without the Phase 3 prediction and
  outcome contract.

Renaming these values, adding random noise, or filtering only reaction rows
would leave the causal defect intact.

## Current production denominator

The design-time read-only inventory, while daemon and Mnemos MCP services are
stopped, contains 25,128 exact historical training/optimizer objects:

| Database | Object class | Count |
|---|---|---:|
| `mnemos.db` | `ground_truth_signals` | 9,171 |
| `mnemos.db` | `scorer_training_queue` | 12,076 |
| `mnemos.db` | `scorer_feedback_events` | 331 |
| `mnemos.db` | `scorer_models` | 24 |
| `mnemos.db` | `bayesian_scorer_state` | 4 |
| `mnemos.db` | `bayesian_feedback` | 0 |
| `rule_weight_optimizer.db` | `rule_outcomes` | 1,781 |
| `rule_weight_optimizer.db` | `optimize_log` | 96 |
| `rule_weight_optimizer.db` | `weight_history` | 1,645 |

The 12,076 queue rows are 10,567 `completed`, 1,172 `pending`, and 337
`unsupported`. All 24 stored scorer models are already inactive. The 3,625
COG-038 feedback objects are a subset of this denominator and must not be
double-counted or promoted merely because they already have a feedback
quarantine record.

The migration command recomputes the live denominator, schema fingerprints,
row hashes, and object-manifest hash. These design-time counts are evidence, not
hard-coded acceptance constants.

## Scope

COG-048 owns:

- canonical training admission from COG-038 `training_evidence` commands;
- prediction/outcome/decision/action/source-authority revalidation;
- deterministic feature and label derivation;
- dataset grouping and train/validation/holdout assignment;
- immutable correction, exclusion, and compensation lineage;
- scorer model, Bayesian prior, and rule-optimizer application receipts;
- removal of every production direct training writer and legacy active reader;
- exact historical training/optimizer inventory, backup, quarantine, restore,
  and idempotent replay;
- a strict independent training-governance audit; and
- Phase 3 aggregate verification across COG-043/035/036/037/038/048.

It does not invent historical predictions or outcomes, infer labels from
clicks, run a real-model retraining campaign, delete historical user data,
repair unrelated Phase 4-7 roots, or claim a release certificate.

## Non-negotiable invariants

1. A training label has exactly one current canonical PredictionRecord and one
   current, mature OutcomeMeasurement.
2. The prediction is sealed before its bound material action/effect and before
   the outcome observation window.
3. Prediction and outcome bind the same decision, action, delivery, subject,
   scope, metric, unit, and strictest CognitiveAccess envelope.
4. The OutcomeMeasurement is reissued and verified through the COG-037
   TaskResultOracle path, including exact Raw source-authority bytes.
5. Reaction values, presentation state, click, ignore, dwell, silence, fixed
   scores, post-hoc heuristics, and arbitrary caller metadata cannot provide
   objective labels.
6. Features are derived only from the immutable pre-outcome prediction
   snapshot and a versioned extractor. Post-outcome bytes cannot enter features.
7. Dataset split is stable, group-aware, code-owned, and recorded before model
   training. Holdout examples never enter fit, prior, optimizer, threshold, or
   early-stopping inputs.
8. Every training, model, prior, and optimizer effect has an append-only command
   and reciprocal independently readable receipt.
9. A correction never overwrites history. It excludes the superseded example,
   marks every dependent run stale, and requires a rebuild from current
   admissions before another model can become active.
10. Legacy rows remain historical data but are invisible to active training and
    model/prior loading after activation.
11. No caller can select a bypass flag, self-issue a permit, or downgrade a
    missing proof to a warning.
12. ACL, purpose, deletion, retention, and freeze behavior remain at least as
    strict as COG-043.

## Canonical architecture

### Cognitive state v4

The existing `CognitiveStateStore` remains the single semantic owner. The
canonical schema advances from `mnemos.cognitive_state_store.v3` to v4 and adds
two object types to the same revision/head/outbox/effect-receipt transaction:

- `training_admission_record` / `mnemos.training_admission_record.v2`; and
- `training_run_record` / `mnemos.training_run_record.v1`.

The explicit v3-to-v4 reconciliation rebuilds the constrained revision table,
preserves every existing row and foreign key, registers the exact v4 DDL hash,
and validates row counts, state/projection hashes, foreign keys, and all prior
enforcement markers. Constructors validate only; they never silently migrate.

### `TrainingGovernanceStore`

`core/cognitive/training_governance.py` is the deep owner. Its public surface is
small:

```text
process_admission_intake(command_id) -> TrainingAdmissionIntakeReceipt
reconcile_admission_intakes(limit) -> TrainingAdmissionReconciliationReport
admit_training_evidence(command_id) -> TrainingAdmissionReceipt
reconcile_pending(limit) -> TrainingReconciliationReport
build_ready_run(dimension, now) -> TrainingRunReceipt
apply_run(run_revision_id) -> TrainingRunReceipt
rebuild_stale_dimension(dimension) -> TrainingRunReceipt
```

Callers provide identities, not labels, split values, feature vectors, model
state, correction mode, or eligibility booleans. Outcome correction can enter
training only through the durable admission intake created by the canonical
feedback path; the store derives correction mode from exact current Outcome
lineage and independently verifies every canonical object.

### Training projection owner

`core/scoring/training_schema.py` is the only DDL/version/hash owner for the
scoring-side projection. It owns append-only tables for admitted examples,
exclusion actions, model artifacts, run receipts, and mutable derived heads:

- `governed_training_samples`;
- `governed_training_sample_actions`;
- `governed_training_sample_receipts`;
- `governed_scorer_models`;
- `governed_scorer_model_heads`; and
- `governed_training_run_receipts`.

The legacy `ground_truth_signals`, `scorer_training_queue`,
`scorer_feedback_events`, `scorer_models`, `bayesian_scorer_state`, and
`bayesian_feedback` tables remain read-only historical assets after activation.
They are not renamed into the new contract and are not accepted as source
truth.

### Training service

`daemon/training_governance_service.py` is a shallow bounded worker registered
as `training_governance`. It asks the store to reconcile pending admissions and
ready/stale runs. It does not derive labels, choose splits, fit arbitrary rows,
or mutate a model directly.

Chronos' old `scorer_training` path delegates to this service or reports the
governance state. It cannot call `AdaptiveScorerV2.process_training_queue()`.

## Training admission record

Every active payload includes:

- system-owned admission ID, revision state, input-set hash, supersedes and
  correction refs;
- exact COG-038 training-evidence command, attribution revision, domain proposal,
  material decision/action, and reciprocal receipt refs;
- exact sealed pre-outcome PredictionRecord, separate current measured terminal
  PredictionRecord, OutcomeMeasurement, DecisionTrace, action, material effect,
  delivery, subject, scope, principal, and CognitiveAccess refs/hashes;
- prediction/outcome temporal proof and maturity state;
- source-authority catalog/hash and Raw issuance proof;
- a feature snapshot derived only from the sealed prediction, with dimension,
  normalized values, extractor version, code/spec/config hashes, and snapshot
  hash;
- a label record containing metric, unit, observed class/value, labeler
  version, code/spec/config hashes, and derivation hash;
- uncertainty, attribution, competing causes, calibration eligibility, and
  exclusion reason;
- deterministic dataset group ID/hash, split, split-policy version/hash, and
  assignment proof;
- lifecycle state (`admitted`, `excluded`, `correction_pending`, or
  `historical_unverified`); and
- all target command/effect refs.

The first supported metric is COG-037's categorical
`predictive_delivery_usefulness`. Its observed class is taken from the verified
OutcomeMeasurement, not from prediction correctness or a UserReaction. The
first governed scorer dimension is `predictive_delivery`; it is not silently
mapped onto `profile`, `kg`, `distill`, or `ops`.

The v1 label registry is exact: `useful -> 1` and `not_useful -> 0`. The v1
feature extractor reads only immutable PredictionRecord fields: selected route,
predicted class, code-owned confidence band and its sealed source inputs,
evaluation-window duration, causal-assumption flags, and hashed source-snapshot
identity. It does not read OutcomeMeasurement, UserReaction, terminal error,
calibration result, or any post-delivery payload. Feature names, normalization,
ordering, and extractor source bytes enter the extractor hash.

Future dimensions require an explicit metric/feature/label registry entry and
tests. Unknown dimensions fail closed.

## Training run record

Every training run binds:

- dimension and algorithm implementation/version/code/config hashes;
- ordered current admission revision IDs and payload hashes;
- dataset manifest hash and exact train/validation/holdout denominators;
- fit input hash containing train examples only;
- validation and holdout report hashes, with holdout bytes excluded from all
  fit/tuning inputs;
- parent governed model ID/hash, if any;
- produced model type/blob hash and deterministic serialization metadata;
- state (`model_sealed`, `sealed`, `applied`, `stale`, `failed`, or
  `insufficient_sample`);
- material action/effect refs and scorer-side reciprocal receipt; and
- supersession/rebuild lineage.

`insufficient_sample` is a valid reproducible terminal result. It does not
invent data or silently consume legacy rows.

## Deterministic split policy

The v1 split policy is global and registry-owned:

- 80% train;
- 10% validation; and
- 10% holdout.

Assignment hashes the canonical subject-group identity, metric, scope, and
split-policy namespace. All revisions and related exposures in one subject
group remain in the same split. Callers cannot override assignment. The public
namespace is part of the policy hash; no new encryption or secret/key-management
layer is introduced.

Holdout examples can be evaluated only after the model artifact is sealed. They
cannot influence feature selection, label mapping, model choice, thresholds,
retry decisions, early stopping, Bayesian state, rule weights, or whether a run
is promoted.

`model_sealed` is the durable internal pre-evaluation state: the exact model
artifact, manifest, and reciprocal projection receipt are committed before any
holdout read. Holdout evaluation then supersedes that revision with `sealed`;
only a subsequent `applied` revision may own the active model head.
`model_sealed` is never loadable as an active model, and the strict audit treats
an unresolved current `model_sealed` head as a contract gap. This is an
implementation clarification of the approved seal-before-holdout requirement,
not an additional caller-selectable lifecycle state.

The v1 readiness policy requires at least 20 current train examples, both label
classes, at least two train examples per class, at least two validation
examples, and at least two holdout examples. These are global registry values
included in the readiness-policy hash. A smaller or one-class set produces
`insufficient_sample`; callers cannot lower the threshold.

## COG-038 training-evidence flow

1. COG-038 records a UserReaction or verified objective OutcomeMeasurement and
   appends the complete attribution revision and seven target commands.
2. Reaction-only evidence reaches a durable `training_evidence` proposal or
   intentional skip, but remains ineligible for training admission.
3. An objective training command names the exact current attribution and
   OutcomeMeasurement.
4. The training worker re-reads the COG-038 command, target journal proposal,
   material gate, decision/action refs, and reciprocal receipt.
5. `TrainingGovernanceStore` resolves the sealed pre-outcome PredictionRecord,
   the current measured terminal PredictionRecord, and OutcomeMeasurement and
   re-runs the COG-037 oracle, temporal, identity, terminal-projection,
   maturity, authority, ACL, and correction checks.
6. It deterministically derives features, label, and split and commits one
   admission revision, semantic event, projection command, and required effect
   receipt path in the canonical state transaction.
7. The scorer projection writes the immutable sample and reciprocal receipt.
8. Exact replay returns the same admission and projection proof.

No COG-038 target adapter calls `AdaptiveScorerV2.feedback()`,
`insert_ground_truth()`, `enqueue_training_sample()`, Bayesian feedback, or the
rule optimizer.

## Scorer and optimizer cutover

All production direct writers are removed or converted to operational telemetry:

- `core/application/kia.py` guard alerts;
- `core/application/intelligence.py` context search;
- `core/application/facade.py` Wiki search/read;
- `core/application/trusted_write_bridge.py` Wiki writes;
- `core/hephaestus/distillation_feedback.py` post-hoc signals;
- `core/app/context_search.py` click/ignore compatibility paths;
- `core/scoring/feedback_persistence.py` legacy label persistence;
- `core/scoring/feedback_channel.py` reaction bridge;
- `core/kia/rule_scorer.py` optimizer inputs; and
- `core/kia/chronos.py` direct queue processing.

Operational events may still be recorded through their domain owners, but they
do not become labels or training examples without a later canonical prediction
and mature objective outcome.

The public pre-cutover method names fail closed for new writes with
`training_admission_receipt_required`. In particular, `load_model()` and
`insert_ground_truth()` remain only as permanent fail-closed safety boundaries:
an exact AST contract requires their side-effect-free rejection body, and any
behavioral drift blocks the strict COG-048 audit. They are not compatibility
implementations or accepted zombie debt. `AdaptiveScorerV2` gains a narrow
governed application method that accepts only a canonical run revision ID and
revalidates its outbox command and current admission set. A caller-created DTO,
boolean, environment flag, or test marker is insufficient.

Model loading reads only `governed_scorer_model_heads` and verifies the model,
run, manifest, current admission, split, and effect hashes. Missing or stale
proof returns rule-only/cold behavior rather than loading a legacy model.

Bayesian prior rebuilds and rule-weight optimization use only current
train-split admissions and record their own run/effect receipts. Validation and
holdout examples are excluded. Existing optimizer history remains readable but
cannot seed current state.

## Corrections, exclusions, and rebuilds

An OutcomeMeasurement correction must reference the exact latest measurement
and terminal prediction revision. The governance store then:

1. appends `correction_pending` for the affected admission;
2. writes one deterministic exclusion action and reciprocal projection receipt;
3. marks every governed run/model/prior/optimizer effect that consumed the old
   admission as stale;
4. prevents stale artifacts from being loaded or updated;
5. admits the corrected current outcome as a new admission revision, if still
   eligible; and
6. rebuilds the dimension from the complete current train-split manifest before
   promoting another model head.

Incremental model effects are never erased from history. A compensation and
rebuild prove the current state. Partial failure remains pending/stale and never
restores the old model as active.

ACL revocation, subject deletion, retention tombstone, or COG-038 correction
uses the same exclusion/rebuild flow. COG-043 deletion verification cannot be
`verified` while an active governed sample or model still references the
subject.

## Failure and restart behavior

- Failure before canonical admission commit leaves no semantic revision or
  projection command.
- Failure after canonical commit leaves one pending deterministic command.
- Failure after sample projection but before canonical receipt is recovered
  from the exact scorer-side receipt; the sample is not inserted twice.
- Failure during fit leaves no applied run or active model head.
- Failure after model artifact write but before canonical receipt is reconciled
  by artifact/run/manifest hash.
- Correction failure keeps the old effect excluded and the dimension stale; it
  never loads a known stale model.
- SQLite busy/temporary unavailability is retryable. Contract, hash, authority,
  maturity, split, or correction failures are terminal and reviewable.
- Every bounded service batch is idempotent and restart-safe.

## Scheme A historical migration

Add `scripts/reconcile_training_governance_history.py`. Dry-run is default and
must not create directories, schemas, metrics, locks, or rows.

The command inventories every source object by database class, table, primary
key, immutable row hash, schema fingerprint, and current activation status. It
also detects legacy active readers and writers before apply.

Apply requires:

- daemon and all Mnemos MCP services stopped;
- exact expected inventory and object-manifest hashes;
- an explicit backup directory;
- successful SQLite backup-API copies of `producer_consumer_ledger.db`,
  `mnemos.db`, and `rule_weight_optimizer.db`;
- integrity checks and sealed backup manifest;
- an exclusive training-governance migration barrier that every producer,
  scheduler, model loader, and optimizer honors; and
- a clean pre-activation state with no unreceipted governed effects.

Each exact historical object receives one
`historical_unverified_training_asset` quarantine entry. If COG-038 already
classified the same source row as historical feedback, COG-048 records a
distinct policy classification linked to that exact prior quarantine; it does
not duplicate or reinterpret source bytes.

Migration never creates a PredictionRecord, OutcomeMeasurement, label, split,
active admission, training run, model head, Bayesian prior, or optimizer weight.
Pending legacy queue rows remain in the legacy table but are ineligible after
activation. Existing models and Bayesian/rule state remain historical and are
ignored. Since all current scorer models are already inactive, activation does
not demote an active model; formal behavior starts cold/rule-only until valid
new admissions exist.

The activation marker is written only after v4 schema reconciliation, complete
quarantine coverage, zero legacy production callers/readers, and independent
verification. The barrier is removed last.

Restore validates every backup hash and schema under the same barrier and
returns all three databases to their exact pre-apply bytes/state. Reapply and
exact replay must converge with `inserted=0`, no new quarantine generation, no
active admission, and unchanged head/model/prior/optimizer hashes.

## Independent strict audit

Add `scripts/audit_training_governance.py --strict --json`. The audit must not
trust the writer's summary. It independently discovers production call sites,
opens canonical and source databases read-only, revalidates schemas and hashes,
reissues objective outcomes, recomputes admissions/splits/labels/manifests,
rebuilds active model lineage, and inspects historical coverage.

Zero-budget metrics include:

- `expected_equals_actual_from_same_reaction`;
- `training_without_prior_prediction`;
- `reaction_used_as_objective_ground_truth`;
- `label_without_provenance`;
- `immature_outcome_admitted`;
- `confounded_outcome_admitted`;
- `prediction_outcome_identity_mismatch`;
- `post_outcome_feature_leak`;
- `holdout_leak`;
- `split_assignment_mismatch`;
- `duplicate_training_effect`;
- `training_effect_without_receipt`;
- `model_without_training_manifest`;
- `model_manifest_hash_mismatch`;
- `stale_corrected_sample_active`;
- `stale_model_active`;
- `bayesian_update_without_admission`;
- `optimizer_update_without_admission`;
- `training_producer_bypass`;
- `legacy_training_active_reader`;
- `historical_training_object_uncovered`;
- `historical_quarantine_promoted_active`;
- `training_schema_registry_mismatch`;
- `training_migration_barrier_bypass`; and
- `phase3_training_contract_gap`.

The report includes complete denominators for formal producers, schedulers,
readers, object classes, admissions, splits, runs, models, priors, optimizer
effects, corrections, historical inventory, quarantine, and Phase 3 required
audits. Empty active admissions are reported as an explicit zero denominator,
not hidden or treated as proof that the positive path works.

## Test strategy

Use vertical public-interface tracer bullets:

1. Reaction-only COG-038 evidence creates no admission or model effect.
2. A prior sealed prediction plus matching mature objective outcome creates one
   admission and exact replay creates none.
3. Prediction after action, outcome before maturity, unknown metric, wrong
   action/subject/scope, forged Raw authority, and confounded outcome fail with
   zero writes.
4. Feature extraction proves it used only pre-outcome prediction bytes.
5. Grouped split assignment is deterministic across restart and prevents
   related exposures crossing splits.
6. Holdout examples cannot enter fit, Bayesian, optimizer, tuning, threshold, or
   promotion inputs.
7. Duplicate commands and restart recovery produce one sample/effect.
8. A corrected outcome excludes the old example, makes dependent artifacts
   stale, and requires a full current-manifest rebuild.
9. ACL revocation and subject deletion prevent resurrection and complete COG-043
   only after sample/model projections are gone or stale.
10. Model artifact tampering, manifest drift, receipt drift, and stale admission
    refs block loading.
11. Fresh v4 and v3-to-v4 initialization orders produce the same canonical DDL
    and fail closed on corrupt/unknown schemas.
12. Migration dry-run/apply/restore/reapply/replay proves exact object coverage,
    backup integrity, rollback, idempotency, and no invented cognition.
13. Static mutation tests remove or reintroduce each producer/reader gate and
    require the independent audit to fail.
14. The original scorer still learns through the governed positive path with a
    sufficient canonical train set; training functionality is not deleted.

Focused tests cover the new store, schema, target adapter, scorer projection,
service, audit, and migration. Broad regression covers COG-043 ACL/deletion,
COG-035 belief, COG-036 decision, COG-037 prediction/outcome, COG-038 feedback,
scorer behavior, Chronos, daemon registration, data ownership, and runtime
producer/consumer receipts.

Final verification runs Quick, Integration, and Heavy through
`HermeticRunEnvironment`, then all Phase 3 strict audits on the same clean code
commit. Production migration evidence is separate from isolated test evidence.

## Operations sequence

1. Implement and pass fresh/upgrade synthetic tests without touching production.
2. Run focused and Phase 3 regression suites.
3. Run the migration dry-run and review the exact denominator and hashes.
4. Stop/verify daemon and all Mnemos MCP services.
5. Apply with explicit expected hashes and backup directory.
6. Run strict training and all Phase 3 audits plus database integrity checks.
7. Perform a real manifest restore and prove the pre-apply state.
8. Reapply from a new backup and run exact idempotent replay.
9. Verify zero uncovered/unexpected objects, zero active legacy readers, zero
   historical promotion, and unchanged formal state outside intended schema and
   quarantine additions.
10. Keep services stopped until code, review, tests, migration, and docs are all
    committed and the user explicitly chooses restart.

## Acceptance criteria

COG-048 and Phase 3 are root-closed only when:

- exactly one training admission owner exists;
- every production writer/reader/scheduler uses the governed seam;
- a positive canonical prediction/outcome path produces and applies a real
  training sample/model with reciprocal proof;
- every COG-048 zero-budget metric is zero;
- all historical training/optimizer objects are covered or explicitly
  unexpected with root-blocking status;
- apply, restore, reapply, and replay are proven against sealed backups;
- COG-043/035/036/037/038 strict audits still pass;
- focused, broad, Quick, Integration, and Heavy tests pass without weakened
  assertions or skip selectors;
- review finds no unresolved specification or repository-standard defect;
- repo docs, Desktop `mnemos系统图谱`, and the original audit document are
  updated in place and bind the exact current commit/evidence; and
- code and Desktop documentation changes are committed locally with clean
  worktrees.

Global maintainability, zombie, security, live daemon continuity, and release
certificate gates remain separate claims. Any failure there is reported
honestly and is not hidden by Phase 3 closure.

## Prohibited shortcuts

- add noise to expected/actual;
- rename click, ignore, dwell, or silence as objective outcome;
- grandfather legacy models or priors as trusted;
- mark historical rows admitted without exact canonical refs;
- keep direct writers behind aliases, compatibility flags, or allowlists;
- let tests mint caller-selected admission permits;
- assign a split after looking at the label or outcome;
- use holdout data for fit or tuning;
- update/correct training rows in place;
- delete training functionality to make the audit empty;
- clear production tables instead of migrating them;
- weaken COG-043 deletion/ACL behavior;
- equate isolated tests with production migration; or
- call Phase 3 a release certificate.
