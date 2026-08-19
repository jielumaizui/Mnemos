# Daemon Instance Identity Design

Status: implemented and validated (`ebec1229`)

## Problem

The old PID file stored only an integer. On Darwin, `kill(pid, 0)` proved that a PID existed but not that it was the Mnemos daemon that created the file. Heartbeat freshness likewise proved only that a file was recently written; it did not bind the writer to a process, build, config, database, or complete service set. This allowed PID reuse, stale code, and empty-service heartbeats to be mistaken for a healthy current daemon.

## Contract

`mnemos.daemon_instance.v2` is the single identity shared by PID file, heartbeat, status, stop, and strict health. It binds:

- `instance_id`, PID, OS process start token, boot session, executable and command hash;
- Python executable, recorded commit and runtime-code content fingerprint;
- config-file byte hash, canonical effective-config fingerprint, database directory identity, exact service manifest and its hash.

PID liveness is not authorization. `pid_exists()` may prevent unsafe stale-record cleanup, but only `verify_instance_record()` can authorize status or a signal. `signal_verified_instance()` repeats verification immediately before each SIGTERM/SIGKILL. A reused PID, mismatched process, or temporarily unverifiable live PID receives no follow-up signal.

## Platform facts

- Darwin: `proc_pidinfo` microsecond start time, `proc_pidpath`, `kern.bootsessionuuid`; `kern.boottime` is migration fallback with a two-second observation tolerance.
- Linux: `/proc/<pid>/stat` starttime, boot UUID, `/proc/<pid>/exe` and cmdline.
- Windows: CIM creation time, executable path and command line; no POSIX `os.kill(pid, 0)` probe.

## Lifecycle

New code can write only a complete JSON identity; integer PID writing was removed. The old integer reader has one bounded purpose: an installed legacy PID can be converted to an ephemeral identity only when current OS facts prove a Mnemos daemon. The converted identity is not persisted. The migration functions have daemon ownership, a 2026-08-10 expiry, and are removed after no supported installation reports an integer PID for 30 days.

The daemon writes a `0600` PID record under the process lock. Startup loads strict configuration, registers services, writes a `0600` `mnemos.daemon_heartbeat.v3` snapshot carrying the same identity, and only then signals startup success to the parent. Health rejects old schema, PID/heartbeat mismatch, incomplete or non-exact service sets, live process mismatch, and current code/config/database drift. The v2 identity keeps the file-byte `config_hash` and adds `config_fingerprint`, derived from `mnemos.config_registry.v1`, the persisted document, environment overrides, and the active performance tier. This detects effective drift even when the config file bytes are unchanged.

Commit drift alone is informational: if runtime-code content is identical, health exposes `commit_match=false` and `build_compatible=true`. This prevents a docs-only commit from requiring restart while still detecting dirty or old runtime code.

## Failure behavior

- PID absent: stale record may be removed through compare-before-delete cleanup.
- PID reused or process facts differ: non-zero result, zero signal.
- PID exists but process facts are temporarily unavailable after SIGTERM: preserve record, return non-zero, do not escalate to SIGKILL.
- Heartbeat is stale, old-schema, empty-service, mismatched, or context-drifted: strict heartbeat degraded with a machine-readable identity reason.
- Same OS account fully compromised: outside this local single-user trust root; no string allowlist claims to solve it.

## Verification

- Targeted daemon identity/health/acceptance tests: 98 passed.
- Broader daemon/health set: 158 passed.
- Integration/acceptance/system: 176 passed.
- Quick regression: 5460 passed plus 15 subtests.
- Local gates: flake8, mypy zero budget, compileall, maintainability, security/privacy, vulture, CI ratchet, dependency graph and No-Zombie policy passed; Desktop facts is revalidated after documentation synchronization.
- Real Darwin migration originally proved the v1 control path. ROOT-20260710-018 added a bounded OS-verified v1-to-v2 control migration so an installed v1 daemon can be stopped safely once; all newly written PID and heartbeat records must be v2/v3 and include the effective config fingerprint.
