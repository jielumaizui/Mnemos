# AgentSource Support Manifest

`core/agent_kit/agent_source_support_manifest.json` is the sole tracked
definition of Mnemos local AgentSource support. Runtime code may only derive
views from it or validate against it; it must not introduce a second editable
agent/source list.

The manifest currently declares eight `host_agent` entries: Codex, Claude,
Hermes, OpenCode, OpenClaw, Crush, Kiro, and Kimi. They are the only entries
eligible for Agent Kit active workflow, authorized runtime probes, and the
8/8 full-power denominator.

Aider, Gemini, Cursor, and Windsurf are `ingestion-only` entries. Their
parsers remain enabled, and each must preserve the declared native-to-Raw,
ACL, fidelity, retention, continuous, and backfill contract. They never count
as host full-power agents and cannot receive a host active-entrypoint or
runtime-probe receipt.

## Claude native artifact layout

The Claude manifest format `projects/**/*.jsonl` is recursive, not a bounded
directory-depth promise. `ClaudeSource` enumerates every native project JSONL
under the resolved `projects` root. Ordinary project transcripts keep their
native filename session ID unless that filename collides across projects, in
which case a deterministic artifact suffix prevents sync/Raw rows from collapsing
two different transcripts. A JSONL below the native
`<project>/<parent-session>/subagents/...` layout is a separate
`source_kind=subagent`: its canonical session ID is artifact-specific and its
metadata retains `parent_session_id`, `parent_relation`, and an opaque stable
`source_artifact_id`. The subagent text is never folded into the parent
transcript. Discovery itself reads paths and metadata only; parsing native
content remains subject to the source-content authorization boundary.

## OpenClaw multi-format sessions

OpenClaw declares its three native artifact variants in
`native.format_resolution`: trajectory JSONL, ordinary session JSONL, and
daily corpus. Each variant has a manifest-owned `source_kind`, `path_glob`,
strict priority, `native_session_id` identity rule, and turn-fingerprint
equivalence rule. The loader and independent manifest audit reject a missing,
duplicate, malformed, or unprioritized variant contract.

`OpenClawSource` enumerates all three variants before it chooses a canonical
session; it must never return early after finding a trajectory. Equivalent
content for one native session becomes one canonical session with opaque
artifact provenance. If one artifact is a strict prefix of another, the longer
visible/structured turn sequence wins as a content extension, even when its
format has lower priority. Only a tie uses the declared format priority. If
two same-ID artifacts cannot be proven equivalent or prefix-related, they are
not silently overwritten: each receives an opaque artifact-qualified canonical
identity.

Corpus files are split by the embedded native session ID and parsed through
`parse_session(SessionInfo)`, so two sessions in one daily corpus do not fold
into one another. The former `corpus_fallback` session kind remains readable
for retry of historical sync records, but new discovery always emits the
precise `corpus` kind. Discovery/parsing of real OpenClaw artifacts is still
subject to the explicit source-content authorization boundary.

## Kimi native artifact sessions

Kimi declares `native.artifact_resolution` as the only owner of its four
native artifact kinds: `main_context`, `subagent_context`, `main_wire`, and
`subagent_wire`. Each declaration binds a path glob, layout selector,
`native_artifact_id` identity rule, aggregation rule, and parent-lineage rule.
The runtime loader and the independent manifest auditor reject missing,
duplicated, or malformed artifact declarations.

`KimiSource` discovers every declared artifact separately. A main context only
aggregates sibling `context*.jsonl` archive segments in declared numeric-then-
active order; it never adds wire or subagent bodies to its `source_files`.
Each wire file is parsed as its own artifact, and every subagent context/wire
retains the native parent session ID plus a `parent_relation`. Canonical IDs
are artifact-qualified whenever needed to avoid collision, while opaque
`source_artifact_id` metadata always distinguishes the physical native
artifact.

Archive events are deduplicated only when an explicit native event/message ID
proves they are the same event. A generic `id` or repeated visible text alone
is insufficient evidence and stays lossless. `KimiAdapter` propagates the
canonical artifact ID together with `native_session_id`, `source_kind`,
`source_artifact_id`, and `parent_session_id`, so downstream signal handling
cannot silently re-collapse a child or wire artifact into the main session.
Discovery/parsing of real Kimi artifacts remains subject to the explicit
source-content authorization boundary.

## Multi-root database sources

A source whose native history may live in more than one database root declares
that fact in its manifest `root_resolver.multi_root` contract.  For Crush the
contract is `mode=all_valid` with project-ancestor discovery enabled: explicit
configuration and environment roots establish owner priority, but they cannot
make another simultaneously valid declared root disappear from discovery.

`CrushSource` opens every valid `crush.db` read-only and exposes every observed
database parent to daemon/backfill snapshots.  A native session is reconciled
by its native session ID plus deterministic content evidence. Equivalent copies
collapse to one canonical session while retaining opaque source-database
provenance. If one native session ID has divergent content in different
databases, each version receives a distinct opaque database-qualified canonical
identity; neither is silently overwritten or selected by current working
directory.

`AgentSource.parse_session(SessionInfo)` is the session-aware parsing seam used
by SyncEngine, daemon raw sync, CLI sync, CaptureWorker, Kimi integration, and
raw-only backfill. File sources keep the default `parse_turns(source_path)`
behavior. Database sources override it so a discovered session retains the
database and native-session identity that discovery proved. Legacy external
sources without the new method retain the compatibility fallback, but new
database adapters must not use mutable discovery queues or a “latest session”
fallback for a discovered `SessionInfo`.

## Continuous capture owner

Every active manifest entry declares the same explicit continuous-capture
contract: `owner=daemon.raw_sync`, `service=raw_sync`,
`activation_key=daemon.services.raw_sync`, its trigger accelerator, a
600-second poll interval, and a 1200-second discovery SLA. `raw_sync` is
enabled by default and is the continuous owner; a watchdog, polling, or hybrid
trigger only accelerates a dirty source. A disabled scheduled scan must never
make the TriggerDispatcher discard an already-observed change.

## Durable completeness reconciliation

`daemon/agent_sync_cursor.py::AgentSyncCursorStore` owns the durable,
`0600` `<database_dir>/agent_sync_cursors.db` ledger used by the scheduled
owner. It stores a per-source, per-canonical-session `next_turn_number` and a
separate round-robin reconciliation cursor. The latter chooses the next bounded
work batch; it is not a completeness claim and it must never replace the
per-session Raw high-water mark. Schema v4 binds each complete
capture generation to the exact `native_source_snapshot_hash`; changing only
the heartbeat hash cannot re-label an existing cursor/Raw generation. Every
discovered session also has exactly one content-bound disposition:
`parsed`, `typed_empty`, or `evidence_excluded`.

Existing v1/v2/v3 ledgers require the backup-first explicit migration below.
The migration leaves new proof fields empty, marks historical session rows
`legacy_unverified`, and invalidates prior snapshot eligibility instead of
inventing evidence; a separately authorized Raw-only reconciliation must
create fresh bindings and dispositions.

```bash
# Stop every Mnemos daemon/MCP writer before producing the reviewed apply plan.
python3 scripts/reconcile_agent_sync_cursor_schema.py --backup-dir <dir> --json
python3 scripts/reconcile_agent_sync_cursor_schema.py \
  --apply \
  --expected-plan-hash <sha256:...> \
  --backup-dir <dir> \
  --json
```

The dry-run plan is bound to COG-045/RM-SCHEMA, the exact cursor DB path and
source hash, the v1/v2/v3 schema and integrity/FK state, the writer-state
precondition, and the allowed v4 delta. Apply acquires the shared offline
migration lock, recomputes that plan inside the lock, and refuses any changed
hash before backup or mutation. The implementation verifies the SQLite backup
against the reviewed logical snapshot before changing the source, compares
every legacy data table after the first apply, restores the original database
on a migration/comparator failure, verifies a restore drill, and persists a
plan-bound receipt. Repeating the same authorized plan is accepted only when
that receipt and the exact post-state still match; it performs zero physical
and semantic writes. A v4 post-check must report `required_gap=0`.

After that schema migration, Raw-only reconciliation is a separate
COG-045/RM-IDENTITY authorization. Its dry-run reads the complete 12-source
native roster (8 host plus 4 ingestion-only), so it must not be run without
explicit native-history access authorization:

```bash
python3 scripts/reconcile_agent_source_raw_capture.py \
  --confirm-read-native-history --backup-dir <dir> --json
python3 scripts/reconcile_agent_source_raw_capture.py \
  --apply \
  --confirm-read-native-history \
  --expected-plan-hash <sha256:...> \
  --backup-dir <dir> \
  --json
```

That plan binds the 12-active-source roster and manifest, preserving the 8/4
role split, plus a parser-owned
`NativeArtifactInventory`, the identity snapshot, Raw/cursor/coverage logical
hashes, recovery limits, reset mode, writer state, backup directory, and the
physical Raw-only delta classes. Each parser declares every artifact whose
bytes can affect a session parse; Kimi context sessions therefore bind all
`context*.jsonl` segments, not only the primary `SessionInfo.source_path`.
Public evidence contains content-free hashes for the resolved root, opaque
artifact identity, source/session mapping, and logical content (including
committed SQLite WAL state), but no native path or transcript body.

Apply runs parser expansion in an isolated, RSS-monitored worker with a private
spool. The worker is killed before it may exceed the plan-bound 256 MiB /
1,000,000-turn limit; only bounded, validated turns enter the parent process.
The complete inventory is compared before and after that freeze, so the reviewed
plan and Raw writer consume one immutable input generation. A resolved,
existing root plus an empty session/artifact roster is preserved as explicit
verified-empty evidence; a missing root remains not detected. Apply then
recomputes the plan under the shared offline migration lock, backs up only
Raw/cursor/coverage, and verifies the first-apply challenger, source-capture
receipts, full pre-existing Raw-row/hash/provenance/ACL/retention conservation,
allowed mutation boundary, private-mode clone restore drill, and an independently
rerun post-apply gap oracle. Any execution, interruption, or evidence failure
restores all three targets to their exact pre-state and invalidates the inner
completion receipt. A plan-bound completion receipt allows a
genuine same-plan second apply only when both the native inventory and target
post-state remain exact and its before comparator can be regenerated from the
sealed backup; that path returns zero physical and semantic delta. The outer
migration receipt also binds the private inner reconciliation receipt by exact
filename and SHA-256, then revalidates its reviewed plan, support manifest,
active-source denominator, backups/pre-state, final challenger, per-source
capture, Raw-only mutation boundary, and session-identity result. Rehashing the
outer receipt cannot grant credit to a semantically altered inner receipt.
The prepared outer receipt first binds the exact inner prepared-file hash and,
before certification, adds the exact completed-file hash. Interrupted and
recovered-rollback paths revalidate schema/plan/code, backup pre-state, prepared
lineage, and the current inner hash. Before retrying a failed same-plan attempt,
the terminal rollback receipt is archived byte-for-byte under its plan and
receipt hashes; a later success cannot overwrite that historical node. The
same-plan verifier rechecks every archived filename, private mode, receipt
hash, plan hash, rollback terminal, and requires every archived node's
`prior_terminal_receipts` to equal the exact ordered prefix before it. Any
number of retries therefore inherits the full ancestor chain. A process exit
after archiving but before the next prepared intent may expose only the unique
archive whose hash matches the current rollback receipt; recovery appends that
node to the same chain. Missing, extra, reordered, forked, or drifted history
fails closed.
Retryable parser or worker-budget faults remain typed in each generation's
content-free evidence. They may be marked recovered only when a later
generation over the same frozen snapshot produces a fully green final
challenger, every source-capture receipt, and the exact mutation boundary.
Their historical presence must not poison that independently verified final
generation. A remaining source gap fails as `raw_reconciliation_incomplete`;
identity/schema failures in the non-retryable allowlist still stop and roll
back on their first occurrence. Every cycle's numeric error count must exactly
equal the sum of its typed error evidence. Unattributed, under-counted, or
over-counted errors fail closed instead of borrowing credit from a later green
capture.

The COG-045 repair budget explicitly authorizes exactly one schema delta and
one migration delta for this RM-IDENTITY boundary:
`append_only_native_session_identity_reconciliation_ledger` and
`raw_rebuild_exact_plan_identity_preflight_and_rollback`. The ledger is not
created by normal `RawEventStore` startup. Only the reviewed, backup-first Raw
rebuild may create it and append plan-bound approvals. Each approval binds the
exact legacy identity set, source artifact, full historical Raw row hash,
current revision and logical-content hash, and complete revision-set hash.
Any later row, current pointer, content, or revision drift invalidates the
approval. Its table plus no-update/no-delete triggers are owned by
`core/sync_framework/raw_session_identity_reconciliation.py`; no other schema
or migration expansion is authorized by this budget.

These are code and isolated-behavior contracts only. They do not prove that the
live v2 cursor was migrated, local Agent history was read/rebuilt, the daemon
SLA was observed, or COG-045 was closed in production.

Each scheduled cycle combines a recent-session tail accelerator with a
persisted, source-wide reconciliation pass. The effective
`sync.raw_sync_sessions_per_source` and `sync.raw_sync_turns_per_session`
limits bound one cycle's latency only. They cannot exclude an old session, a
long-session suffix, or another discovered canonical session from eventual
reconciliation. Session aliases are canonicalized before discovery de-duplication,
existing-turn lookup, Raw write, cursor update, sync-log accounting, and
complete-session handoff.

The cursor advances only through the contiguous prefix whose turns have a
non-empty canonical Raw revision receipt. A transient Raw failure leaves the
first uncommitted turn eligible for replay; no partial batch can be handed off
as a complete session. Source turn counts are intentionally unknown until the
durable roster-bound denominator ledger has observed every currently discovered
canonical session. `scripts/audit_agent_source_coverage.py --strict --json`
uses coverage-report v2 together with the disposition-bound cursor schema v4 and
rejects a pending or stale denominator instead of treating a bounded scan as
global completion.

The CLI follows the same contract: `--max`, `--since`, source filters, config
batch limits, and dry-run scopes may report `partial`, `failed`, or `dry_run`,
but never persist global `done` or enqueue a partial-session handoff. Enabling
the live scheduled owner or scanning native histories remains an explicit
authorization boundary; hermetic cursor proof is not evidence that a user's
real histories have been read.

After each scan the daemon writes a bounded, `0600`
`agent_source_coverage.json` sidecar and publishes its safe projection in
`daemon_heartbeat.json`. Each source exposes only owner/SLA, discovery and
capture timestamps, bounded cursor metadata, counts, gap code, and error type;
it never exposes a native path, turn text, or token. The heartbeat restores the
sidecar before the first post-restart scan so a restart cannot hide the last
known source state.

Use the independent live-state verifier after a daemon has completed its
scheduled scan:

```bash
python3 scripts/audit_agent_source_coverage.py --strict --json
```

It reads the tracked manifest, effective configuration, and heartbeat directly
without instantiating parsers or reading transcript bodies. It independently
opens the cursor ledger and canonical Raw database read-only, then reconciles
the snapshot-bound generation, expected turns, immutable Raw receipts, and
Raw headers. A passing result requires all 12 active sources to have an enabled
owner, a fresh coverage record, a manifest-bound native snapshot, and
observed/captured native turns. The report preserves each source's `host_agent`
or `ingestion_only` role: the four ingestion-only sources must close
Native-to-Raw coverage but are never counted in the eight-host full-power
denominator. A binary installation, a one-off backfill, a heartbeat count, or
a static manifest pass is not substitute evidence.

Every native Raw receipt is bound to the current `support_manifest_hash`,
role, parser identity, capability-contract hash, native-to-Raw, ACL, and
retention contract. A forged source identity, manifest identity, parser-bound
metadata, or invalid observed fidelity is rejected before Raw insertion. If a
legitimate native observation exposes visible-text loss, ACL lineage loss, or
another contract degradation, the Raw store retains the visible bytes with
`support_raw_contract_state=nonconforming` and typed
`support_raw_contract_errors`; it is classified `partial` and cannot be
mistaken for a conforming Native-to-Raw or certification input. Generic
non-native capture continues to use its own source owner and cannot claim a
native-source marker. If a later degraded native observation has lower content
quality than an existing complete revision, Mnemos appends an immutable
`raw_native_contract_observations` record instead of discarding it; the logical
current event remains non-certifying until a later conforming native observation
restores its effective status. An observed fidelity mismatch, including
`unknown`, is nonconforming and therefore never becomes `complete` by default.
That effective state also lowers the logical Raw lifecycle confidence and
survival score immediately, so retention-driven projection cannot prioritize a
newly degraded event using its old complete score.

Backfill emits a `NativeSourceSnapshot` containing that hash plus observed
roots, parser, cursor, and native denominator. The daemon and backfill reject
an undeclared source before discovery, parsing, or Raw upsert. A snapshot is
evidence only; it cannot define a new source or capability. A changed manifest
invalidates older runtime-probe receipts and snapshots until they are observed
again. Existing registry instances are also rechecked against the exact
manifest parser class before reuse, so a declared name cannot retain a
substituted parser after a contract change.

Use the independent verifier before release:

```bash
python3 scripts/audit_agent_source_support_manifest.py --strict --json
```

It parses the manifest and parser modules directly, checks both directions,
and rejects residual handwritten protocol/registry/evidence/diagnostic lists.
This static check deliberately reports runtime snapshot and receipt counters as
`null` when no observation was supplied; it never turns source-text presence
into a runtime-zero claim. Its `certifying=false` and `release_eligible=false`
are intentional: a source-manifest audit verifies the contract shape, not an
actual host runtime capability.

After an explicitly authorized native observation, preserve the machine JSON
report and validate it independently:

```bash
python3 scripts/backfill_raw_event_store.py --dry-run --json > "$RUNTIME_EVIDENCE_PATH"
python3 scripts/audit_agent_source_support_manifest.py \
  --runtime-evidence "$RUNTIME_EVIDENCE_PATH" --require-runtime-evidence --strict --json
```

The first command reads native history and opens a Raw store, so it is not a
substitute for user authorization or an isolated test setup. It emits a
`structural_source_observation` report with a canonical schema, producer,
`support_manifest_hash`, and `report_hash`. The hash is only a corruption
checksum, not a signature or producer attestation; the report may not contain
runtime receipts or any `certifying` / `release_eligible` / full-power claim.
The verifier rejects non-empty `unmanifested_sources`, daemon errors, and
backfill failures, but keeps `runtime_full_power_ok=null` and
`certifying=false` even when the snapshot shape is valid.

Host full-power has two independently checked receipts. `agent kit --json`
shows six non-interchangeable layers for every host: `installed`,
`path_detected`, `discovery_covered`, `content_parsed`, `raw_committed`, and
`runtime_verified`. Installation, a discovered directory, or a structural
snapshot can only satisfy their own layer; none can be promoted to another.

`AgentRuntimeReceiptStore` is the authoritative append-only owner for the
authorized host runtime receipt. A structural source observation, a manifest
pass, or an adapter's own status report cannot substitute for that receipt.

First, an authorized host calls the fixed synthetic-safe `health_check` then
`agent_runtime_probe`. The canonical v3 runtime receipt stores a
`runtime_canary_hash`, not the sample text, and returns its server-generated
receipt ID to the host. Prior v1/v2 payloads or a receipt without the canary
hash fail closed; read-time evaluation recomputes the fixed canary and rejects
a different but well-formed SHA-256 value. Second, after the daemon has finished a frozen native
denominator generation, run the content-free receipt writer:

```bash
python3 scripts/attest_agent_source_capture.py --agent <agent> --apply --json
```

The verifier never reopens native transcript files and never trusts a source
report. It independently compares the daemon coverage sidecar,
`agent_sync_cursors.db`, and canonical Raw revision headers, then reads only
the exact Raw revisions bound to that frozen generation. Their structured
tool evidence must contain both the exact `agent_runtime_probe` call and the
server result carrying the same receipt ID and `runtime_canary_hash` in one
session, joined by the same native tool-call ID. JSON-encoded structured fields
are decoded under fixed per-field, total-byte, depth, and node bounds. Merely
copying those values into visible text is rejected.

The durable receipt writer reruns this read-only verifier from coverage,
cursor, and Raw inputs before every write. It has no API that persists
caller-shaped evidence booleans or hashes.

The durable source-capture receipt binds the current health-check hash,
support-manifest hash, native-source-snapshot hash, exact Raw revision-set
hash, `runtime_receipt_id_hash`,
`runtime_canary_raw_revision_ids_hash`, completeness counters, and timestamp.
A changed manifest, new runtime receipt, stale probe, incomplete denominator,
Raw-count mismatch, missing structured canary, missing receipt, or revoked
authorization fails closed. Only both current receipts plus the independent
canonical Raw canary proof can make an installed host `full_power`; a
structural report, its checksum, visible-text self-signing, or externally
supplied JSON can never satisfy the 8/8 denominator.
