# COG-037 canonical PredictionLedger design

**Status:** approach A and this written specification were approved by the user
on 2026-07-18. Implementation and production reconciliation are complete.
COG-037 remains open only until the final documentation gates and local code
commit are complete; global maintainability/release closure remains a separate
Phase 3 boundary.

## Scope and completion boundary

This slice makes `CognitiveStateStore` the only canonical owner of predictions,
their evaluation windows, terminal outcomes, errors, and calibration inputs. It
must prove that every predictive delivery decision made after activation has an
immutable PredictionRecord before the corresponding delivery-event effect, and
that every matured prediction reaches exactly one terminal state:

- `measured`: an exact, mature, independently evidenced OutcomeMeasurement
  matches the prediction's subject, metric, unit, window, and attribution
  contract;
- `unknown`: exposure is proven but no qualifying outcome is available when the
  evaluation window closes;
- `censored`: exposure or follow-up cannot be proven, retention/privacy removed
  required evidence, or observation was otherwise structurally impossible; or
- `confounded`: a mature measurement exists but competing actions or causes make
  the selected action's contribution non-identifiable.

The first vertical slice is predictive knowledge delivery. It freezes the
expected usefulness of `deliver`, `silent`, or `suppress` selection before the
route effect, then links any later objective measurement to the exact prediction,
decision, action, delivery event, and evaluation window.

The slice does not call Mnemos a general WorldModel, convert a trust/task-fit
score into a probability, treat a click/dismiss/reaction as objective truth,
train a model, or automatically update trust, policy, persona, or beliefs.
Feedback attribution remains COG-038 and trustworthy training remains COG-048.

## Pre-implementation evidence

The repository currently registers the name `prediction_record` and the schema
label `mnemos.prediction_record.v1`, but validates only five broad fields:
`prediction`, `baseline`, `measurement_window`, `confidence_method`, and
`causal_hypothesis`. There is no prediction-specific validator, deep module,
pre-delivery atomic seal, maturity owner, terminal-state machine, reproducible
calibration report, migration tool, or strict lineage audit.

`DecisionTrace` now stores structured `expected_outcomes`, but those fields are
decision evidence, not an independently versioned PredictionLedger. Existing
delivery and outcome tables remain operational projections. In particular,
`OutcomeRecorder` currently derives a binary label from a user reaction and
defaults unknown actions to positive; COG-037 must never accept those rows as an
objective measured outcome.

The read-only production inventory inspected on 2026-07-18 contains five
historical `channel='predictive_push'` delivery decisions:

| Decision | Count | Existing objective outcome |
| --- | ---: | ---: |
| `deliver` | 3 | 0 |
| `suppress` | 2 | 0 |
| **Total** | **5** | **0** |

The canonical state database currently has zero active `prediction_record`,
`outcome_measurement`, `user_reaction_event`, or `decision_trace` revisions.
The five old delivery rows do not contain an action-before prediction payload,
metric/unit contract, evaluation window, or objective outcome. They cannot be
promoted to active predictions.

## Implemented and reconciled state

The current worktree now implements the deep PredictionLedger, strict payload
contracts, pre-effect delivery seals, append-only outcome/terminal/correction
lifecycle, bounded daemon maturity service, deterministic categorical
calibration report, historical reconciliation CLI, and independent strict
lineage audit. The audit is wired into local gates, pre-commit, CI, and the
full-score required-gate denominator.

With the daemon confirmed stopped, the user reviewed and authorized the exact
five-object inventory (`deliver=3`, `suppress=2`) with
`inventory_hash=sha256:f11e1e0a48082ab56cf7b3be4754028a9f420aaf559b9090d7699523d763696a`
and
`object_manifest_hash=sha256:f913fae79fcb6e6ad5804a9503a76fad28ceac1479f4bf426ce2408c0c6a5ce7`.
The first apply inserted five quarantine objects and enabled prediction
enforcement with zero active PredictionRecord revisions.

The restore drill verified the manifest and restored logical preimage
`sha256:28e7f5a14e0868919ee3644f70e752512baca54f17a2b33bccb3cd4f9b4dee8f`.
The final reapply then reported `inserted=5/existing=0`; the zero-change replay
reported `inserted=0/existing=5`. All source, backup, restored, and final target
integrity checks returned `ok`. The final backup roots are
`Desktop/Mnemos-migration-backups/COG-037-final-2026-07-18` and
`Desktop/Mnemos-migration-backups/COG-037-replay-2026-07-18`.

Independent specification and repository-standards reviews are clean for the
COG-037 change set. The global maintainability closure still reports unrelated
active Phase 3 large-file/fingerprint debt, so this result is not described as
repository-wide standards or release closure. The final isolated Quick
denominator passed with `6734 passed` and `15 subtests passed`,
`outside_write_count=0`, and `formal_state_diff=[]`;
the post-fix isolated integration denominator passed with `350 passed` and the
Heavy denominator passed with `18 passed`. The final strict prediction audit
reports all lineage metrics at zero, including terminal projection receipt
tamper detection, five covered historical quarantine objects, and zero active
PredictionRecord revisions. Documentation closure and the local commit remain
outstanding.

## Chosen architecture

Add one deep owner, `core/cognitive/prediction_ledger.py`, with cohesive
`prediction_ledger_support.py` and `prediction_outcome_support.py` modules. The
owner controls prediction identity, validation, append-only lifecycle, outcome
matching, maturity, calibration eligibility, and deterministic reports while
persisting canonical revisions through the existing `CognitiveStateStore` and
`CognitiveStateUnitOfWork`. The outcome support module owns the fixed
TaskResultOracle issuance/revalidation seam; neither support module creates a
second persistence owner.

The module exposes one cohesive store with typed inputs and receipts:

```text
PredictionRecordStore.prepare_route_prediction(route_facts) -> PredictionPlan
PredictionRecordStore.seal_nonmaterial(plan, principal) -> PredictionSealReceipt
PredictionRecordStore.finalize(prediction_id, evidence, now) -> PredictionTerminalReceipt
PredictionRecordStore.correct_terminal(prediction_id, correction, principal) -> PredictionTerminalReceipt
PredictionRecordStore.reconcile_matured(now, limit) -> MaturityBatchReceipt
PredictionRecordStore.verify(prediction_revision_id, principal) -> PredictionVerification
PredictionRecordStore.calibration_report(query) -> PredictionCalibrationReport
```

`PredictionPlan` is system-created from the delivery router's exact route facts;
public callers cannot construct canonical IDs, hashes, windows, metric identity,
confidence semantics, delivery refs, terminal states, errors, or calibration
eligibility.

### Rejected approaches

- Extending only `DecisionTrace.expected_outcomes` would leave no independently
  queryable prediction state, terminal lifecycle, outcome binding, or calibration
  denominator.
- Creating a prediction table in `delivery_events.db` would introduce a second
  canonical owner and make prediction/outcome recovery depend on cross-database
  mutable state.
- Treating current trust/task-fit scores as probabilities would create fabricated
  Brier/ECE evidence. Those scores were not produced or calibrated as
  probabilities.
- Waiting for COG-038 before sealing predictions would allow new untraceable
  predictive deliveries and preserve the COG-037 root defect.

## `mnemos.prediction_record.v1`

Every active payload contains:

- system-owned `prediction_id`, revision state, supersedes ref, and immutable
  prediction-input hash;
- exact subject, scope, source snapshot/revision/hash, DecisionTrace revision,
  action ref, and deterministic delivery event ref;
- selected route disposition (`deliver`, `silent`, or `suppress`), derived from
  the exact `(decision, delivered_level)` pair rather than caller text;
- `prediction_kind`, metric ID, unit, predicted value, baseline, and the complete
  measurement specification;
- confidence method, method version, code/spec hashes, source score values, and
  an explicit `is_probability` flag;
- evaluation-window start/end, timezone, maturity policy, and causal assumptions;
- exposure evidence refs, objective outcome revision/hash, attribution method,
  competing causes, terminal state/reason, and error representation;
- calibration eligibility and exclusion reason; and
- the strictest derived CognitiveAccess envelope.

For predictive routes the application resolves the canonical Wiki ACL before
calling the delivery router. The router requires that exact
`source_access_control` plus a server-resolved write principal, validates its
owner and scope, and includes its canonical ACL hash in the material-action
input binding. The PredictionRecord ACL is derived from that source envelope;
the ledger cannot create a fixed caller-independent envelope or broaden the
source. Missing ACLs, owner drift, read-only principals, scope drift, and ACL
hash drift all fail before prediction or delivery effects are written.

The first vertical slice uses a categorical metric,
`predictive_delivery_usefulness`, with unit `class_label`. Its confidence method
is `delivery_policy_score_band.v1`: it records the exact trust/task-fit/
interruption inputs and a code-owned score band, but sets
`is_probability=false`. It therefore supports a confusion matrix and coverage
report, not Brier score or ECE.

The mapping is fixed: `decision='suppress'` maps to `suppress`;
`decision='deliver'` with `delivered_level='silent'` maps to `silent`; and every
other `decision='deliver'` maps to `deliver`. A silent delivery is still a
COG-036 material delivery and requires the same DecisionTrace/permit/effect
closure as a visible delivery.

The selected route disposition and predicted usefulness are separate fields. A
suppressed candidate remains a real pre-effect prediction, but because the
counterfactual was not exposed it normally matures as `censored`, not as a
measured success.

The canonical evaluation window is resolved from
`prediction.predictive_delivery_window_hours`, default 168 hours. The global
effective value and its config hash enter the prediction identity. Callers
cannot override the window per operation.

## Immutable revisions and terminal transitions

The first revision is `open`. The prediction input, subject, metric, unit,
predicted value, selected route, confidence method, evaluation window, causal
assumptions, snapshot, decision, and action bindings are immutable across all
later revisions.

Finalization appends one superseding revision instead of updating the open row.
The store accepts one of four terminal states and enforces:

- finalization before the window end is allowed only for a mature objective
  measurement whose observation period satisfies the measurement spec;
- `measured` requires exactly one matching OutcomeMeasurement revision and a
  recomputable error;
- `confounded` requires a matching measurement plus explicit evidence-backed
  competing causes;
- `unknown` requires verified exposure and the absence of a qualifying
  measurement at maturity;
- `censored` requires a structured censoring cause, such as missing presentation
  proof, retention deletion, revoked access, or unavailable follow-up;
- exact replay returns the existing terminal revision;
- a different second terminal state through ordinary `finalize()` is an
  immutable conflict; and
- `correct_terminal()` requires an authorized OutcomeMeasurement correction,
  exact `correction_of_revision_id`, and supersedes refs to both the prior
  measurement and terminal prediction revision. It appends a corrected terminal
  revision; no row is overwritten.

A routed delivery receipt is not presentation proof. The current
`transport-status:routed-not-presented` evidence can support route execution but
normally leads to `censored` if no later exposure proof appears. A future exact
UserReactionEvent may prove exposure, but the reaction value itself still cannot
prove usefulness or enter `measured`.

## OutcomeMeasurement eligibility

COG-037 strengthens the PredictionLedger's outcome admission seam without
pre-solving COG-038's feedback fanout. A measurement is prediction-eligible only
when it provides:

- exact prediction, decision, action, delivery, and subject refs;
- the same metric ID and unit as the sealed measurement spec;
- an observation period inside the allowed window and an explicit maturity time;
- raw evidence refs and content hashes, measurement method/version/code hash,
  baseline, observed value, and uncertainty;
- attribution method, confidence, and competing-cause evidence; and
- source authority and CognitiveAccess that permit this use.

The selected authority entry is additionally verified against canonical Raw:
revision hash, role-local bytes, exact span start/end, and span content hash must
all match. State validation and the independent audit separately recompute the
`source_authority_id` and catalog hash, so a self-consistent forged catalog does
not become eligible evidence.

Legacy `cognitive_outcomes`, `OutcomeRecorder` labels, click/dismiss feedback,
silence, timeout, and arbitrary caller metadata are not eligible. They may remain
readable operational history but cannot close a prediction as `measured`.

## Atomic pre-effect seal

The delivery router computes a route decision and a system-owned PredictionPlan
before writing `delivery_events.db`.

For `deliver`, including `delivered_level='silent'`,
`DecisionTraceStore.seal()` includes the PredictionRecord revision in the same
`BEGIN IMMEDIATE` transaction as ValueContext, pre-action snapshot,
DecisionTrace, semantic event, and material action command. Every DecisionTrace
gains a `prediction_refs` sequence; non-predictive decisions keep it empty,
while predictive delivery decisions require exactly the refs derived by the
domain evaluator. The command, delivery event, and target effect receipt repeat
the exact prediction revision/hash refs. Permit validation fails if any
prediction payload or binding drifts.

For `suppress`, the PredictionRecord is committed before the non-material
delivery event. Its local projection command binds the deterministic
delivery event ID and exact event payload hash. The router writes the append-only
event and records a reciprocal projection receipt. A crash after prediction seal
but before the event leaves a recoverable pending command; replay either writes
the one exact event or observes it. It never fabricates an outward material
effect.

After activation, every new `predictive_push` delivery event must carry an exact
PredictionRecord revision/hash ref. A missing, foreign, late, or post-effect
prediction fails closed.

## Maturity owner and daemon behavior

Add a focused `daemon/prediction_service.py` adapter and a registered
`prediction_maturity` service. The deep store remains the lifecycle owner; the
daemon only invokes `reconcile_matured()` with a bounded batch.

The service runs hourly by default through the existing daemon interval/registry
mechanism. It is idempotent and restart-safe:

1. read open predictions whose window has ended;
2. load only exact eligible objective measurements and exposure evidence;
3. derive one terminal state by the fixed state machine;
4. append the terminal revision and semantic receipt; and
5. leave a retryable item open only when the canonical store is temporarily
   unavailable, never because outcome evidence is absent.

Absence of evidence is resolved to `unknown` or `censored` at maturity, not
permanent retry. Only transient `sqlite3.OperationalError`/canonical-store
unavailability remains retryable; permanent SQLite contract errors append a
terminal censored revision and surface degraded service state. A terminal
projection receipt is accepted only when its command binds the canonical
terminal revision ID/hash/state, deterministic target and before/after hashes,
and all reciprocal refs; the strict audit independently recomputes those
bindings. Daemon transport success is not a terminal prediction proof.

## Deterministic calibration report

`PredictionCalibrationReport` is a pure read model rebuilt from immutable
prediction and OutcomeMeasurement revisions. The report binds:

- selected metric, method/version, code/spec hash, and time range;
- all matured prediction IDs and terminal revision hashes;
- measured/unknown/censored/confounded counts and coverage ratios;
- confusion matrix and categorical accuracy for the first slice;
- exclusions with reason codes; and
- canonical input hash and report hash.

The implementation identity hashes the actual bytes of
`prediction_ledger.py`, `prediction_ledger_support.py`, and
`prediction_outcome_support.py`; the specification identity hashes this file.
A change to score-band, route-disposition, terminal, objective-oracle, or
calibration logic therefore changes the recorded code/spec identity.

Only `measured` and unconfounded, calibration-eligible outcomes enter accuracy.
Unknown, censored, and confounded records remain in coverage denominators. Brier
score and ECE are emitted only for a future record with `is_probability=true` and
a declared probability method; the initial score bands never enter those
formulas. An empty measured set yields a reproducible `insufficient_sample`
report rather than invented accuracy.

## Historical object-level provenance reconciliation

Add `scripts/reconcile_prediction_history.py`. It inventories every historical
`delivery_events` row with `channel='predictive_push'` by exact database identity,
schema fingerprint, primary key, immutable row content hash, decision, and
timestamp.

The migration writes one `historical_unverifiable_prediction` quarantine entry
per source object into the existing canonical migration quarantine. It does not
create an active PredictionRecord, prediction value, confidence, evaluation
window, terminal outcome, error, calibration row, DecisionTrace, action, or
exposure proof.

Apply requires:

- daemon confirmed stopped and the daemon PID-file OS lock held;
- a fresh post-stop inventory and exact user-reviewed inventory hash;
- a new backup directory and verified SQLite backup of the canonical state DB;
- source schema/content revalidation immediately before commit;
- append-only quarantine plus the prediction-enforcement activation marker in
  one transaction;
- post-apply integrity, row-count, uniqueness, zero-active-revision, and logical
  snapshot verification; and
- manifest-driven restore plus dry-run/replay tests.

The current count of five is evidence for the inspected snapshot, not a hard-coded
denominator. Source drift before apply requires a new dry-run and review.

## Activation and backward boundary

Fresh state stores install strict prediction enforcement. Existing stores without
the registered activation component report `migration_required`; predictive
delivery fails closed rather than silently emitting a legacy event.

The strict audit separates:

- pre-activation historical objects, all of which must be exactly quarantined;
- post-activation runtime delivery decisions, all of which must have a pre-effect
  PredictionRecord; and
- current open predictions, which are valid only before their evaluation window
  matures.

Historical quarantine never satisfies a runtime prediction or calibration
denominator.

## Strict independent audit

Add `scripts/audit_prediction_outcome_lineage.py --strict --json` and wire it into
local gates, pre-commit, CI, and full-score required gates. It independently
recomputes schema ownership, activation, source history, runtime delivery links,
revision hashes, state transitions, outcome eligibility, error math, maturity,
daemon registration, and calibration reports.

Required zero metrics include:

- `outcome_without_prediction=0`;
- `mature_prediction_without_terminal=0`;
- `predictive_delivery_without_presealed_prediction=0`;
- `prediction_after_delivery_effect=0`;
- `prediction_decision_action_binding_mismatch=0`;
- `prediction_payload_hash_mismatch=0`;
- `prediction_terminal_conflict=0`;
- `reaction_used_as_objective_outcome=0`;
- `ineligible_measurement_used_for_error=0`;
- `score_band_used_as_probability=0`;
- `calibration_input_or_report_hash_mismatch=0`;
- `historical_prediction_inference_count=0`; and
- `historical_predictive_object_uncovered=0`.

The report must expose open-not-mature, measured, unknown, censored, confounded,
historical quarantine, and calibration-eligible denominators rather than hiding
them behind a single pass/fail count.

## Verification matrix

Tests cover:

- deliver, silent, and suppress pre-effect prediction seals;
- prediction/decision/action/delivery identity and hash drift;
- crashes after prediction, decision, event, target effect, and terminal receipt;
- exact replay and conflicting replay;
- early, on-time, late, missing, corrected, and duplicate outcomes;
- metric, unit, subject, window, authority, ACL, and attribution mismatches;
- measured, unknown, censored, and confounded transitions;
- reactions proving neither usefulness nor objective outcome;
- categorical confusion-matrix reproducibility and zero-sample reports;
- rejection of Brier/ECE for non-probability score bands;
- daemon restart, bounded batches, and mature no-evidence closure;
- five-object and drifted historical inventories, backup/restore, apply/replay,
  schema corruption, and migration rollback;
- strict audit tamper fixtures; and
- complete Quick plus isolated environment manifest verification.

No test may weaken the old delivery, DecisionTrace, source-authority, ACL,
trusted-push, schema-registry, or hermetic-write contracts.

## Documentation and completion evidence

Implementation updates the canonical schema/operations documentation, changelog,
generated event/dependency maps, Desktop `mnemos系统图谱`, and the original
`Mnemos-Phase0-7全局工程修复合同-2026-07-24.md` 中对应 COG-037 的
append-only 状态历史；2026-07-12 文档仅作冻结 provenance，不再追加当前状态。

COG-037 is complete only when:

1. the written spec and implementation commits are identified;
2. the production dry-run inventory is reviewed and the authorized apply,
   restore drill, final reapply, and zero-change replay succeed;
3. the strict audit and all existing required audits pass;
4. focused tests and complete isolated Quick pass with
   `outside_write_count=0` and `formal_state_diff=[]`;
5. repo and Desktop documentation gates pass with current commit-bound evidence;
   and
6. the worktree is clean after a local implementation commit.

This closure does not claim COG-038, COG-048, global maintainability/zombie
closure, real-API full-score certification, or release eligibility.
