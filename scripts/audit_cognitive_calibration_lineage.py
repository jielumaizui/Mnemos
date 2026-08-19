#!/usr/bin/env python3
"""Audit COG-049 calibration lineage, persistence and projection invariants."""

from __future__ import annotations

import argparse
import ast
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognitive.auto_calibration import (  # noqa: E402
    CalibrationEngine,
    CrossSourceValidator,
)
from core.cognitive.calibration_math import recompute_posterior  # noqa: E402
from core.cognitive.calibration_record import CalibrationRecordStore  # noqa: E402
from core.cognitive.models import (  # noqa: E402
    Dimension,
    Observation,
    ObservationBatch,
    ObservationType,
)
from core.cognitive.observation_calibration_schema import (  # noqa: E402
    inspect_observation_calibration_schema,
)
from core.cognitive.observation_store import ObservationStore  # noqa: E402
from core.cognitive.sources import SourceItem  # noqa: E402
from core.cognitive.state_contract import (  # noqa: E402
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_schema import (  # noqa: E402
    initialize_cognitive_state_schema,
    inspect_cognitive_state_schema,
)
from core.cognitive.state_store import CognitiveStateStore  # noqa: E402
from core.cognitive.wiki_exporter import WikiExporter  # noqa: E402
from core.ops.audit_run import (  # noqa: E402
    AuditExecutionEnvironment,
    audit_database_state_targets,
    discover_audit_formal_directory_targets,
    discover_audit_formal_state_targets,
    verify_os_write_denied,
)
from core.runtime_environment import environment_snapshot  # noqa: E402

AUDIT_SCHEMA_VERSION = "mnemos.cognitive_calibration_lineage_audit.v1"

ZERO_BUDGET_METRICS = (
    "derived_source_double_count",
    "calibration_without_record",
    "calibration_record_hash_mismatch",
    "calibration_input_hash_mismatch",
    "calibration_spec_hash_mismatch",
    "prior_posterior_binding_mismatch",
    "source_span_binding_mismatch",
    "stale_spec_current_count",
    "partial_calibration_pointer_count",
    "unbound_posterior_count",
    "calibrated_unverified_base_count",
    "projection_calibration_hash_mismatch",
    "observation_id_projection_gap",
    "source_span_projection_gap",
    "omission_receipt_gap",
    "orphan_current_calibration_count",
)


class _EpochCognitiveStateStore(CognitiveStateStore):
    """Read-only state facade whose every query is bound to the audit epoch."""

    def __init__(
        self,
        state_path: Path,
        audit: AuditExecutionEnvironment,
    ) -> None:
        super().__init__(state_path)
        self._audit = audit

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if not read_only:
            raise PermissionError("audit cognitive state facade is read-only")
        return self._audit.open_sqlite_readonly(self.db_path)


def _os_write_deny_identity() -> tuple[Path | None, dict[str, object] | None]:
    path = os.environ.get("MNEMOS_AUDIT_WRITE_DENY_SENTINEL")
    device = os.environ.get("MNEMOS_AUDIT_WRITE_DENY_DEVICE")
    inode = os.environ.get("MNEMOS_AUDIT_WRITE_DENY_INODE")
    sha256 = os.environ.get("MNEMOS_AUDIT_WRITE_DENY_SHA256")
    if not all((path, device, inode, sha256)):
        return None, None
    return Path(str(path)), {
        "device": int(str(device)),
        "inode": int(str(inode)),
        "sha256": str(sha256),
    }


def _static_contract() -> dict[str, Any]:
    ddl_owners: list[str] = []
    unverified_binding_calls: list[str] = []
    legacy_public_binders: list[str] = []
    allowed_binder = "core/cognitive/calibration_record.py"
    for path in sorted((ROOT / "core").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        if "CALIBRATION_COLUMN_DDL =" in source:
            ddl_owners.append(relative)
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name == "apply_calibration"
            ):
                legacy_public_binders.append(f"{relative}:{node.lineno}")
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "_apply_committed_calibration" and relative != allowed_binder:
                unverified_binding_calls.append(f"{relative}:{node.lineno}")
    return {
        "observation_calibration_schema_owners": ddl_owners,
        "schema_owner_count": len(ddl_owners),
        "unverified_binding_calls": unverified_binding_calls,
        "unverified_binding_callsite_count": len(unverified_binding_calls),
        "legacy_public_binders": legacy_public_binders,
        "legacy_public_binder_count": len(legacy_public_binders),
    }


def _raw(revision_id: str, text: str) -> SourceItem:
    return SourceItem(
        source_type="raw",
        file_path=f"raw://{revision_id}",
        content=text,
        raw_revision_id=revision_id,
        source_content_hash=("sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()),
    )


def _derived(revision_id: str, text: str, raw_text: str = "AI evidence") -> SourceItem:
    return SourceItem(
        source_type="wiki",
        file_path=f"/wiki/{revision_id}.md",
        content=text,
        frontmatter={
            "raw_event_refs": [
                {
                    "revision_id": revision_id,
                    "content_hash": (
                        "sha256:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
                    ),
                    "span_start": 0,
                    "span_end": len(raw_text),
                }
            ]
        },
    )


def _observation() -> Observation:
    return Observation(
        id="audit-observation",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value={"concepts": {"ai": 2}, "dominant": "ai", "total_mentions": 2},
        confidence=0.6,
        source_path="aggregated:raw:1,wiki:1",
        source_id="aggregate",
        evidence=["AI evidence"],
        source_span_ids=["raw-span:audit-raw:0:11"],
    )


def _synthetic_report() -> dict[str, Any]:
    with TemporaryDirectory(prefix="mnemos-calibration-audit-") as raw_temp, ExitStack() as stack:
        audit = stack.enter_context(
            AuditExecutionEnvironment.isolated(
                Path(raw_temp) / "run",
            )
        )
        root = audit.root
        if root is None:
            raise RuntimeError("isolated calibration audit has no run root")
        observation_store = ObservationStore(
            str(root / "observations.db"),
            ownership_config=audit.runtime_config,
        )
        state_path = root / "producer_consumer_ledger.db"
        initialize_cognitive_state_schema(state_path)
        state_store = CognitiveStateStore(state_path)
        records = CalibrationRecordStore(state_store)
        engine = CalibrationEngine(validators=[CrossSourceValidator()])
        observation = _observation()
        observation_store.save(observation)
        sources = [_raw("audit-raw", "AI evidence"), _derived("audit-raw", "AI summary")]
        first = engine.calibrate(observation, [observation], sources)
        independent = engine.calibrate(
            observation,
            [observation],
            [_raw("independent-a", "AI evidence"), _raw("independent-b", "AI evidence")],
        )
        counter = engine.calibrate(
            observation,
            [observation],
            [_raw("supporting", "AI evidence"), _raw("counter", "gardening only")],
        )
        first_commit, persisted = records.commit(observation, first)
        second = engine.calibrate(observation, [observation], sources)
        second_commit, second_persisted = records.commit(observation, second)
        records.apply_to_observation(observation_store, first_commit)
        rebound = observation_store.get_by_id(observation.id)
        if rebound is None:
            raise RuntimeError("synthetic Observation disappeared")
        batch = ObservationBatch(observations=[rebound])
        lifecycle = audit.create_projection_lifecycle(root / "wiki")
        exporter = WikiExporter(str(root / "wiki"), lifecycle=lifecycle)
        first_projection = exporter.export_batch(batch, {rebound.id: persisted})[rebound.id]
        first_text = (root / "wiki/L3-Observations/attention.md").read_text(encoding="utf-8")
        second_projection = exporter.export_batch(batch, {rebound.id: persisted})[rebound.id]
        second_text = (root / "wiki/L3-Observations/attention.md").read_text(encoding="utf-8")
        current = state_store.current_revision("calibration_record", observation.id)
        if current is None:
            raise RuntimeError("synthetic CalibrationRecord disappeared")
        validate_cognitive_state_payload("calibration_record", current.payload)
        stale = records.current_reports(
            [observation.id], expected_spec_hash="sha256:upgraded-spec"
        )[observation.id]
        hash_pattern = re.compile(r'calibration_set_hash: "([^\"]+)"')
        first_hash = hash_pattern.search(first_text)
        second_hash = hash_pattern.search(second_text)
        synthetic: dict[str, Any] = {
            "derived_source_double_count": first.derived_source_double_count,
            "derived_members_deduplicated": first.derived_members_deduplicated,
            "independent_cluster_count": len(first.independent_evidence_clusters),
            "true_independent_support_count": len(independent.supporting_evidence),
            "counter_evidence_count": len(counter.counter_evidence),
            "same_input_spec_revision_equal": (
                first_commit.revision_id == second_commit.revision_id
            ),
            "same_input_spec_record_hash_equal": (
                persisted.calibration_record_hash == second_persisted.calibration_record_hash
            ),
            "prior_posterior_recomputable": bool(
                current.payload["calculation_input_hash"]
                == sha256_json(current.payload["input_snapshot"])
                and float(current.payload["posterior"])
                == recompute_posterior(
                    float(current.payload["prior"]),
                    current.payload["validations"],
                    prior_weight=float(
                        current.payload["input_snapshot"]["validator_spec"]["prior_weight"]
                    ),
                )
            ),
            "spec_upgrade_marks_old_stale": stale.stale,
            "projection_hash_equal": bool(
                first_hash
                and second_hash
                and first_hash.group(1) == second_hash.group(1)
                and first_projection.calibration_set_hash == second_projection.calibration_set_hash
            ),
            "projection_has_observation_id": observation.id in second_text,
            "projection_has_calibration_id": persisted.calibration_revision_id in second_text,
            "projection_has_source_span_id": "raw-span:audit-raw:0:11" in second_text,
            "projection_has_omission_receipt": "omission:" in second_text,
        }
        stack.close()
        synthetic["audit_execution"] = audit.report()
        return synthetic


def _read_observations(
    db_path: Path,
    audit: AuditExecutionEnvironment,
) -> tuple[dict[str, Any], list[sqlite3.Row]]:
    if not db_path.is_file():
        return {"classification": "uninitialized", "ok": True}, []
    with audit.open_sqlite_readonly(db_path) as conn:
        schema = inspect_observation_calibration_schema(conn)
        if schema.get("classification") == "absent":
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if tables:
                return {
                    "classification": "unknown_or_partial",
                    "migration_required": True,
                    "errors": [
                        "observation database contains unrelated tables "
                        "without canonical anchors"
                    ],
                    "ok": False,
                }, []
            return {"classification": "uninitialized", "ok": True}, []
        if not schema["ok"]:
            return schema, []
        rows = conn.execute("""
            SELECT id, dimension, confidence, base_confidence,
                   base_measurement_status,
                   calibration_revision_id, calibration_input_hash,
                   calibration_spec_hash, calibration_record_hash,
                   source_span_ids
            FROM observations
            """).fetchall()
    return schema, rows


def _read_state_store(
    state_path: Path,
    audit: AuditExecutionEnvironment,
) -> tuple[dict[str, Any], CognitiveStateStore | None]:
    if not state_path.is_file():
        return {"classification": "uninitialized", "ok": True}, None
    try:
        with audit.open_sqlite_readonly(state_path) as conn:
            state = inspect_cognitive_state_schema(conn)
    except sqlite3.Error as exc:
        return {
            "classification": "unreadable",
            "ok": False,
            "error": str(exc),
        }, None
    if state.classification == "absent" and not state.tables:
        return {"classification": "uninitialized", "ok": True}, None
    if state.classification == "absent":
        return {
            "classification": "unknown_or_partial",
            "migration_required": True,
            "errors": [
                "cognitive state database contains unrelated tables without canonical anchors"
            ],
            "ok": False,
        }, None
    state_report = state.as_dict()
    if not state.ok:
        return state_report, None
    return state_report, _EpochCognitiveStateStore(state_path, audit)


def _live_metrics(
    database_dir: Path,
    wiki_dir: Path | None,
    current_spec_hash: str,
    audit: AuditExecutionEnvironment,
) -> dict[str, Any]:
    metrics = {name: 0 for name in ZERO_BUDGET_METRICS}
    observations_path = database_dir / "observations.db"
    state_path = database_dir / "producer_consumer_ledger.db"
    schema, rows = _read_observations(observations_path, audit)
    metrics["partial_calibration_pointer_count"] = int(schema.get("partial_pointer_count", 0))
    metrics["unbound_posterior_count"] = int(schema.get("unbound_posterior_count", 0))
    metrics["calibrated_unverified_base_count"] = int(
        schema.get("calibrated_unverified_base_count", 0)
    )
    calibrated = [row for row in rows if str(row["calibration_revision_id"] or "")]
    state_schema, state_store = _read_state_store(state_path, audit)
    referenced_current: set[str] = set()
    projection_texts: dict[Path, str] = {}
    for row in calibrated:
        revision_id = str(row["calibration_revision_id"])
        revision = state_store.revision(revision_id) if state_store is not None else None
        current = (
            state_store.current_revision("calibration_record", str(row["id"]))
            if state_store is not None
            else None
        )
        if revision is None or current is None or current.revision_id != revision_id:
            metrics["calibration_without_record"] += 1
            continue
        referenced_current.add(revision_id)
        try:
            validate_cognitive_state_payload("calibration_record", revision.payload)
        except (TypeError, ValueError):
            metrics["calibration_input_hash_mismatch"] += 1
        if str(row["calibration_record_hash"]) != revision.payload_hash:
            metrics["calibration_record_hash_mismatch"] += 1
        if str(row["calibration_input_hash"]) != str(
            revision.payload.get("calculation_input_hash")
        ):
            metrics["calibration_input_hash_mismatch"] += 1
        if str(row["calibration_spec_hash"]) != str(revision.payload.get("validator_spec_hash")):
            metrics["calibration_spec_hash_mismatch"] += 1
        source_span_ids = json.loads(str(row["source_span_ids"] or "[]"))
        if source_span_ids != list(revision.payload.get("source_span_ids", ())):
            metrics["source_span_binding_mismatch"] += 1
        if (
            abs(float(row["base_confidence"]) - float(revision.payload.get("prior", -1))) > 1e-9
            or abs(float(row["confidence"]) - float(revision.payload.get("posterior", -1))) > 1e-9
        ):
            metrics["prior_posterior_binding_mismatch"] += 1
        if str(revision.payload.get("validator_spec_hash")) != current_spec_hash:
            metrics["stale_spec_current_count"] += 1
        metrics["derived_source_double_count"] += int(
            revision.payload.get("derived_source_double_count", 0)
        )
        if wiki_dir is not None:
            projection = wiki_dir / "L3-Observations" / f"{row['dimension']}.md"
            if projection not in projection_texts:
                projection_texts[projection] = (
                    projection.read_text(encoding="utf-8") if projection.is_file() else ""
                )
            text = projection_texts[projection]
            if str(row["calibration_record_hash"]) not in text:
                metrics["projection_calibration_hash_mismatch"] += 1
            if str(row["id"]) not in text or revision_id not in text:
                metrics["observation_id_projection_gap"] += 1
            if any(str(source_span_id) not in text for source_span_id in source_span_ids[:20]):
                metrics["source_span_projection_gap"] += 1
            receipt_ids = [
                str(value.get("receipt_id") or "")
                for value in revision.payload.get("omission_receipts", ())
                if isinstance(value, Mapping)
            ]
            if any(receipt_id not in text for receipt_id in receipt_ids):
                metrics["omission_receipt_gap"] += 1

    current_count = 0
    if state_store is not None:
        current_revisions = state_store.current_revisions(object_type="calibration_record")
        current_count = len(current_revisions)
        metrics["orphan_current_calibration_count"] = sum(
            1 for revision in current_revisions if revision.revision_id not in referenced_current
        )
    return {
        "initialized": observations_path.is_file(),
        "observation_schema": schema,
        "cognitive_state_schema": state_schema,
        "calibrated_observation_count": len(calibrated),
        "historical_unverified_base_count": int(schema.get("historical_unverified_base_count", 0)),
        "current_calibration_record_count": current_count,
        "metrics": metrics,
    }


def build_report(
    database_dir: Path,
    wiki_dir: Path | None,
    *,
    extra_formal_targets: tuple[Path, ...] = (),
    extra_formal_directory_targets: tuple[Path, ...] = (),
    test_only_sandbox_readonly: bool = False,
) -> dict[str, Any]:
    engine = CalibrationEngine()
    static = _static_contract()
    synthetic = _synthetic_report()
    database_names = ("observations.db", "producer_consumer_ledger.db")
    formal_environment = environment_snapshot()
    readonly_targets = [
        database_dir / suffix
        for name in database_names
        for suffix in (name, f"{name}-wal", f"{name}-shm")
    ]
    readonly_targets.extend(discover_audit_formal_state_targets(formal_environment))
    readonly_targets.extend(extra_formal_targets)
    directory_targets = [
        database_dir,
        *discover_audit_formal_directory_targets(formal_environment),
        *extra_formal_directory_targets,
    ]
    if wiki_dir is not None:
        directory_targets.append(wiki_dir)
        observation_projection_dir = wiki_dir / "L3-Observations"
        directory_targets.append(observation_projection_dir)
        if observation_projection_dir.is_dir():
            readonly_targets.extend(sorted(observation_projection_dir.glob("*.md")))
    epoch_parent = (
        Path(formal_environment["MNEMOS_RUN_ROOT"]) / "tmp" if test_only_sandbox_readonly else None
    )
    with TemporaryDirectory(
        prefix="mnemos-calibration-evidence-epoch-",
        dir=epoch_parent,
    ) as raw_epoch:
        required_databases = tuple(database_dir / name for name in database_names)
        evidence_snapshot_root = Path(raw_epoch) / "snapshots"
        if test_only_sandbox_readonly:
            readonly_audit = AuditExecutionEnvironment.sandbox_readonly(
                readonly_targets,
                directory_targets=directory_targets,
                required_sqlite_databases=required_databases,
                evidence_snapshot_root=evidence_snapshot_root,
                writer_inactive=lambda _root: True,
            )
        else:
            readonly_audit = AuditExecutionEnvironment.production_readonly(
                readonly_targets,
                directory_targets=directory_targets,
                required_sqlite_databases=required_databases,
                evidence_snapshot_root=evidence_snapshot_root,
                write_deny_probe=_os_write_deny_identity()[0],
                write_deny_identity=_os_write_deny_identity()[1],
            )
        with readonly_audit:
            live = _live_metrics(database_dir, wiki_dir, engine.spec_hash, readonly_audit)
        audit_execution = readonly_audit.report()
    return _assemble_report(
        engine=engine,
        static=static,
        synthetic=synthetic,
        live=live,
        audit_execution=audit_execution,
        audit_scope=(
            "sandbox_readonly_test_fixture" if test_only_sandbox_readonly else "production_readonly"
        ),
        production_evidence=not test_only_sandbox_readonly,
    )


def build_static_report() -> dict[str, Any]:
    """Build the cross-platform static and isolated synthetic contract report."""

    engine = CalibrationEngine()
    static = _static_contract()
    synthetic = _synthetic_report()
    live = {
        "initialized": False,
        "observation_schema": {"classification": "uninitialized", "ok": True},
        "cognitive_state_schema": {"classification": "uninitialized", "ok": True},
        "calibrated_observation_count": 0,
        "historical_unverified_base_count": 0,
        "current_calibration_record_count": 0,
        "metrics": {name: 0 for name in ZERO_BUDGET_METRICS},
    }
    return _assemble_report(
        engine=engine,
        static=static,
        synthetic=synthetic,
        live=live,
        audit_execution=synthetic["audit_execution"],
        audit_scope="isolated_static_contract",
        production_evidence=False,
    )


def _assemble_report(
    *,
    engine: CalibrationEngine,
    static: dict[str, Any],
    synthetic: dict[str, Any],
    live: dict[str, Any],
    audit_execution: dict[str, Any],
    audit_scope: str,
    production_evidence: bool,
) -> dict[str, Any]:
    synthetic_failures = [
        name
        for name, value in {
            "derived_lineage_dedup": (
                synthetic["derived_source_double_count"] == 0
                and synthetic["derived_members_deduplicated"] == 1
                and synthetic["independent_cluster_count"] == 1
            ),
            "independent_and_counter_evidence": (
                synthetic["true_independent_support_count"] == 2
                and synthetic["counter_evidence_count"] == 1
            ),
            "same_input_spec_replay": synthetic["same_input_spec_revision_equal"]
            and synthetic["same_input_spec_record_hash_equal"],
            "prior_posterior_recompute": synthetic["prior_posterior_recomputable"],
            "spec_staleness": synthetic["spec_upgrade_marks_old_stale"],
            "projection_comparator": synthetic["projection_hash_equal"],
            "projection_ids": synthetic["projection_has_observation_id"]
            and synthetic["projection_has_calibration_id"]
            and synthetic["projection_has_source_span_id"]
            and synthetic["projection_has_omission_receipt"],
        }.items()
        if not value
    ]
    live_failures = [name for name in ZERO_BUDGET_METRICS if int(live["metrics"][name]) != 0]
    if live["initialized"] and not live["observation_schema"].get("ok"):
        live_failures.append("observation_schema")
    if not live["cognitive_state_schema"].get("ok"):
        live_failures.append("cognitive_state_schema")
    static_failures = []
    if static["observation_calibration_schema_owners"] != [
        "core/cognitive/observation_calibration_schema.py"
    ]:
        static_failures.append("observation_calibration_schema_owner")
    if static["unverified_binding_callsite_count"]:
        static_failures.append("unverified_calibration_binding_callsite")
    if static["legacy_public_binder_count"]:
        static_failures.append("legacy_public_calibration_binder")
    failures = sorted(set(synthetic_failures + live_failures + static_failures))
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "ok": not failures,
        "validator_spec_hash": engine.spec_hash,
        "zero_budget_metrics": list(ZERO_BUDGET_METRICS),
        "static_contract": static,
        "synthetic": synthetic,
        "live": live,
        "audit_execution": audit_execution,
        "audit_scope": audit_scope,
        "production_evidence": production_evidence,
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dir", type=Path)
    parser.add_argument("--wiki-dir", type=Path)
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.static_only:
        report = build_static_report()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if args.strict and not report["ok"] else 0
    from core.config import Config

    config = Config(provision=False)
    database_dir = args.database_dir or Path(config.database_dir)
    wiki_dir = args.wiki_dir or Path(config.wiki_dir)
    extra_directory_targets = [
        Path(config.mnemos_dir),
        Path(config.database_dir),
        Path(config.wiki_dir),
        Path(config.vault_dir("raw")),
    ]
    desktop = Path.home() / "Desktop"
    extra_targets = (
        desktop / "Mnemos-Phase0-7全局工程修复合同-2026-07-24.md",
        desktop / "Mnemos认知链路审计与全量修复方案-2026-07-12.md",
        Path(config.config_path),
        *audit_database_state_targets(Path(config.database_dir)),
    )
    report = build_report(
        database_dir,
        wiki_dir,
        extra_formal_targets=extra_targets,
        extra_formal_directory_targets=tuple(extra_directory_targets),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.strict and not report["ok"] else 0


_CHILD_ENV_KEYS = frozenset(
    {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "TZ",
        "TERM",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONNOUSERSITE",
        "HOME",
        "USERPROFILE",
        "MNEMOS_DIR",
        "MNEMOS_DATABASE_DIR",
        "MNEMOS_WIKI_DIR",
        "MNEMOS_OBSIDIAN_CONFIG_PATH",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    }
)


def _run_cli_with_os_write_deny(argv: list[str]) -> int:
    if "--static-only" in argv:
        return main(argv)
    if os.environ.get("MNEMOS_AUDIT_OS_WRITE_DENY") == "sandbox-exec-v1":
        sentinel, identity = _os_write_deny_identity()
        if sentinel is None or identity is None:
            raise RuntimeError("sandbox-exec marker is missing its bound sentinel")
        verify_os_write_denied(
            sentinel,
            expected_device=int(str(identity["device"])),
            expected_inode=int(str(identity["inode"])),
            expected_sha256=str(identity["sha256"]),
        )
        return main(argv)
    if sys.platform != "darwin":
        raise RuntimeError(
            "production calibration audit requires a supported OS write-deny adapter"
        )
    sandbox_exec = shutil.which("sandbox-exec")
    if sandbox_exec is None:
        raise RuntimeError("sandbox-exec is required for production calibration audit")
    with (
        TemporaryDirectory(prefix="mnemos-calibration-os-guard-") as raw_guard,
        TemporaryDirectory(prefix="mnemos-calibration-os-sentinel-") as raw_sentinel,
    ):
        guard_root = Path(raw_guard).resolve()
        sentinel = Path(raw_sentinel).resolve() / "write-deny-sentinel"
        sentinel.write_bytes(os.urandom(32))
        sentinel.chmod(0o600)
        sentinel_stat = sentinel.stat()
        sentinel_sha256 = hashlib.sha256(sentinel.read_bytes()).hexdigest()
        temporary = guard_root / "tmp"
        pycache = guard_root / "pycache"
        temporary.mkdir()
        pycache.mkdir()
        profile = "\n".join(
            [
                "(version 1)",
                "(allow default)",
                "(deny file-write*)",
                f'(allow file-write* (subpath "{guard_root}"))',
            ]
        )
        environment = {key: value for key, value in os.environ.items() if key in _CHILD_ENV_KEYS}
        environment.update(
            {
                "TMPDIR": str(temporary),
                "TEMP": str(temporary),
                "TMP": str(temporary),
                "PYTHONPYCACHEPREFIX": str(pycache),
                "PYTHONDONTWRITEBYTECODE": "1",
                "MNEMOS_AUDIT_OS_WRITE_DENY": "sandbox-exec-v1",
                "MNEMOS_AUDIT_WRITE_DENY_SENTINEL": str(sentinel),
                "MNEMOS_AUDIT_WRITE_DENY_DEVICE": str(sentinel_stat.st_dev),
                "MNEMOS_AUDIT_WRITE_DENY_INODE": str(sentinel_stat.st_ino),
                "MNEMOS_AUDIT_WRITE_DENY_SHA256": sentinel_sha256,
            }
        )
        completed = subprocess.run(
            [
                sandbox_exec,
                "-p",
                profile,
                sys.executable,
                str(Path(__file__).resolve()),
                *argv,
            ],
            env=environment,
            check=False,
        )
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(_run_cli_with_os_write_deny(sys.argv[1:]))
