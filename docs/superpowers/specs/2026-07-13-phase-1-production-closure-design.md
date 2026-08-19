# Phase 1 production closure design

**Status:** Approved for execution by the user on 2026-07-13.

## Objective

Close Phase 1 of the cognitive remediation audit at production-effect level. The
existing implementation and hermetic evidence for COG-045, COG-001, COG-002,
COG-004 through COG-009, and COG-026 remain valid only as
`IMPLEMENTED_PENDING_LIVE_EFFECT` until the real local sources, canonical Raw
store, Raw projection, runtime receipts, and backup disposition have been
verified.

The target end state is not a synthetic green result. It is a reproducible,
content-private evidence chain showing that all eight host Agents satisfy the
manifest-owned Native-to-Raw denominator, that their runtime full-power evidence
is independent of source self-reporting, that the published Raw projection is
lossless, and that historical projection backups are within the approved disk
budget or have an explicit recovery disposition.

## Authorized scope

The user authorized this run to:

- enumerate and read local histories for the eight host Agents and the related
  Raw, projection, and backup metadata needed for the audit;
- update the production `raw_sync` setting, stop and restart the daemon under a
  controlled identity check, and restore the service after the verification;
- write synthetic-safe canaries, canonical Raw revisions, projection output,
  runtime/source-capture receipts, and acceptance evidence;
- inventory historical projection backups and delete only backups that the
  recovery audit classifies as eligible and that are necessary to bring the
  total under the configured budget.

This authorization does not permit exporting history content, copying content
into reports, weakening a gate, treating a fixture as production evidence, or
deleting any non-projection user note or unrecoverable backup.

## Evidence and privacy contract

All reports and ledger updates record stable identifiers, counts, byte totals,
hashes, timestamps, paths only where the existing contract permits them, and
typed dispositions. Native turn bodies, secrets, prompt text, and other
history content remain local to the authorized processing paths and are never
written into the audit report, Desktop map, or acceptance ledger.

Every live assertion has a separate verifier:

- source denominator and Native-to-Raw coverage are checked by the independent
  source auditor;
- Agent full-power requires an authorized runtime receipt plus a matching,
  independently calculated source-to-Raw capture receipt;
- Raw projection fidelity is checked by reverse parsing publisher-owned Markdown
  and comparing field byte hashes through a read-only Raw database connection;
- backup eligibility comes from the metadata/recovery auditor, not from filename
  heuristics or a disk-usage estimate alone.

## Execution sequence

1. **Safety snapshot.** Record the current commit, configuration hash, daemon
   identity and state, database/vault/backup metadata, and configured budget.
   Validate that the active source-support manifest is the sole denominator
   owner. Do not delete or migrate anything in this step.
2. **Controlled daemon transition.** Stop only the verified production daemon,
   retain the pre-change configuration and state evidence, enable `raw_sync`,
   start the daemon on the current commit, and verify its identity before any
   source scan. If start-up or identity verification fails, restore the prior
   configuration and service state before investigating.
3. **Live capture and reconciliation.** Use synthetic-safe per-host canaries
   and the complete manifest-owned native source roster. Run the durable
   cursor-backed Native-to-Raw reconciliation through two polling periods and
   one daemon restart. A failed Raw write, incomplete denominator, gap, or
   non-contiguous cursor leaves the relevant item pending; it never advances a
   downstream completion state.
4. **Independent acceptance.** Run the source coverage audit, source-capture
   attestation, authorized runtime probes, and `mnemos agent kit --json`.
   Require eight full-power hosts, matching manifest/native snapshot/revision-set
   hashes, and zero gap counters before recording production receipts.
5. **Raw projection and backup closure.** Publish the current Raw projection,
   run the lossless reverse fidelity audit, then inventory historical projection
   backups with metadata and recovery classification. Delete only eligible
   legacy projection backups selected by the audited disposition; preserve a
   verified recovery set and never move unrelated user files. Re-audit the
   resulting total against the disk budget.
6. **Release-quality verification.** With the daemon under the controlled state
   required by the hermetic runner, run strict Quick and the applicable local
   gates. Require a clean formal-state result, no outside writes, and no
   omitted/forged acceptance evidence.
7. **Closure and restoration.** Keep the intended `raw_sync` service owner
   enabled after successful validation, update the in-place Phase 1 ledger,
   Desktop audit, and system map with real receipts and outcome boundaries,
   commit code/document changes, and perform a final challenger pass. Any
   failed condition remains explicitly open with its evidence; it is not
   converted to a passing status.

## Rollback and stop conditions

Before every mutating transition, preserve enough configuration and service
identity evidence to restore the immediately preceding safe state. Immediately
restore and stop the operation if the daemon identity is ambiguous, a source
scan accesses an unsupported source, the Raw database/schema integrity check
fails, a receipt hash conflicts, the projection fidelity audit reports a loss,
or the backup auditor cannot prove recovery eligibility.

An incomplete source denominator, a failed runtime probe, or a strict gate
failure is a repair signal rather than an excuse to disable the check. The run
may continue with a narrower diagnosis only after recording the failed evidence;
Phase 1 remains open until all stated conditions pass.

## Definition of done

Phase 1 is closed only when all of the following are true at the current
commit:

- each Phase 1 COG has production evidence rather than
  `IMPLEMENTED_PENDING_LIVE_EFFECT`;
- all eight host Agents are independently verified `full_power` with complete
  Native-to-Raw coverage and matching receipts;
- the lossless Raw projection and its reverse auditor report zero missing,
  duplicate, truncation, and field-hash mismatches;
- the projection backup total is within budget with each retained/deleted item
  carrying an audited recovery disposition;
- strict Quick and required gates are clean, hermetic, and tied to this commit;
- the repository ledger, Desktop audit, and Desktop system map describe the
  real result without plaintext or unsupported claims.
