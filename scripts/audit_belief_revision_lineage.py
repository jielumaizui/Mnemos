#!/usr/bin/env python3
"""Strict COG-035 audit for canonical belief lineage and projections."""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.access_policy import AccessNarrowing, PrincipalEnvelope  # noqa: E402
from core.cognitive.access_control import (  # noqa: E402
    make_cognitive_access_envelope,
    validate_cognitive_access_envelope,
)
from core.cognitive.belief_migration import BeliefCandidateReconciler  # noqa: E402
from core.cognitive.belief_revision import (  # noqa: E402
    BeliefRevisionCommand,
    BeliefRevisionProjector,
    BeliefRevisionStore,
)
from core.cognitive.state_contract import (  # noqa: E402
    sha256_json,
    validate_cognitive_state_payload,
)
from core.cognitive.state_schema import (  # noqa: E402
    initialize_cognitive_state_schema,
    inspect_cognitive_state_schema,
)
from core.db_utils import render_sql  # noqa: E402
from core.cognitive.state_store import CognitiveStateStore  # noqa: E402
from core.cognitive_graph.store import CognitiveGraphStore  # noqa: E402
from core.config import get_config  # noqa: E402

REPORT_SCHEMA_VERSION = "mnemos.belief_revision_lineage_audit.v1"
NOW = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)
ZERO_METRICS = (
    "active_without_evidence",
    "multiple_current_revision",
    "unresolved_silent_conflict",
    "belief_acl_leak",
    "unresolved_projection_effect",
    "historical_candidate_active",
    "explanation_contract_gap",
    "projection_current_gap",
    "migration_semantic_inference",
    "rollback_residual",
    "semantic_replay_gap",
    "scope_identity_collision",
    "unknown_negative_conflation",
    "expiry_false_conflation",
)


def audit_belief_revision_lineage(
    *,
    live_state_db: Path,
    live_graph_db: Path,
    strict: bool,
) -> dict[str, Any]:
    matrix, matrix_metrics = _acceptance_matrix()
    live, live_metrics = _audit_live(live_state_db, live_graph_db)
    metrics = {
        key: int(matrix_metrics.get(key, 0)) + int(live_metrics.get(key, 0)) for key in ZERO_METRICS
    }
    failures = [name for name, passed in matrix["contracts"].items() if not passed]
    errors = [f"acceptance contract failed: {name}" for name in failures]
    errors.extend(f"{name}={value}" for name, value in metrics.items() if value)
    if not live.get("ok", False):
        errors.append("live cognitive state schema is not canonical")
    ok = not errors
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "ok": ok,
        "strict": bool(strict),
        "metrics": metrics,
        "matrix": matrix,
        "live": live,
        "errors": errors,
    }


def _acceptance_matrix() -> tuple[dict[str, Any], dict[str, int]]:
    contracts: dict[str, bool] = {}
    metrics = {key: 0 for key in ZERO_METRICS}
    with tempfile.TemporaryDirectory(prefix="mnemos-cog035-audit-") as temp:
        root = Path(temp)
        state_path = root / "producer_consumer_ledger.db"
        graph_path = root / "cognitive_graph.db"
        initialize_cognitive_state_schema(state_path)
        state = CognitiveStateStore(state_path)
        beliefs = BeliefRevisionStore(state)
        projector = BeliefRevisionProjector(state, CognitiveGraphStore(str(graph_path)))
        principal = _principal()

        first_command = _command(
            1,
            claim="Backups remain until retention expiry.",
            supporting=("evidence:support:1",),
        )
        first = beliefs.revise(first_command, principal=principal)
        replay = beliefs.revise(first_command, principal=principal)
        same_direction = beliefs.revise(
            _command(
                2,
                claim=first_command.claim,
                supporting=("evidence:support:2",),
                expected=first.revision_id,
            ),
            principal=principal,
        )
        disputed = beliefs.revise(
            _command(
                3,
                claim=first_command.claim,
                opposing=("evidence:oppose:1",),
                expected=same_direction.revision_id,
            ),
            principal=principal,
        )
        dispute_explanation = beliefs.explain(
            disputed.belief_id,
            principal=principal,
            narrowing=AccessNarrowing(project="mnemos"),
            now=NOW,
        )
        scope_fork = beliefs.revise(
            _command(
                4,
                claim=first_command.claim,
                project="other",
                supporting=("evidence:other",),
            ),
            principal=principal,
        )

        expiry_time = NOW + timedelta(minutes=1)
        expiring = beliefs.revise(
            _command(
                5,
                claim="A temporary maintenance window is active.",
                supporting=("evidence:window",),
                valid_until=expiry_time.isoformat(),
            ),
            principal=principal,
        )
        correction_source = beliefs.revise(
            _command(
                6,
                claim="The local retention flag is enabled.",
                supporting=("evidence:flag",),
            ),
            principal=principal,
        )
        corrected = beliefs.revise(
            _command(
                7,
                claim="The local retention flag is enabled.",
                expected=correction_source.revision_id,
                withdrawn=("evidence:flag",),
                correction_of=correction_source.revision_id,
                correction_evidence="source:7",
            ),
            principal=principal,
        )
        future_time = NOW + timedelta(minutes=2)
        beliefs.revise(
            _command(
                8,
                claim="A scheduled policy is active.",
                supporting=("evidence:schedule",),
                valid_from=future_time.isoformat(),
            ),
            principal=principal,
        )
        retry_revision = beliefs.revise(
            _command(
                9,
                claim="Projection retry retains exact effect identity.",
                supporting=("evidence:retry",),
            ),
            principal=principal,
        )

        restart_replay = BeliefRevisionStore(CognitiveStateStore(state_path)).revise(
            first_command,
            principal=principal,
        )
        try:
            projector.process_command(
                retry_revision.command_id,
                now=NOW,
                _failpoint=lambda stage: (
                    (_ for _ in ()).throw(RuntimeError("projection fault"))
                    if stage == "after_projection"
                    else None
                ),
            )
        except RuntimeError:
            pass
        hash_after_fault = projector.projection_hash(retry_revision.belief_id)
        pending_after_fault = any(
            value["command_id"] == retry_revision.command_id
            for value in state.pending_commands("cognitive_graph")
        )
        retry_effect = projector.process_command(retry_revision.command_id, now=NOW)
        retry_hash_stable = projector.projection_hash(retry_revision.belief_id) == hash_after_fault
        projection_result = projector.process_pending(now=NOW)

        denied = beliefs.explain(
            disputed.belief_id,
            principal=_principal("principal:not-owner"),
            narrowing=AccessNarrowing(project="mnemos"),
            now=NOW,
        )
        expired_explanation = beliefs.explain(
            expiring.belief_id,
            principal=principal,
            narrowing=AccessNarrowing(project="mnemos"),
            now=expiry_time + timedelta(seconds=1),
        )
        corrected_explanation = beliefs.explain(
            corrected.belief_id,
            principal=principal,
            narrowing=AccessNarrowing(project="mnemos"),
            now=NOW,
        )
        suppressed = projector.suppress_inactive_heads(now=expiry_time + timedelta(seconds=1))
        validity = projector.reconcile_validity(now=future_time + timedelta(seconds=1))

        migration = _candidate_migration_acceptance(root, state_path)
        rollback_clean = _fault_matrix(root)

        contracts.update(
            {
                "first_revision": first.status == "committed",
                "exact_replay": replay.status == "existing"
                and replay.revision_id == first.revision_id,
                "same_direction_evidence": same_direction.revision_id != first.revision_id,
                "support_opposition_disputed": dispute_explanation.stance == "disputed"
                and dispute_explanation.supporting_evidence
                == ("evidence:support:1", "evidence:support:2")
                and dispute_explanation.opposing_evidence == ("evidence:oppose:1",),
                "scope_fork": scope_fork.belief_id != first.belief_id,
                "expiry_not_false": not expired_explanation.active
                and expired_explanation.inactive_reason == "expired"
                and expired_explanation.stance == "supported",
                "explicit_correction": corrected_explanation.stance == "unknown"
                and corrected_explanation.withdrawn_evidence == ("evidence:flag",),
                "supersedes_lineage": dispute_explanation.revision_lineage
                == (
                    first.revision_id,
                    same_direction.revision_id,
                    disputed.revision_id,
                ),
                "restart_replay": restart_replay.status == "existing"
                and restart_replay.revision_id == first.revision_id,
                "projection_retry": pending_after_fault
                and retry_effect.status == "committed"
                and retry_hash_stable,
                "acl_denial_before_body": denied.status == "access_denied"
                and denied.claim == ""
                and denied.supporting_evidence == (),
                "projection_effect_closure": projection_result["failed"] == 0
                and projection_result["pending"] == 0,
                "expiry_projection_suppression": suppressed >= 1,
                "future_validity_activation": validity["activated"] >= 1,
                "migration_quarantine": migration,
                "uow_fault_rollback": rollback_clean,
                "caller_identity_not_exposed": not {
                    "belief_id",
                    "claim_id",
                    "stance",
                }.intersection(field.name for field in fields(BeliefRevisionCommand)),
                "explanation_contract": all(
                    (
                        dispute_explanation.current_revision_id,
                        dispute_explanation.scope[0],
                        dispute_explanation.scope[1],
                        dispute_explanation.valid_from,
                        dispute_explanation.confidence_method,
                        dispute_explanation.revision_lineage,
                    )
                ),
                "projection_identity": retry_effect.target_effect_id
                == retry_revision.projection_effect_id,
            }
        )

        database_metrics = _database_metrics(
            state_path,
            graph_path,
            now=future_time + timedelta(seconds=1),
        )
        metrics.update(database_metrics)
        metrics["rollback_residual"] = 0 if rollback_clean else 1
        metrics["semantic_replay_gap"] = 0 if contracts["exact_replay"] else 1
        metrics["scope_identity_collision"] = 0 if contracts["scope_fork"] else 1
        metrics["unknown_negative_conflation"] = 0 if contracts["explicit_correction"] else 1
        metrics["expiry_false_conflation"] = 0 if contracts["expiry_not_false"] else 1
        metrics["belief_acl_leak"] += 0 if contracts["acl_denial_before_body"] else 1

    passed_count = sum(1 for value in contracts.values() if value)
    return (
        {
            "contract_count": len(contracts),
            "passed_count": passed_count,
            "contracts": contracts,
        },
        metrics,
    )


def _candidate_migration_acceptance(root: Path, state_path: Path) -> bool:
    wiki = root / "prior-wiki"
    wiki.mkdir()
    (wiki / "candidate.md").write_text("# Unverified pre-canonical prose\n", encoding="utf-8")
    graph = root / "prior-graph.db"
    reflection = root / "prior-reflection.db"
    profile = root / "prior-profile.db"
    fixtures = (
        (
            graph,
            "CREATE TABLE cognitive_relations "
            "(id TEXT PRIMARY KEY, source TEXT, target TEXT, relation_type TEXT)",
            "INSERT INTO cognitive_relations VALUES ('r1','wiki://a','kg://b','related_to')",
        ),
        (
            reflection,
            "CREATE TABLE reflection_records (record_id TEXT PRIMARY KEY, body TEXT)",
            "INSERT INTO reflection_records VALUES ('f1','prior reflection')",
        ),
        (
            profile,
            "CREATE TABLE profile_assertions (assertion_id TEXT PRIMARY KEY, body TEXT)",
            "INSERT INTO profile_assertions VALUES ('p1','prior assertion')",
        ),
    )
    for path, ddl, insert in fixtures:
        with sqlite3.connect(path) as conn:
            conn.execute(ddl)
            conn.execute(insert)
    reconciler = BeliefCandidateReconciler(
        state_db=state_path,
        wiki_roots=(wiki,),
        cognitive_graph_dbs=(graph,),
        reflection_dbs=(reflection,),
        profile_dbs=(profile,),
    )
    dry_run = reconciler.reconcile()
    applied = reconciler.reconcile(
        apply=True,
        backup_dir=root / "migration-backup",
        confirm_daemon_stopped=True,
        expected_inventory_hash=dry_run["inventory_hash"],
    )
    return bool(
        dry_run["candidate_count"] == 4
        and dry_run["inserted_count"] == 0
        and applied["inserted_count"] == 4
        and applied["active_head_delta"] == 0
        and applied["active_revision_delta"] == 0
        and applied["state_integrity_check"] == "ok"
    )


def _fault_matrix(root: Path) -> bool:
    for index, boundary in enumerate(("after_revision", "after_event", "after_outbox"), 20):
        path = root / f"fault-{boundary}.db"
        initialize_cognitive_state_schema(path)
        store = BeliefRevisionStore(CognitiveStateStore(path))

        def failpoint(stage: str, expected: str = boundary) -> None:
            if stage == expected:
                raise RuntimeError(f"fault:{expected}")

        try:
            store.revise(
                _command(
                    index,
                    claim=f"Fault boundary {boundary} rolls back.",
                    supporting=(f"evidence:{boundary}",),
                ),
                principal=_principal(),
                _failpoint=failpoint,
            )
        except RuntimeError:
            pass
        with sqlite3.connect(path) as conn:
            counts = tuple(
                int(
                    conn.execute(
                        render_sql(
                            "SELECT COUNT(*) FROM {table}",
                            identifiers={"table": table},
                        )
                    ).fetchone()[0]
                )
                for table in (
                    "cognitive_state_revisions",
                    "cognitive_state_heads",
                    "cognitive_data_events",
                    "cognitive_state_outbox",
                )
            )
        if counts != (0, 0, 0, 0):
            return False
    return True


def _audit_live(state_path: Path, graph_path: Path) -> tuple[dict[str, Any], dict[str, int]]:
    if not state_path.is_file():
        return {"classification": "not_initialized", "ok": True}, {key: 0 for key in ZERO_METRICS}
    with sqlite3.connect(f"file:{state_path}?mode=ro", uri=True) as conn:
        schema = inspect_cognitive_state_schema(conn)
    if not schema.ok:
        return schema.as_dict(), {key: 0 for key in ZERO_METRICS}
    return (
        {**schema.as_dict(), "state_db": str(state_path), "graph_db": str(graph_path)},
        _database_metrics(state_path, graph_path, now=datetime.now(timezone.utc)),
    )


def _database_metrics(
    state_path: Path,
    graph_path: Path,
    *,
    now: datetime,
) -> dict[str, int]:
    metrics = {key: 0 for key in ZERO_METRICS}
    current_rows: list[tuple[str, str, Mapping[str, Any]]] = []
    with sqlite3.connect(f"file:{state_path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        grouped = conn.execute("""
            SELECT object_type, object_id, COUNT(*) AS total
            FROM cognitive_state_heads
            WHERE object_type='belief_revision'
            GROUP BY object_type, object_id HAVING COUNT(*) > 1
            """).fetchall()
        metrics["multiple_current_revision"] = len(grouped)
        rows = conn.execute("""
            SELECT r.* FROM cognitive_state_heads AS h
            JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
            WHERE h.object_type='belief_revision'
            """).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                validate_cognitive_state_payload("belief_revision", payload)
                validate_cognitive_access_envelope(
                    payload["access_control"],
                    expected_scope_type=str(row["scope_type"]),
                    expected_scope_id=str(row["scope_id"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                metrics["explanation_contract_gap"] += 1
                metrics["belief_acl_leak"] += 1
                continue
            supporting = set(payload["supporting_evidence"])
            opposing = set(payload["opposing_evidence"])
            if payload["stance"] in {"supported", "refuted", "disputed"} and not (
                supporting or opposing
            ):
                metrics["active_without_evidence"] += 1
            if supporting and opposing and payload["stance"] != "disputed":
                metrics["unresolved_silent_conflict"] += 1
            if (
                payload["belief_id"] != row["object_id"]
                or payload["supersedes_revision_id"] != str(row["supersedes_revision_id"] or "")
                or payload["correction_of_revision_id"]
                != str(row["correction_of_revision_id"] or "")
            ):
                metrics["explanation_contract_gap"] += 1
            current_rows.append((str(row["object_id"]), str(row["revision_id"]), payload))
        metrics["historical_candidate_active"] = int(conn.execute("""
                SELECT COUNT(*) FROM cognitive_state_heads AS h
                JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
                WHERE r.object_type='belief_revision' AND r.admission_state!='active'
                """).fetchone()[0])
        unresolved = conn.execute("""
            SELECT o.command_id, o.payload_json, r.status, r.target_effect_id,
                   r.before_hash, r.after_hash, r.evidence_refs
            FROM cognitive_state_outbox AS o
            LEFT JOIN cognitive_state_effect_receipts AS r ON r.command_id=o.command_id
            WHERE o.command_type='project_belief_revision'
            """).fetchall()
        for row in unresolved:
            try:
                payload = json.loads(str(row["payload_json"]))
                evidence = set(json.loads(str(row["evidence_refs"] or "[]")))
                valid = bool(
                    row["status"] == "committed"
                    and row["target_effect_id"] == payload["projection_effect_id"]
                    and str(row["before_hash"]).startswith("sha256:")
                    and str(row["after_hash"]).startswith("sha256:")
                    and f"belief-command:{row['command_id']}" in evidence
                    and f"belief-revision:{payload['revision_id']}" in evidence
                    and f"graph-projection:{row['after_hash']}" in evidence
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                valid = False
            if not valid:
                metrics["unresolved_projection_effect"] += 1
        quarantine_rows = conn.execute("""
            SELECT payload_json FROM cognitive_state_migration_quarantine
            WHERE reason_code='unverified_belief_candidate'
            """).fetchall()
        forbidden = {
            "belief_id",
            "claim_id",
            "stance",
            "confidence",
            "supersedes_revision_id",
            "correction_of_revision_id",
            "body",
        }
        for row in quarantine_rows:
            try:
                payload = json.loads(str(row[0]))
                valid = bool(
                    payload.get("classification") == "unverified_candidate"
                    and payload.get("active_schema_upgrade") is False
                    and not forbidden.intersection(payload)
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                valid = False
            if not valid:
                metrics["migration_semantic_inference"] += 1

    if current_rows:
        if not graph_path.is_file():
            metrics["projection_current_gap"] += sum(
                1 for _, _, payload in current_rows if _payload_active(payload, now)
            )
        else:
            with sqlite3.connect(f"file:{graph_path}?mode=ro", uri=True) as graph:
                for belief_id, revision_id, payload in current_rows:
                    count = int(
                        graph.execute(
                            """
                            SELECT COUNT(*) FROM cognitive_relations
                            WHERE source=? AND target=?
                              AND relation_type='current_belief_revision' AND stale=0
                            """,
                            (
                                f"belief://{belief_id}",
                                f"belief-revision://{revision_id}",
                            ),
                        ).fetchone()[0]
                    )
                    expected = 1 if _payload_active(payload, now) else 0
                    if count != expected:
                        metrics["projection_current_gap"] += 1
    return metrics


def _payload_active(payload: Mapping[str, Any], now: datetime) -> bool:
    if payload["stance"] in {"unknown", "deprecated"}:
        return False
    valid_from = datetime.fromisoformat(str(payload["valid_from"]).replace("Z", "+00:00"))
    if now < valid_from:
        return False
    valid_until = str(payload["valid_until"] or "")
    return not valid_until or now < datetime.fromisoformat(valid_until.replace("Z", "+00:00"))


def _principal(principal_id: str = "principal:belief-owner") -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id=principal_id,
        agent="codex",
        host_kind="audit",
        capability_id="cog035-audit",
        capabilities=frozenset({"memory_write"}),
        allowed_projects=frozenset({"mnemos", "other"}),
        allowed_source_agents=frozenset({"codex"}),
    )


def _command(
    source_no: int,
    *,
    claim: str,
    project: str = "mnemos",
    supporting: tuple[str, ...] = (),
    opposing: tuple[str, ...] = (),
    withdrawn: tuple[str, ...] = (),
    expected: str = "",
    correction_of: str = "",
    correction_evidence: str = "",
    valid_from: str = "",
    valid_until: str = "",
) -> BeliefRevisionCommand:
    source_id = f"source:{source_no}"
    digest = sha256_json(source_id)
    access = make_cognitive_access_envelope(
        owner_principal_id="principal:belief-owner",
        owner_agent="codex",
        scope_type="project",
        scope_id=project,
        project=project,
        purposes=("belief_read", "cognitive_state_write"),
        consent_provenance_refs=(source_id,),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=(digest,),
    )
    return BeliefRevisionCommand(
        claim=claim,
        claim_kind="fact",
        scope_type="project",
        scope_id=project,
        source_id=source_id,
        source_revision_id=f"revision:{source_no}",
        source_content_hash=digest,
        source_access_control=access,
        supporting_evidence=supporting,
        opposing_evidence=opposing,
        withdrawn_evidence=withdrawn,
        valid_from=valid_from or NOW.isoformat(),
        valid_until=valid_until,
        invalidation_conditions=("source evidence changes",),
        expected_current_revision_id=expected,
        correction_of_revision_id=correction_of,
        correction_evidence_ref=correction_evidence,
        created_at=NOW.isoformat(),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", type=Path)
    parser.add_argument("--graph-db", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = get_config()
    state_db = (
        args.state_db or Path(config.database_dir) / "producer_consumer_ledger.db"
    ).expanduser()
    graph_db = (args.graph_db or Path(config.cognitive_graph_db_path)).expanduser()
    report = audit_belief_revision_lineage(
        live_state_db=state_db,
        live_graph_db=graph_db,
        strict=args.strict,
    )
    print(
        json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=None if args.json else 2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
