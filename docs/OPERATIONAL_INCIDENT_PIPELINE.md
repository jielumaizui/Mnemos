# Operational Incident Pipeline

## Scope

This contract owns machine failures raised by the distillation pipeline. It
replaces the former path that created `ForcedRetrospective` tasks directly
from validation failures. Human retrospectives remain a separate domain and
may be proposed only after an incident is resolved and a reusable lesson has
been identified.

The canonical flow is:

```text
IncidentOccurrence
  -> OperationalIncident
  -> DiagnosticTask
  -> RootCauseReport
  -> NotificationCommand
  -> NotificationReceipt
  -> ReplayCommand
  -> ReplayReceipt
  -> Resolution
  -> optional RetrospectiveProposal
```

## Canonical owner and identity

`core/ops/operational_incident.py` is the sole schema and state-transition
owner; shared replay identity hashing lives in the narrow
`core/ops/operational_incident_identity.py` helper. `operational_incidents.db` is registered with
`mnemos.operational_incident.v1`; readers and runtime constructors validate
the registry and physical SQLite objects but never create or migrate them.

Every failed attempt creates one collision-safe artifact and one immutable
occurrence. An active incident fingerprint contains only stable root-cause
dimensions:

- failure class and stable error codes;
- source family and producer;
- immutable execution-spec and exact prompt identity;
- provider, model, and route;
- output schema, parser, and validator hashes.

Variable validation text, session IDs, artifact paths, and timestamps remain
occurrence evidence and never split or overwrite the stable incident.
Artifact bytes are SHA-256 verified before an occurrence can be committed.
The occurrence and its `incident_ingest_receipts` committed receipt are
inserted in the same transaction. If the incident store is temporarily
unavailable, the `0600` artifact remains explicitly `status=pending`; the
bounded daemon consumer later ingests it exactly once and generic retention
remains fail-closed until registration succeeds. Conversation and document
provider failures use this same ingress owner.
Every occurrence also binds the lossless visible-input hash, response hash,
canonical Raw event revisions, restricted diagnostic ACL, and unresolved
incident retention class. Failure artifacts are created with mode `0600`,
access by the formal replay path is append-only audited, and unresolved
artifact paths are protected from generic retention cleanup.

## Diagnosis, notification, and resolution

The first occurrence creates exactly one durable diagnostic task. Diagnosis
collects occurrence, artifact, prompt, input, response, Raw event,
execution-spec, provider/model/route, schema, parser, and validator
references. It retains explicit empty-transport evidence when applicable,
a reproduction command, repair guidance, and verification requirements.
All automatically classified provider/schema codes are symptoms and remain
`investigating`; validation text is never promoted to a confirmed root cause
by itself. A later evidence-backed diagnosis is an append-only report
revision rather than an overwrite. Confirmation requires
`formal_diagnostic_reproducer.v1` to execute the actual before and after
reproducers; it persists only a before-failed/after-passed proof with result
hashes, source references, and the reproduction command. Caller-authored
status strings cannot confirm a root cause. Each registered reproducer also
maps to exactly one canonical root-cause code; a valid proof cannot be reused
to confirm a different diagnosis.

The production/manual diagnostic entrypoint is
`scripts/diagnose_operational_incident.py`. It accepts only allowlisted
reproducer IDs and exact before/after JSON fixtures, hashes both fixture files
into the evidence refs, runs the production domain validator, and requires an
explicit `--confirm-record-evidence` mutation flag.

A notification command is created only after a root-cause report exists.
Delivery uses a stable idempotency key, an expiring lease, append-only
attempts, and a terminal receipt. Notification content reports status and
report identity only; it does not create a retrospective or suggest a direct
Wiki write. `daemon/operational_incident_service.py` is the bounded runtime
consumer for pending diagnostic tasks and notification commands.

Resolution requires both a confirmed diagnosis and a committed formal replay
receipt. `scripts/replay_distillation_failure.py` first performs a read-only
plan that verifies incident, occurrence, report, and artifact bindings.
`--apply` additionally requires the exact database-scoped plan hash, exact
artifact hash, the canonical Raw database, and `--confirm-send-content`.
The command accepts no caller-provided session text or messages: it loads the
exact bound Raw revisions, checks their session and stored content hashes, and
rebuilds the lossless visible input. That input must equal the occurrence's
bound visible-input hash; the replay result's input spec must return the same
visible-input and Raw revision bindings. Replay command and
receipt both store the same canonical input-binding hash, and only the
`formal_distillation_replay.v1` executor may commit it. It re-runs extraction
without writing Wiki pages and appends a terminal replay receipt. A separate
resolution action must bind the repair and verification evidence.

## Reconciliation and rollback

The supported historical migration entrypoint is:

```bash
python3 scripts/reconcile_operational_incidents.py --json
```

Dry-run is the default and must not create a database, WAL, backup, receipt,
or reminder. Apply is offline-only:

```bash
python3 scripts/reconcile_operational_incidents.py \
  --apply \
  --expected-plan-hash <sha256> \
  --backup-dir <existing-reviewed-scope> \
  --json
```

Apply requires all daemon and MCP writers to be stopped, acquires the shared
offline-migration locks, and creates integrity-checked SQLite backups before
mutation. Legacy artifacts are migrated with a durable source/hash ledger.
Exact legacy recaps are marked `superseded_by_operational_incident`, not
`resolved` or `consumed`; any existing session, skip, plan, command, or
receipt blocks the transition so consumption state cannot be discarded.
Exact reminder files are backed up and moved from the active reminder tree
into `99-Archive/OperationalIncidentLegacy`; they are never copied into
`00-Inbox`. The move is sealed by an exact project-contract material action
and a trusted Wiki create/move lifecycle. A separate append-only
processing/completion event ledger closes the crash window: recovery accepts
an already moved file only when its exact hash matches and the corresponding
Wiki move mutation has a durable event trace. Intercepted or uncommitted
trusted moves fail closed and never receive an archived disposition.
Ambiguous recaps/reminders are preserved and recorded in the
append-only `legacy_incident_quarantine` table for manual classification.
Apply reports a source-to-disposition conservation equation, and re-running
an already disposed plan is a no-op.

Rollback uses the generated SQLite backups while writers remain stopped.
Backups are never deleted by the reconciliation command.

## Audit and acceptance

Static code-path diagnostics:

```bash
python3 scripts/audit_operational_incident_pipeline.py \
  --static-only --strict --json
```

Required isolated runtime contract:

```bash
python3 scripts/audit_operational_incident_pipeline.py \
  --self-test --strict --json
```

Target-store closure:

```bash
python3 scripts/audit_operational_incident_pipeline.py \
  --db <operational_incidents.db> --strict --json
```

The target-store audit independently recomputes orphan, complete diagnostic
binding, artifact ACL/retention/access, delivery-receipt, replay-resolution,
retrospective-order, quarantine, and active fingerprint-duplicate gaps. The
full-score contract runs the isolated runtime mode rather than treating
static source inspection as functional proof. Its successful exit means
`contract_gate_eligible=true` for the isolated runtime contract only;
`production_effect_verified=false` and `production_release_eligible=false`
remain explicit until the separately authorized production workflow runs.
The required acceptance corpus also proves:

- ten same-root failures produce one incident, ten occurrences, and one
  diagnostic task;
- variable text does not split a cluster, while a schema version change does;
- empty transport is diagnosed from explicit evidence before notification;
- notification failure remains replayable and produces one success receipt;
- artifact tampering is rejected;
- reconciliation is read-only by default, binds database and Wiki scope,
  backs up before apply, conserves every source disposition, transitions only
  exact legacy bindings, quarantines ambiguity, and is idempotent;
- formal replay rejects substituted messages, is plan/input/Raw-bound, and
  produces a terminal receipt;
- pending artifacts are consumed exactly once with a durable ingest receipt,
  and document provider failures enter the same incident root;
- diagnostic confirmation rejects self-reported pass/pass evidence;
- diagnostic confirmation rejects an unrelated same-class artifact fixture
  and a root-cause code not mapped to the selected registered proof;
- reminder reconciliation recovers a move/disposition crash only from an
  exact published Wiki lifecycle effect and binds a recovered create receipt
  to the current page identity rather than a reused path;
- legacy recap call sites and direct `00-Inbox` bypass advice are both zero.

Passing these code and isolated-store contracts does not prove production
migration, live notification delivery, real-API replay, readiness, or release
certification. Those require separately authorized production operations and
their resulting receipts.
