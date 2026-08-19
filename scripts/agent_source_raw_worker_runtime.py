"""Isolated challenger and Raw-generation worker execution."""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import psutil

from core.ops.durable_io import read_native_bytes
from scripts.agent_source_raw_recovery_contract import (
    AgentSourceRawReconciliationError,
    raw_generation_worker_isolation_contract,
)

_CHALLENGER_REPORT_SCHEMA = "mnemos.agent_source_native_raw_challenger.v3"
_RAW_GENERATION_REPORT_SCHEMA = "mnemos.agent_source_raw_generation_worker.v1"
_RAW_GENERATION_FAILURE_SCHEMA = "mnemos.raw_generation_worker_failure.v3"


@dataclass(frozen=True)
class ChallengerWorkerDependencies:
    create_recovery_worker_root: Callable[[str], tuple[Path, int]]
    create_private_challenger_raw_snapshot: Callable[..., Any]
    create_private_target: Callable[[Path], None]
    remove_worker_root: Callable[[Path], None]
    close_inherited_descriptors: Callable[[], int]
    establish_parent_death_guard: Callable[..., tuple[int, int]]
    install_filesystem_sandbox: Callable[..., str]
    install_read_only_guard: Callable[..., None]
    isolated_parse_spool: Callable[[Path], AbstractContextManager[None]]
    audit_native_to_raw: Callable[..., Mapping[str, Any]]
    read_guard_violations: Callable[[Path], set[str]]
    complete_parent_death_guard: Callable[[int, int], None]
    kill_worker_process_group: Callable[[int], None]
    max_report_bytes: int
    max_rss_bytes: int
    max_seconds: int


@dataclass(frozen=True)
class RawGenerationWorkerDependencies:
    assert_parent_handles_closed: Callable[..., None]
    create_recovery_worker_root: Callable[[str], tuple[Path, int]]
    create_private_target: Callable[[Path], None]
    remove_worker_root: Callable[[Path], None]
    close_inherited_descriptors: Callable[[], int]
    establish_parent_death_guard: Callable[..., tuple[int, int]]
    install_filesystem_sandbox: Callable[..., str]
    sqlite_sidecars: Callable[[Path], tuple[Path, Path, Path]]
    install_write_guard: Callable[..., None]
    fsync_regular_file: Callable[[Path], None]
    fsync_directory: Callable[[Path], None]
    raw_event_store_factory: Callable[..., Any]
    raw_only_engine_factory: Callable[..., Any]
    cursor_store_factory: Callable[[Path], Any]
    load_source_coverage_state: Callable[[Path], Mapping[str, Any]]
    isolated_parse_spool: Callable[[Path], AbstractContextManager[None]]
    run_raw_service: Callable[..., Mapping[str, Any]]
    static_source_registry_factory: Callable[[Iterable[Any]], Any]
    safe_sync_error_evidence: Callable[..., list[dict[str, Any]]]
    read_guard_violations: Callable[[Path], set[str]]
    safe_cycle_report: Callable[..., dict[str, Any]]
    complete_parent_death_guard: Callable[[int, int], None]
    kill_worker_process_group: Callable[[int], None]
    set_active_generation: Callable[[int], None]
    max_report_bytes: int
    max_rss_bytes: int
    max_seconds: int


def audit_native_to_raw_isolated(
    sources: Iterable[Any],
    *,
    raw_db_path: Path,
    manifest: Any | None = None,
    require_all_host_sources: bool = True,
    source_scope: str = "host",
    dependencies: ChallengerWorkerDependencies,
) -> dict[str, Any]:
    """Run one content-free challenger pass in a disposable bounded worker."""
    if not hasattr(os, "fork"):
        raise AgentSourceRawReconciliationError("native_challenger_worker_unavailable")
    source_list = list(sources)
    expected_source_names = {str(getattr(source, "name", "") or "") for source in source_list}
    if "" in expected_source_names or len(expected_source_names) != len(source_list):
        raise AgentSourceRawReconciliationError("native_challenger_source_roster_invalid")
    try:
        worker_root, _stale_worker_roots_cleaned = dependencies.create_recovery_worker_root("challenger")
        writable_root = worker_root / "writable"
        writable_root.mkdir(mode=0o700)
        output_path = writable_root / "report.json"
        dependencies.create_private_target(output_path)
        violation_marker = writable_root / "write-violations.log"
        dependencies.create_private_target(violation_marker)
    except (OSError, AgentSourceRawReconciliationError):
        raise AgentSourceRawReconciliationError("native_challenger_worker_unavailable") from None
    try:
        parent_watch_read, parent_watch_write = os.pipe()
        pid = os.fork()
    except OSError:
        for descriptor in (
            locals().get("parent_watch_read"),
            locals().get("parent_watch_write"),
        ):
            if isinstance(descriptor, int):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        dependencies.remove_worker_root(worker_root)
        raise AgentSourceRawReconciliationError("native_challenger_worker_unavailable") from None
    if pid == 0:  # pragma: no branch - isolated challenger worker
        try:
            os.close(parent_watch_write)
            dependencies.close_inherited_descriptors()
            guardian_pid, worker_life_write = dependencies.establish_parent_death_guard(
                parent_watch_read,
                worker_root,
            )
            raw_snapshot_path = dependencies.create_private_challenger_raw_snapshot(
                raw_db_path,
                worker_root,
            )
            filesystem_sandbox = dependencies.install_filesystem_sandbox(
                (writable_root,),
                allowed_write_paths=(Path(os.devnull),),
            )
            child_guard_hashes: set[str] = set()
            snapshot_read_roots = tuple(
                Path(root).expanduser().resolve(strict=False)
                for source in source_list
                for root in (
                    source.snapshot_read_roots()
                    if callable(getattr(source, "snapshot_read_roots", None))
                    else ()
                )
            )
            dependencies.install_read_only_guard(
                allowed_write_roots=(writable_root,),
                allowed_read_paths=(raw_snapshot_path,),
                allowed_read_roots=snapshot_read_roots,
                blocked_name_hashes=child_guard_hashes,
                violation_marker=violation_marker,
            )
            with dependencies.isolated_parse_spool(writable_root):
                report = dependencies.audit_native_to_raw(
                    source_list,
                    raw_db_path=raw_snapshot_path,
                    manifest=manifest,
                    require_all_host_sources=require_all_host_sources,
                    source_scope=source_scope,
                )
            child_guard_hashes.update(dependencies.read_guard_violations(violation_marker))
            mutable_report = dict(report)
            mutable_report["worker_guard"] = {
                "blocked_process_mutation_count": len(child_guard_hashes),
                "blocked_process_mutation_name_hashes": sorted(child_guard_hashes),
                "inherited_regular_file_descriptors_closed": True,
                "filesystem_sandbox": filesystem_sandbox,
            }
            encoded = json.dumps(
                mutable_report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > dependencies.max_report_bytes:
                os._exit(92)
            with output_path.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            dependencies.complete_parent_death_guard(
                guardian_pid,
                worker_life_write,
            )
            os._exit(0)
        except BaseException:  # child must not unwind inherited transaction state
            os._exit(90)
    os.close(parent_watch_read)
    killed_for_budget = False
    killed_for_timeout = False
    started = time.monotonic()
    try:
        process = psutil.Process(pid)
        baseline_rss = process.memory_info().rss
        while True:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                break
            try:
                if process.memory_info().rss - baseline_rss > dependencies.max_rss_bytes:
                    killed_for_budget = True
                    dependencies.kill_worker_process_group(pid)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            if time.monotonic() - started > dependencies.max_seconds:
                killed_for_timeout = True
                dependencies.kill_worker_process_group(pid)
            time.sleep(0.01)
        if killed_for_budget:
            raise AgentSourceRawReconciliationError("native_challenger_worker_budget_exceeded")
        if killed_for_timeout:
            raise AgentSourceRawReconciliationError("native_challenger_worker_timeout")
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code == 92:
            raise AgentSourceRawReconciliationError("native_challenger_report_budget_exceeded")
        if exit_code != 0:
            raise AgentSourceRawReconciliationError("native_challenger_worker_failed")
        try:
            encoded = read_native_bytes(output_path)
            if not encoded or len(encoded) > dependencies.max_report_bytes:
                raise ValueError("invalid challenger report size")
            report = json.loads(encoded)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise AgentSourceRawReconciliationError("native_challenger_report_invalid") from None
        if (
            not isinstance(report, dict)
            or report.get("schema_version") != _CHALLENGER_REPORT_SCHEMA
            or not isinstance(report.get("sources"), dict)
            or not isinstance(report.get("blocking_sources"), list)
            or not isinstance(report.get("ok"), bool)
            or not isinstance(report.get("worker_guard"), dict)
        ):
            raise AgentSourceRawReconciliationError("native_challenger_report_invalid")
        reports = report["sources"]
        blocking_sources = sorted(
            name
            for name, evidence in reports.items()
            if not isinstance(evidence, dict) or evidence.get("status") != "ok"
        )
        if (
            set(reports) != expected_source_names
            or report.get("source_scope") != source_scope
            or report["blocking_sources"] != blocking_sources
            or report["ok"] != (not blocking_sources)
        ):
            raise AgentSourceRawReconciliationError("native_challenger_report_invalid")
        worker_guard = report["worker_guard"]
        blocked_hashes = worker_guard.get("blocked_process_mutation_name_hashes")
        blocked_count = worker_guard.get("blocked_process_mutation_count")
        if (
            not isinstance(blocked_count, int)
            or isinstance(blocked_count, bool)
            or blocked_count < 0
            or not isinstance(blocked_hashes, list)
            or len(blocked_hashes) != blocked_count
            or len(set(blocked_hashes)) != blocked_count
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value)
                for value in blocked_hashes
            )
        ):
            raise AgentSourceRawReconciliationError("native_challenger_report_invalid")
        if (
            worker_guard.get("filesystem_sandbox")
            != "darwin_kernel_deny_writes_outside_allowed_roots_v1"
            or worker_guard.get("inherited_regular_file_descriptors_closed") is not True
        ):
            raise AgentSourceRawReconciliationError("native_challenger_report_invalid")
        if blocked_count:
            raise AgentSourceRawReconciliationError("native_challenger_write_scope_violation")
        report["worker_isolation"] = {
            "schema_version": "mnemos.native_challenger_worker_isolation.v1",
            "formal_write_guard": ("python_audit_global_read_only_except_private_worker_root"),
            "allowed_ephemeral_root_count": 1,
            "filesystem_sandbox": ("darwin_kernel_deny_writes_outside_allowed_roots_v1"),
            "inherited_regular_file_descriptor_policy": (
                "close_all_preexisting_regular_file_descriptors_v1"
            ),
            "crash_cleanup": ("guardian_cleanup_plus_owner_registry_next_run_reap_v2"),
            "parent_death_guard": ("pipe_eof_split_process_group_kill_cleanup_v2"),
            "max_rss_bytes": dependencies.max_rss_bytes,
            "max_report_bytes": dependencies.max_report_bytes,
            "max_seconds": dependencies.max_seconds,
        }
        return report
    except AgentSourceRawReconciliationError:
        raise
    except (OSError, ChildProcessError, psutil.Error):
        raise AgentSourceRawReconciliationError("native_challenger_worker_failed") from None
    finally:
        try:
            waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == 0:
                dependencies.kill_worker_process_group(pid)
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass
        try:
            os.close(parent_watch_write)
        except OSError:
            pass
        dependencies.remove_worker_root(worker_root)


def validate_raw_generation_report(
    report: Any,
    *,
    source_names: Iterable[str],
    generation_number: int,
) -> dict[str, Any]:
    expected_sources = set(source_names)
    if (
        not isinstance(report, dict)
        or report.get("schema_version") != _RAW_GENERATION_REPORT_SCHEMA
        or not isinstance(report.get("cycle"), dict)
        or not isinstance(report.get("error_evidence"), list)
        or not isinstance(report.get("process_write_scope"), dict)
        or not isinstance(report.get("worker_guard"), dict)
        or report.get("generation") != generation_number
    ):
        raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
    cycle = report["cycle"]
    sources = cycle.get("sources")
    reported_error_count = report.get("reported_error_count")
    if (
        set(cycle) != {"errors", "sources"}
        or not isinstance(cycle.get("errors"), int)
        or isinstance(cycle.get("errors"), bool)
        or int(cycle["errors"]) < 0
        or not isinstance(sources, dict)
        or set(sources) != expected_sources
        or not isinstance(reported_error_count, int)
        or isinstance(reported_error_count, bool)
        or reported_error_count != cycle["errors"]
    ):
        raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
    expected_source_fields = {
        "status",
        "gap",
        "native_sessions",
        "native_turns",
        "denominator_complete",
        "denominator_turns",
        "snapshot_denominator_turns",
    }
    for evidence in sources.values():
        if (
            not isinstance(evidence, dict)
            or set(evidence) != expected_source_fields
            or not isinstance(evidence["status"], str)
            or not isinstance(evidence["gap"], str)
            or not isinstance(evidence["denominator_complete"], bool)
        ):
            raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
        for field in (
            "native_sessions",
            "native_turns",
            "denominator_turns",
            "snapshot_denominator_turns",
        ):
            value = evidence[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
    typed_error_count = 0
    for evidence in report["error_evidence"]:
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"source", "error_type", "error_code", "message_hash", "count"}
            or not isinstance(evidence["source"], str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", evidence["source"])
            or not isinstance(evidence["error_type"], str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", evidence["error_type"])
            or not isinstance(evidence["error_code"], str)
            or (
                evidence["error_code"]
                and not re.fullmatch(r"[a-z][a-z0-9_]{2,127}", evidence["error_code"])
            )
            or not isinstance(evidence["message_hash"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["message_hash"])
            or not isinstance(evidence["count"], int)
            or isinstance(evidence["count"], bool)
            or evidence["count"] < 1
        ):
            raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
        typed_error_count += int(evidence["count"])
    if report.get("typed_error_count") != typed_error_count:
        raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
    scope = report["process_write_scope"]
    guard = report["worker_guard"]
    for evidence in (scope, guard):
        hashes = evidence.get("blocked_process_mutation_name_hashes")
        count = evidence.get("blocked_process_mutation_count")
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(hashes, list)
            or len(hashes) != count
            or len(set(hashes)) != count
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value)
                for value in hashes
            )
        ):
            raise AgentSourceRawReconciliationError("raw_generation_worker_report_invalid")
    if (
        scope.get("process_write_scope_verified") is not True
        or scope.get("process_write_guard") != "python-audit-exact-database-path-v1"
        or guard.get("filesystem_sandbox") != "darwin_kernel_deny_writes_outside_allowed_roots_v1"
        or guard.get("inherited_regular_file_descriptors_closed") is not True
    ):
        raise AgentSourceRawReconciliationError("raw_generation_worker_write_scope_unverified")
    return report


def run_raw_generation_isolated(
    *,
    config: Any,
    raw_db_path: Path,
    cursor_path: Path,
    coverage_path: Path,
    sources: Iterable[Any],
    source_names: Iterable[str],
    limits: Mapping[str, int],
    process_write_scope: Any,
    generation_number: int,
    dependencies: RawGenerationWorkerDependencies,
) -> dict[str, Any]:
    """Run one state-mutating Raw generation in a disposable bounded worker."""
    if not hasattr(os, "fork"):
        raise AgentSourceRawReconciliationError("raw_generation_worker_unavailable")
    source_list = list(sources)
    source_name_list = list(source_names)
    if len(source_name_list) != len(set(source_name_list)) or {
        str(getattr(source, "name", "") or "") for source in source_list
    } != set(source_name_list):
        raise AgentSourceRawReconciliationError("raw_generation_source_roster_invalid")
    if (
        not isinstance(generation_number, int)
        or isinstance(generation_number, bool)
        or generation_number < 1
    ):
        raise AgentSourceRawReconciliationError("raw_generation_number_invalid")
    dependencies.assert_parent_handles_closed(
        raw_db_path=raw_db_path,
        cursor_path=cursor_path,
        coverage_path=coverage_path,
    )
    try:
        worker_root, _stale_worker_roots_cleaned = dependencies.create_recovery_worker_root("raw-generation")
        output_path = worker_root / "report.json"
        dependencies.create_private_target(output_path)
        violation_marker = worker_root / "write-violations.log"
        dependencies.create_private_target(violation_marker)
    except (OSError, AgentSourceRawReconciliationError):
        raise AgentSourceRawReconciliationError("raw_generation_worker_unavailable") from None
    try:
        parent_watch_read, parent_watch_write = os.pipe()
        pid = os.fork()
    except OSError:
        for descriptor in (
            locals().get("parent_watch_read"),
            locals().get("parent_watch_write"),
        ):
            if isinstance(descriptor, int):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        dependencies.remove_worker_root(worker_root)
        raise AgentSourceRawReconciliationError("raw_generation_worker_unavailable") from None
    if pid == 0:  # pragma: no branch - isolated state-mutating generation
        worker_phase = "initialize"
        try:
            dependencies.set_active_generation(generation_number)
            os.close(parent_watch_write)
            worker_phase = "close_inherited_descriptors"
            dependencies.close_inherited_descriptors()
            worker_phase = "establish_parent_death_guard"
            guardian_pid, worker_life_write = dependencies.establish_parent_death_guard(
                parent_watch_read,
                worker_root,
            )
            child_guard_hashes: set[str] = set()
            database_dir = Path(config.database_dir).expanduser().resolve(strict=False)
            coverage_temporary = coverage_path.with_name(
                f".{coverage_path.name}.raw-recovery.tmp"
            )
            worker_phase = "install_filesystem_sandbox"
            filesystem_sandbox = dependencies.install_filesystem_sandbox(
                (worker_root,),
                allowed_write_paths=(
                    database_dir,
                    raw_db_path,
                    *dependencies.sqlite_sidecars(raw_db_path),
                    cursor_path,
                    *dependencies.sqlite_sidecars(cursor_path),
                    coverage_path,
                    coverage_temporary,
                    Path(os.devnull),
                ),
            )
            worker_phase = "install_write_guard"
            dependencies.install_write_guard(
                database_dir=database_dir,
                allowed_names={
                    raw_db_path.name,
                    cursor_path.name,
                    coverage_path.name,
                },
                allowed_write_roots=(worker_root,),
                allowed_read_roots=tuple(
                    Path(root).expanduser().resolve(strict=False)
                    for source in source_list
                    for root in (
                        source.snapshot_read_roots()
                        if callable(getattr(source, "snapshot_read_roots", None))
                        else ()
                    )
                ),
                blocked_name_hashes=child_guard_hashes,
                violation_marker=violation_marker,
            )
            cycle_errors: list[tuple[str, BaseException]] = []

            def record_cycle_error(service: str, error: Exception) -> None:
                cycle_errors.append((str(service), error))

            def current_limits() -> dict[str, int]:
                return {key: int(value) for key, value in limits.items()}

            def persist_coverage(coverage: Mapping[str, Any]) -> None:
                coverage_temporary_created = False
                try:
                    try:
                        dependencies.create_private_target(coverage_temporary)
                    except FileExistsError:
                        raise
                    except BaseException:
                        coverage_temporary_created = True
                        raise
                    coverage_temporary_created = True
                    coverage_temporary.write_text(
                        json.dumps(
                            coverage,
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    os.chmod(coverage_temporary, 0o600)
                    dependencies.fsync_regular_file(coverage_temporary)
                    os.replace(coverage_temporary, coverage_path)
                    dependencies.fsync_directory(database_dir)
                finally:
                    if coverage_temporary_created:
                        coverage_temporary.unlink(missing_ok=True)

            def create_raw_engine() -> Any:
                nonlocal worker_phase
                worker_phase = "initialize_raw_event_store"
                raw_store = dependencies.raw_event_store_factory(db_path=raw_db_path, config=config)
                worker_phase = "initialize_raw_only_engine"
                engine = dependencies.raw_only_engine_factory(raw_store=raw_store)
                worker_phase = "run_raw_service"
                return engine

            worker_phase = "initialize_cursor_store"
            cursor_store = dependencies.cursor_store_factory(database_dir)
            worker_phase = "load_previous_coverage"
            previous_coverage = dependencies.load_source_coverage_state(
                coverage_path
            )
            worker_phase = "run_raw_service"
            with dependencies.isolated_parse_spool(worker_root):
                cycle_result = dependencies.run_raw_service(
                    record_cycle_error,
                    continuous_sync_limits_func=current_limits,
                    cursor_store=cursor_store,
                    previous_source_coverage=previous_coverage,
                    coverage_state_sink=persist_coverage,
                    engine_factory=create_raw_engine,
                    source_registry=dependencies.static_source_registry_factory(source_list),
                )
            worker_phase = "build_worker_report"
            error_evidence = dependencies.safe_sync_error_evidence(cycle_errors)
            child_guard_hashes.update(dependencies.read_guard_violations(violation_marker))
            reported_error_count = int(cycle_result.get("errors") or 0)
            report = {
                "schema_version": _RAW_GENERATION_REPORT_SCHEMA,
                "generation": generation_number,
                "cycle": dependencies.safe_cycle_report(cycle_result, source_name_list),
                "error_evidence": error_evidence,
                "reported_error_count": reported_error_count,
                "typed_error_count": sum(
                    int(item.get("count") or 0)
                    for item in error_evidence
                ),
                "process_write_scope": process_write_scope.evidence(),
                "worker_guard": {
                    "blocked_process_mutation_count": len(child_guard_hashes),
                    "blocked_process_mutation_name_hashes": sorted(
                        child_guard_hashes
                    ),
                    "inherited_regular_file_descriptors_closed": True,
                    "filesystem_sandbox": filesystem_sandbox,
                },
            }
            encoded = json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if len(encoded) > dependencies.max_report_bytes:
                os._exit(92)
            worker_phase = "write_worker_report"
            with output_path.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            worker_phase = "complete_parent_death_guard"
            dependencies.complete_parent_death_guard(
                guardian_pid,
                worker_life_write,
            )
            os._exit(0)
        except BaseException as exc:  # child must not unwind inherited transaction state
            failure_type = exc.__class__.__name__
            reason_code = str(getattr(exc, "code", "") or "")
            failure_details = getattr(exc, "details", {})
            failure_phase = (
                str(failure_details.get("phase") or "")
                if isinstance(failure_details, Mapping)
                else ""
            )
            if not failure_phase:
                failure_phase = worker_phase
            guardian_exit_code = (
                failure_details.get("guardian_exit_code")
                if isinstance(failure_details, Mapping)
                else None
            )
            os_errno = (
                failure_details.get("os_errno")
                if isinstance(failure_details, Mapping)
                else None
            )
            sqlite_errorcode = getattr(exc, "sqlite_errorcode", None)
            sqlite_errorname = str(getattr(exc, "sqlite_errorname", "") or "")
            failure = {
                "schema_version": _RAW_GENERATION_FAILURE_SCHEMA,
                "exception_type": (
                    failure_type
                    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{1,127}", failure_type)
                    else "UnknownError"
                ),
                "reason_code": (
                    reason_code
                    if re.fullmatch(r"[a-z][a-z0-9_]{2,127}", reason_code)
                    else ""
                ),
                "failure_phase": (
                    failure_phase
                    if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", failure_phase)
                    else ""
                ),
                "guardian_exit_code": (
                    int(guardian_exit_code)
                    if isinstance(guardian_exit_code, int)
                    and not isinstance(guardian_exit_code, bool)
                    and -128 <= guardian_exit_code <= 255
                    else None
                ),
                "os_errno": (
                    int(os_errno)
                    if isinstance(os_errno, int)
                    and not isinstance(os_errno, bool)
                    and 0 <= os_errno <= 255
                    else None
                ),
                "sqlite_errorcode": (
                    int(sqlite_errorcode)
                    if isinstance(sqlite_errorcode, int)
                    and not isinstance(sqlite_errorcode, bool)
                    and 0 <= sqlite_errorcode <= 0xFFFF
                    else None
                ),
                "sqlite_errorname": (
                    sqlite_errorname
                    if re.fullmatch(r"SQLITE_[A-Z0-9_]{1,63}", sqlite_errorname)
                    else ""
                ),
            }
            try:
                encoded = json.dumps(
                    failure,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                with output_path.open("wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                pass
            os._exit(90)
    os.close(parent_watch_read)
    killed_for_budget = False
    killed_for_timeout = False
    started = time.monotonic()
    try:
        process = psutil.Process(pid)
        baseline_rss = process.memory_info().rss
        while True:
            waited_pid, status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == pid:
                break
            try:
                if process.memory_info().rss - baseline_rss > dependencies.max_rss_bytes:
                    killed_for_budget = True
                    dependencies.kill_worker_process_group(pid)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            if time.monotonic() - started > dependencies.max_seconds:
                killed_for_timeout = True
                dependencies.kill_worker_process_group(pid)
            time.sleep(0.01)
        if killed_for_budget:
            raise AgentSourceRawReconciliationError("raw_generation_worker_budget_exceeded")
        if killed_for_timeout:
            raise AgentSourceRawReconciliationError("raw_generation_worker_timeout")
        exit_code = os.waitstatus_to_exitcode(status)
        if exit_code == 92:
            raise AgentSourceRawReconciliationError("raw_generation_worker_report_budget_exceeded")
        if exit_code != 0:
            details: dict[str, Any] = {}
            if exit_code == 90:
                try:
                    failure = json.loads(read_native_bytes(output_path))
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    failure = {}
                if (
                    isinstance(failure, Mapping)
                    and failure.get("schema_version")
                    == _RAW_GENERATION_FAILURE_SCHEMA
                    and set(failure)
                    == {
                        "schema_version",
                        "exception_type",
                        "reason_code",
                        "failure_phase",
                        "guardian_exit_code",
                        "os_errno",
                        "sqlite_errorcode",
                        "sqlite_errorname",
                    }
                    and re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_]{1,127}",
                        str(failure.get("exception_type") or ""),
                    )
                    and (
                        not failure.get("reason_code")
                        or re.fullmatch(
                            r"[a-z][a-z0-9_]{2,127}",
                            str(failure["reason_code"]),
                        )
                    )
                    and (
                        not failure.get("failure_phase")
                        or re.fullmatch(
                            r"[a-z][a-z0-9_]{2,63}",
                            str(failure["failure_phase"]),
                        )
                    )
                    and (
                        failure.get("guardian_exit_code") is None
                        or (
                            isinstance(failure["guardian_exit_code"], int)
                            and not isinstance(
                                failure["guardian_exit_code"],
                                bool,
                            )
                            and -128 <= failure["guardian_exit_code"] <= 255
                        )
                    )
                    and (
                        failure.get("os_errno") is None
                        or (
                            isinstance(failure["os_errno"], int)
                            and not isinstance(failure["os_errno"], bool)
                            and 0 <= failure["os_errno"] <= 255
                        )
                    )
                    and (
                        failure.get("sqlite_errorcode") is None
                        or (
                            isinstance(failure["sqlite_errorcode"], int)
                            and not isinstance(
                                failure["sqlite_errorcode"],
                                bool,
                            )
                            and 0 <= failure["sqlite_errorcode"] <= 0xFFFF
                        )
                    )
                    and (
                        not failure.get("sqlite_errorname")
                        or re.fullmatch(
                            r"SQLITE_[A-Z0-9_]{1,63}",
                            str(failure["sqlite_errorname"]),
                        )
                    )
                ):
                    details["worker_failure"] = dict(failure)
            raise AgentSourceRawReconciliationError(
                "raw_generation_worker_failed",
                details=details,
            )
        try:
            encoded = read_native_bytes(output_path)
            if not encoded or len(encoded) > dependencies.max_report_bytes:
                raise ValueError("invalid Raw generation report size")
            report = json.loads(encoded)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise AgentSourceRawReconciliationError(
                "raw_generation_worker_report_invalid"
            ) from None
        validated = validate_raw_generation_report(
            report,
            source_names=source_name_list,
            generation_number=generation_number,
        )
        validated["worker_isolation"] = raw_generation_worker_isolation_contract()
        return validated
    except AgentSourceRawReconciliationError:
        raise
    except (OSError, ChildProcessError, psutil.Error):
        raise AgentSourceRawReconciliationError("raw_generation_worker_failed") from None
    finally:
        try:
            waited_pid, _status = os.waitpid(pid, os.WNOHANG)
            if waited_pid == 0:
                dependencies.kill_worker_process_group(pid)
                os.waitpid(pid, 0)
        except (ChildProcessError, ProcessLookupError):
            pass
        try:
            os.close(parent_watch_write)
        except OSError:
            pass
        dependencies.remove_worker_root(worker_root)
