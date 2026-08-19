# Phase 3 cross-domain aggregate reclosure implementation plan

## Governing contract

Implement the user-approved design in
`docs/superpowers/specs/2026-07-19-phase3-cross-domain-reclosure-design.md`,
starting from design commit `3b7b983f`.

The active state is `REOPENED_CROSS_DOMAIN_BLOCKED`. Work remains serial: one
red test, one root repair, one green verification, and one reviewable commit at
a time. Daemon and Mnemos MCP services remain stopped throughout implementation
and production reconciliation.

## Commit 1: typed Belief-to-Decision snapshot authorization

### Red tests

Add public-path tests proving:

- a same-principal, same-project, same-session canonical Belief appears in a
  sealed Decision snapshot;
- the read summary reports the Belief candidate and `belief_read`
  authorization;
- wrong principal, agent, project, session, visibility, consent, or purpose is
  denied before semantic body fetch;
- callers cannot supply a purpose map or trigger a fallback purpose; and
- mixed authorized sources derive a resolved strictest Decision ACL and bind a
  source-purpose contract hash.

Primary test files:

- `tests/unit/cognitive/test_decision_trace.py`;
- `tests/unit/cognitive/test_cognitive_access_control.py`; and
- `tests/integration/test_phase3_cognitive_chain_e2e.py`.

### Implementation

- Add `core/cognitive/decision_snapshot_access.py` as the only fixed mapping
  owner and contract-hash owner.
- Add typed authorized-source and authorization-summary models.
- Add an ACL-first store operation that queries each supported source type with
  its fixed purpose and returns only authorized bodies.
- Add a consumer-bound strictest-access derivation without weakening
  `derive_strictest_cognitive_access()`.
- Replace the single generic read in `DecisionTraceStore.seal()`.
- Persist per-source purpose, ACL hash, and contract hash in snapshot lineage.
- Extend COG-035/036 audits with a cross-domain purpose-contract denominator.

### Verification

Run the focused unit/integration tests, COG-035 strict audit, COG-036 strict
audit, static write-owner audit, and `git diff --check`. Commit only when the
new positive test and all existing negative ACL tests pass.

## Commit 2: durable objective-attribution admission intake

### Red tests

Add failpoint tests at these exact boundaries:

1. before feedback target completion;
2. after the `training_evidence` target receipt;
3. after admission revision commit;
4. after scoring sample projection; and
5. before admission-intake terminal receipt.

Each restart must converge one admission, one sample, and one intake receipt.
The crash after target receipt must leave a pending admission command even when
feedback pending count is zero.

### Implementation

- Define `TRAINING_ADMISSION_CONSUMER`, command type, payload contract, and
  contract hash in the training contract owner.
- Extend objective attribution to create one admission command in the same UoW
  as attribution and COG-038 target commands.
- Keep COG-038 `command_ids` limited to feedback targets and expose the exact
  admission command separately.
- Bind the intake to attribution, current Outcome, required target-command
  manifest, source ACL/principal identity, scope, correction lineage, and
  contract hash.
- Add `TrainingGovernanceStore.reconcile_admission_intakes()` and exact
  single-command processing.
- Allow only the governed admission command contract to use the generic
  non-feedback effect receipt despite being attached to a feedback attribution
  revision; retain specialized closure for all COG-038 feedback commands.
- Make `OutcomeRecorder` request low-latency reconciliation of the durable
  command rather than directly owning admission.
- Run admission reconciliation before projection/run work in
  `daemon/training_governance_service.py`.

### Verification

Run feedback-attribution store/entrypoint/command-closure tests,
OutcomeRecorder tests, training-governance tests, the new crash/restart
integration matrix, COG-038 strict audit, and COG-048 static/full test audits.

## Commit 3: maturity and current terminal-Prediction enforcement

### Red tests

- Future `matured_at`: intake remains pending, admission/sample count stays
  zero, and no terminal intake receipt exists.
- Exact maturity boundary: the same intake commits once.
- Open, censored, confounded, unknown, stale, wrong-Outcome, and tombstoned
  Prediction heads do not admit.
- The feature snapshot still hashes only the sealed pre-outcome Prediction.
- Direct admission APIs cannot hard-code or caller-select maturity.

### Implementation

- Add a typed retryable `TrainingEvidenceNotReady` result/error.
- In evidence resolution, compare the injected governance clock with the exact
  timezone-aware `matured_at`.
- Resolve the current Prediction head and require terminal `measured`, exact
  Outcome binding, proven exposure, and no calibration exclusion.
- Advance the admission payload to v2 with separate sealed
  `prediction_ref` and current `prediction_terminal_ref`.
- Derive `temporal_proof.maturity` only after successful verification.
- Update payload validators, projection verification, schema registries,
  hashes, audits, and test fixtures.

### Verification

Run the COG-037 prediction/outcome suite and strict audit together with all
training admission/projection tests and contract/schema audits.

## Commit 4: automatic correction, exclusion, stale, and rebuild

### Red tests

- Public corrected Outcome automatically uses correction dispatch without a
  direct `correct_admission()` caller.
- The old admission transitions through correction-pending to excluded; its
  scoring sample is inactive.
- Every dependent run/model and active model head becomes stale/unloadable.
- The corrected admission becomes the only active sample and deterministic
  rebuild produces the only loadable model lineage.
- Zero-prior-admission correction supersedes the old pending intake without
  requiring a fabricated old sample.
- Crashes at each correction phase replay only missing effects.

### Implementation

- Derive correction mode from exact Outcome outer/payload lineage inside the
  intake dispatcher.
- Extend pending-command supersession to the governed admission consumer.
- Refactor correction into idempotent internal phases with independently
  readable receipts.
- Accept zero or one prior current admission; more than one fails closed.
- Ensure run stale propagation invalidates governed model heads/loaders.
- Keep the corrected intake pending until maturity/current terminal proof is
  ready, then admit once.
- Make the daemon rebuild stale dimensions after intake convergence.

### Verification

Run correction, exclusion, tombstone, run/model lifecycle, daemon restart, and
public OutcomeRecorder tests plus COG-038/048 strict audits.

## Commit 5: real upstream verification and canonical model evidence

### Red tests

For each upstream class, mutate one canonical chain after admission and prove a
new run/model write is rejected:

- missing/stale intake command or receipt;
- missing/stale attribution;
- missing/stale Outcome;
- missing sealed or terminal Prediction;
- mismatched Decision/action/material receipt;
- corrected-away or tombstoned sample; and
- mismatched scoring projection receipt.

### Implementation

- Expand `_verified_current_admissions()` into a full upstream-chain verifier.
- Reuse canonical COG-036/037/038 verification owners; do not duplicate loose
  hash checks or trust admission body fields.
- Reject the complete run before readiness, fit, holdout, seal, model artifact,
  or active-head writes when any upstream proof fails.
- Replace the 24 fabricated scorer-v2 admissions with enough full public
  Decision-to-Model chains to satisfy deterministic train/validation/holdout
  readiness and both labels.
- Keep fixture builders in test support code, but require them to call public
  production APIs rather than insert cognitive rows.

### Verification

Run the complete scorer-v2 training loop, training governance store/run/model
tests, mutation matrix, COG-036/037/038/048 strict audits, and changed-test
selection.

## Commit 6: reconciliation, aggregate audit, and required gates

### Red tests

- A pre-contract objective attribution with complete canonical proofs produces
  exactly one proposed intake in dry-run and one command on apply.
- Missing ACL, Outcome, command, receipt, or currentness is classified and not
  backfilled.
- Backup failure, integrity failure, apply failure, and replay conflict roll
  back without partial state.
- Second dry-run/replay reports zero changes.
- Aggregate audit detects each of the five original defects independently even
  when all individual domain audit summaries are green.

### Implementation

- Add `scripts/reconcile_phase3_training_admission_intakes.py` with stopped-
  service enforcement, SQLite backup API, inventory/object-manifest hashes,
  exact apply, rollback, integrity checks, and idempotent replay.
- Add `scripts/audit_phase3_cognitive_chain.py --strict --json` with the
  zero-budget metrics from the design.
- Replace COG-048's hard-coded formal-producer denominator with actual intake
  commands and receipts.
- Index both admission-intake and governed-projection consumers.
- Register the aggregate audit in local, pre-commit/CI, and full-score required
  gate manifests without weakening static/runtime/certifying boundaries.
- Update architecture dependency facts/generators when new production symbols
  change generated maps.

### Verification

Run script unit tests, migration failpoint tests, schema registry, architecture
graph, local-gate manifest tests, all Phase 3 strict audits, aggregate strict
audit, and document/prompt/security static audits affected by the new files.

## Production reconciliation and closure verification

After all implementation commits are reviewed and the worktree is clean:

1. confirm daemon/MCP processes remain stopped;
2. run the intake reconciliation dry-run against production;
3. review exact denominator, inventory hash, object-manifest hash, proposed
   writes, and unresolved classifications;
4. if non-empty and fully provable, apply with a reviewed backup directory;
5. run post-apply dry-run, exact replay, and `PRAGMA integrity_check` for state
   and scoring databases;
6. run full runtime COG-035/036/037/038/048 and aggregate Phase 3 audits;
7. run hermetic changed tests, Quick, Integration, and Heavy on one commit;
8. invoke the review skill for independent Standards and Spec review, repair
   every actionable finding, and rerun affected evidence;
9. synchronize repository docs, Desktop `mnemos系统图谱`, and the governing
   Desktop audit source in place; and
10. commit final code/docs locally while leaving services stopped.

## Commit 7: historical cognitive-action target-state reconciliation

### Red tests

- A legacy Observation receipt remains valid after legal calibration, ACL,
  source-span, update-time, and row-version evolution only after an exact
  object-level reconciliation.
- Changing value, unit, source identity, evidence, or original
  semantic timestamps makes the object ineligible and keeps the audit red.
- The planner rejects a matching `source_id` when the immutable action artifact
  does not reconstruct the current v3 semantic projection.
- Apply preserves every original action/effect/target-receipt/material hash,
  appends one immutable target-owned row, and makes replay a no-op.
- Inventory drift, manifest drift, backup failure, SQLite failure, or an active
  daemon/MCP process causes zero writes and a failing report.

### Implementation

- Advance the Observation action-owned target projection to canonical v3 and
  centralize JSON/timestamp normalization plus artifact-to-expected-state
  reconstruction in `core.hephaestus.cognitive_action_targets`.
- Make new target receipts bind v3 directly and keep legacy exact-hash
  verification available for unchanged rows.
- Add a focused planner/executor and
  `scripts/reconcile_cognitive_action_target_states.py` with exact inventory and
  object-manifest hashes, canonical persisted inventory JSON, shared
  runtime-quiescence guard, SQLite backups, append-only target-side
  reconciliation rows/triggers, rollback, integrity, and idempotent replay.
- Extend the strict cognitive-action effect audit to validate each
  reconciliation independently against the immutable command, effect, receipt,
  artifact, stored migration-time row snapshot, and current action-owned target
  state; it must also recompute batch/inventory/object hashes and reject weakened
  key/check/FK/trigger schemas. No caller-selectable contract or bypass is
  accepted.

### Verification

Run the focused unit/failpoint suite, strict COG-014 action-effect audit,
COG-036 Decision/material lineage audit, strict aggregate Phase 3 audit, schema
and static-write-owner gates, `git diff --check`, and production dry-run. Apply
only against the reviewed object manifest with both action and target databases
backed up; then require zero post-apply candidates and `integrity_check=ok`.

Phase 3 can be marked aggregate closed only after every acceptance criterion in
the governing design is evidenced. The result remains distinct from a global
release certificate and must list any unrelated repository release blockers.
