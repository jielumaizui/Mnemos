#!/usr/bin/env python3
"""Audit the COG-047 canonical state, ledger, outbox and action invariants."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cognition_episode_contract import (  # noqa: E402
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_SCHEMA_VERSION,
)
from core.cognitive.state_contract import (  # noqa: E402
    CognitiveStateRevision,
    LocalConsumerCommand,
    sha256_json,
)
from core.cognitive.access_control import make_cognitive_access_envelope  # noqa: E402
from core.cognitive.state_schema import (  # noqa: E402
    STATE_SCHEMA_VERSION,
    initialize_cognitive_state_schema,
    inspect_cognitive_state_schema,
)
from core.cognitive.state_store import CognitiveStateStore  # noqa: E402
from core.config import get_config  # noqa: E402
from core.ops.action_ledger_schema import (  # noqa: E402
    SCHEMA_VERSION as ACTION_LEDGER_SCHEMA_VERSION,
    initialize_action_ledger_schema,
    inspect_action_ledger_schema,
)
from core.ops.cognitive_data_contract import CognitiveDataEvent  # noqa: E402
from core.system_contracts import (  # noqa: E402
    ActionLedger,
    make_quality_gate_observation,
)

REPORT_SCHEMA_VERSION = "mnemos.cognitive_state_store_audit.v1"
ZERO_METRICS = (
    "metadata_only_cognition",
    "consumed_without_event",
    "aggregate_consumed_with_missing_consumer",
    "multiple_current_revision",
    "mutable_action_evidence",
    "semantic_revision_without_envelope",
    "envelope_without_semantic_revision",
    "partial_facade_commit",
    "outbox_without_source_commit",
    "inactive_revision_in_active_head",
    "effect_receipt_without_command",
    "effect_receipt_reciprocity_gap",
    "effect_receipt_evidence_gap",
    "revision_hash_mismatch",
    "outbox_hash_mismatch",
    "current_state_hash_mismatch",
)


def _ddl_owners(root: Path, table_name: str) -> list[str]:
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?" + re.escape(table_name) + r"\b",
        re.IGNORECASE,
    )
    owners: list[str] = []
    for directory in ("core", "integrations", "daemon", "scripts"):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if pattern.search(path.read_text(encoding="utf-8")):
                owners.append(str(path.relative_to(root)))
    return owners


def _forbidden_action_replaces(root: Path) -> list[str]:
    pattern = re.compile(
        r"INSERT\s+OR\s+REPLACE\s+INTO\s+action_ledger\b",
        re.IGNORECASE,
    )
    matches: list[str] = []
    for path in sorted((root / "core").rglob("*.py")):
        if pattern.search(path.read_text(encoding="utf-8")):
            matches.append(str(path.relative_to(root)))
    return matches


def _episode(event_id: str, content_hash: str) -> CognitiveStateRevision:
    evidence_ref = f"raw-event:{event_id}#0:64"
    source_revision_id = f"raw-revision:{event_id}"
    authority_id = f"source-authority:{event_id}"
    source_span = {
        "source_authority_id": authority_id,
        "revision_id": event_id,
        "role": "user",
        "span_start": 0,
        "span_end": 64,
        "span_status": "exact",
        "content_sha256": content_hash,
        "source_revision_sha256": content_hash,
    }
    authority_catalog = {
        "schema_version": "mnemos.source_authority_catalog.v1",
        "entries": [
            {
                "source_authority_id": authority_id,
                "source_authority": "explicit_user",
                "source_event_id": event_id,
                "role": "user",
                "purpose": "user_instruction",
                "content_sha256": content_hash,
                "span_start": 0,
                "span_end": 64,
                "span_status": "exact",
                "source_revision_sha256": content_hash,
                "artifact_ref_id": "",
                "allows_cognitive_update": True,
            }
        ],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    artifact_catalog = {
        "schema_version": "mnemos.artifact_catalog.v1",
        "entries": [],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    artifact_catalog_hash = sha256_json(artifact_catalog)
    authority_catalog_hash = sha256_json(authority_catalog)
    access_control = make_cognitive_access_envelope(
        owner_principal_id="audit:agent",
        owner_agent="audit-agent",
        scope_type="project",
        scope_id="mnemos",
        project="mnemos",
        session_id=event_id,
        purposes=("cognitive_state_read",),
        consent_provenance_refs=(authority_catalog_hash,),
        sensitivity="sensitive",
        retention_policy="cognitive_state",
        source_acl_lineage=(authority_catalog_hash,),
    )
    context_hash = sha256_json(
        {
            "schema_version": "mnemos.cognition_extraction_context.v1",
            "source_agent": "audit-agent",
            "source_session_id": event_id,
            "source_event_ids": [event_id],
            "raw_completeness": "full",
            "loss_contract": "lossless-visible-v1",
            "source_spans": [source_span],
            "artifact_catalog_hash": artifact_catalog_hash,
            "source_authority_catalog_hash": authority_catalog_hash,
            "acl": "local_user",
            "access_control": access_control,
            "purpose": "cognition_distillation",
            "retention_policy": "cognitive_state",
        }
    )
    resolved_evidence = {
        "source_event_id": event_id,
        "source_authority_id": authority_id,
        "quote": "synthetic audit evidence",
        "authority_role": "user",
        "authority_span_start": 0,
        "authority_span_end": 64,
        "authority_span_status": "exact",
        "authority_content_sha256": content_hash,
        "authority_source_revision_sha256": content_hash,
    }
    episode_fields = {
        field_name: [
            {
                "entry_id": f"audit-entry:{field_name}",
                "status": "known",
                "value": f"audit cognition field: {field_name}",
                "evidence_refs": [resolved_evidence],
                "claim_ids": ["audit-claim"],
            }
        ]
        for field_name in COGNITION_EPISODE_FIELDS
    }
    claims = [
        {
            "claim_id": "audit-claim",
            "claim_text": "The canonical state audit preserves the complete claim catalog.",
            "claim_type": "technical_fact",
            "scope": {
                "domain": "cognitive-state-audit",
                "applies_to": ["mnemos"],
                "not_applies_to": [],
            },
            "evidence": [resolved_evidence],
            "relation_to_existing": {
                "type": "new",
                "target_pages": [],
                "delta_text": "",
                "reason": "synthetic audit fixture",
            },
            "recommended_action": "create_page",
            "confidence": 1.0,
        }
    ]
    behavior_intent = {
        "content_source": "native_dialogue",
        "user_intent_signal": "sharing_information",
        "intent_hypothesis": "The audit must prove lossless canonical cognition.",
        "intent_evidence": [resolved_evidence],
        "intent_verification_events": [],
        "intent_confidence": 1.0,
        "intent_status": "verified",
        "behavior_summary": "Verify the canonical cognition state contract.",
    }
    return CognitiveStateRevision.create(
        object_type="cognition_episode",
        object_id=f"episode:{event_id}",
        source_event_id=event_id,
        source_revision_id=source_revision_id,
        source_content_hash=content_hash,
        scope_type="project",
        scope_id="mnemos",
        evidence_refs=(evidence_ref,),
        payload={
            "schema_version": COGNITION_EPISODE_SCHEMA_VERSION,
            "cognition_context_hash": context_hash,
            "input_spec_hash": "sha256:audit-input",
            "extraction_output_hash": content_hash,
            "source_agent": "audit-agent",
            "source_session_id": event_id,
            "source_event_ids": [event_id],
            "raw_completeness": "full",
            "loss_contract": "lossless-visible-v1",
            "source_spans": [source_span],
            "artifact_catalog_hash": artifact_catalog_hash,
            "source_authority_catalog_hash": authority_catalog_hash,
            "source_authority_catalog": authority_catalog,
            "artifact_catalog": artifact_catalog,
            "acl": "local_user",
            "access_control": access_control,
            "purpose": "cognition_distillation",
            "retention_policy": "cognitive_state",
            "claims": claims,
            "claim_catalog_hash": sha256_json(claims),
            "user_behavior_intent": behavior_intent,
            **episode_fields,
        },
        created_at="2026-07-16T02:00:00+00:00",
    )


def _event(event_id: str, content_hash: str) -> CognitiveDataEvent:
    return CognitiveDataEvent(
        event_id=event_id,
        source_id=f"raw-event:{event_id}",
        asset_id=f"raw-asset:{event_id}",
        source_kind="cognitive_state",
        source_uri=f"raw://{event_id}",
        content_hash=content_hash,
        canonical_subject=f"episode:{event_id}",
        data_type="cognition_episode",
        producer="cognitive_state_store",
        intended_consumers=("wiki", "cognitive_graph"),
        privacy_level="private",
        confidence=1.0,
        evidence_refs=(f"raw-event:{event_id}#0:64",),
        dedupe_key=f"audit:{event_id}",
        created_at="2026-07-16T02:00:00+00:00",
    )


def _commands(revision_id: str) -> tuple[LocalConsumerCommand, ...]:
    return tuple(
        LocalConsumerCommand.create(
            revision_id=revision_id,
            consumer_id=consumer,
            # COG-047's generic state-store audit closes synthetic commands
            # with the generic receipt API.  Real cognition-episode projection
            # commands are deliberately reserved for the COG-030 target oracle
            # and are exercised by audit_cognitive_event_dispatch.py.
            command_type="audit_cognitive_state_projection",
            payload={"revision_id": revision_id, "target": consumer},
            created_at="2026-07-16T02:00:00+00:00",
        )
        for consumer in ("wiki", "cognitive_graph")
    )


def _counts(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        return {
            "semantic": int(
                conn.execute("SELECT COUNT(*) FROM cognitive_state_revisions").fetchone()[0]
            ),
            "envelope": int(
                conn.execute("SELECT COUNT(*) FROM cognitive_data_events").fetchone()[0]
            ),
            "outbox": int(
                conn.execute("SELECT COUNT(*) FROM cognitive_state_outbox").fetchone()[0]
            ),
            "receipt": int(
                conn.execute("SELECT COUNT(*) FROM cognitive_state_effect_receipts").fetchone()[0]
            ),
        }


def _synthetic_state_audit() -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mnemos-cog047-audit-") as temp_dir:
        root = Path(temp_dir)
        missing = root / "constructor" / "producer_consumer_ledger.db"
        CognitiveStateStore(missing)
        constructor_read_only = not missing.exists() and not missing.parent.exists()
        missing_action = root / "action-constructor" / "action_ledger.db"
        ActionLedger(missing_action)
        action_constructor_read_only = (
            not missing_action.exists() and not missing_action.parent.exists()
        )

        rollback_counts: dict[str, dict[str, int]] = {}
        for stage in ("after_revision", "after_event", "after_outbox"):
            db_path = root / stage / "producer_consumer_ledger.db"
            initialize_cognitive_state_schema(db_path)
            store = CognitiveStateStore(db_path)
            event_id = f"cogevent-audit-{stage}"
            content_hash = f"sha256:audit-{stage}"
            revision = _episode(event_id, content_hash)

            def failpoint(current: str, expected: str = stage) -> None:
                if current == expected:
                    raise sqlite3.OperationalError(f"injected:{expected}")

            try:
                store.unit_of_work().commit(
                    revisions=(revision,),
                    event=_event(event_id, content_hash),
                    commands=_commands(revision.revision_id),
                    failpoint=failpoint,
                )
            except sqlite3.OperationalError:
                pass
            rollback_counts[stage] = _counts(db_path)

        db_path = root / "happy" / "producer_consumer_ledger.db"
        initialize_cognitive_state_schema(db_path)
        store = CognitiveStateStore(db_path)
        event_id = "cogevent-audit-happy"
        content_hash = "sha256:audit-happy"
        revision = _episode(event_id, content_hash)
        commands = _commands(revision.revision_id)
        commit = store.unit_of_work().commit(
            revisions=(revision,),
            event=_event(event_id, content_hash),
            commands=commands,
        )
        store.record_effect_receipt(
            commands[0].command_id,
            status="committed",
            target_effect_id="wiki-page:audit-happy",
            before_hash="sha256:absent",
            after_hash="sha256:wiki-v1",
            evidence_refs=("wiki-journal:audit-happy",),
        )
        store.record_effect_receipt(
            commands[1].command_id,
            status="intentional_skip",
            target_effect_id="cognitive-graph:audit-happy",
            evidence_refs=("projection-policy:not-applicable",),
        )
        metrics = store.integrity_report()
        rebuilt = store.rebuild_current_state()
        final_counts = _counts(db_path)

        action_db = root / "action" / "action_ledger.db"
        initialize_action_ledger_schema(action_db)
        action = make_quality_gate_observation(
            actor="cog047-audit",
            target="synthetic-action-ledger",
            evidence_refs=("audit:cog047",),
            observation_id="obsact-cog047-audit",
        )
        ledger = ActionLedger(action_db)
        ledger.record_observation(action)
        ledger.record_observation(action)
        immutable_results: list[bool] = []
        for statement in (
            "UPDATE action_ledger SET status='failed' WHERE action_id='obsact-cog047-audit'",
            "DELETE FROM action_ledger WHERE action_id='obsact-cog047-audit'",
        ):
            try:
                with sqlite3.connect(action_db) as conn:
                    conn.execute(statement)
            except sqlite3.IntegrityError:
                immutable_results.append(True)
            else:
                immutable_results.append(False)

    rollback_clean = all(
        values == {"semantic": 0, "envelope": 0, "outbox": 0, "receipt": 0}
        for values in rollback_counts.values()
    )
    if not constructor_read_only:
        errors.append("CognitiveStateStore constructor created filesystem state")
    if not action_constructor_read_only:
        errors.append("ActionLedger constructor created filesystem state")
    if not rollback_clean:
        errors.append("a transaction failpoint left partial semantic/envelope/outbox state")
    for key in ZERO_METRICS:
        if int(metrics.get(key, -1)) != 0:
            errors.append(f"synthetic acceptance metric {key} is not zero")
    if int(metrics.get("canonical_state_owner_count", 0)) != 1:
        errors.append("synthetic canonical state owner count is not one")
    if not rebuilt["projection_hash_matches"]:
        errors.append("current state cannot be rebuilt from immutable revisions")
    if final_counts != {"semantic": 1, "envelope": 1, "outbox": 2, "receipt": 2}:
        errors.append("synthetic semantic/envelope/outbox/receipt denominators drifted")
    if immutable_results != [True, True]:
        errors.append("ActionLedger permits UPDATE or DELETE")
    return (
        {
            "constructor_read_only": constructor_read_only,
            "action_constructor_read_only": action_constructor_read_only,
            "rollback_counts": rollback_counts,
            "rollback_clean": rollback_clean,
            "commit_status": commit.status,
            "denominators": final_counts,
            "metrics": metrics,
            "rebuild": rebuilt,
            "action_update_delete_rejected": immutable_results == [True, True],
        },
        errors,
    )


def _live_state(db_path: Path) -> tuple[dict[str, Any], list[str]]:
    if not db_path.is_file():
        return ({"classification": "not_initialized", "ok": True}, [])
    errors: list[str] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        state = inspect_cognitive_state_schema(conn)
    payload = state.as_dict()
    if not state.ok:
        errors.append("live producer_consumer_ledger.db requires explicit reconciliation")
        return payload, errors
    metrics = CognitiveStateStore(db_path).integrity_report()
    payload["metrics"] = metrics
    for key in ZERO_METRICS:
        if int(metrics.get(key, -1)) != 0:
            errors.append(f"live acceptance metric {key} is not zero")
    return payload, errors


def _live_action_state(db_path: Path) -> tuple[dict[str, Any], list[str]]:
    if not db_path.is_file():
        return ({"classification": "not_initialized", "ok": True}, [])
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        state = inspect_action_ledger_schema(conn)
    errors = [] if state.ok else ["live action_ledger.db requires explicit reconciliation"]
    return state.as_dict(), errors


def build_report(
    *,
    state_db_path: Path,
    action_db_path: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    repo_root = root or ROOT
    errors: list[str] = []
    state_owners = _ddl_owners(repo_root, "cognitive_state_revisions")
    envelope_owners = _ddl_owners(repo_root, "cognitive_data_events")
    action_owners = _ddl_owners(repo_root, "action_ledger")
    expected_state_owner = ["core/cognitive/state_schema_ddl.py"]
    expected_action_owner = ["core/ops/action_ledger_schema.py"]
    if state_owners != expected_state_owner:
        errors.append(f"cognitive state DDL owner drift: {state_owners}")
    if envelope_owners != expected_state_owner:
        errors.append(f"cognitive envelope DDL owner drift: {envelope_owners}")
    if action_owners != expected_action_owner:
        errors.append(f"action ledger DDL owner drift: {action_owners}")
    replacements = _forbidden_action_replaces(repo_root)
    if replacements:
        errors.append(f"ActionLedger still has INSERT OR REPLACE writers: {replacements}")
    synthetic, synthetic_errors = _synthetic_state_audit()
    live_state, live_errors = _live_state(state_db_path)
    live_action, live_action_errors = _live_action_state(action_db_path)
    errors.extend(synthetic_errors)
    errors.extend(live_errors)
    errors.extend(live_action_errors)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "state_schema_version": STATE_SCHEMA_VERSION,
        "action_schema_version": ACTION_LEDGER_SCHEMA_VERSION,
        "ok": not errors,
        "ddl_owners": {
            "cognitive_state": state_owners,
            "cognitive_envelope": envelope_owners,
            "action_ledger": action_owners,
        },
        "forbidden_action_replace_writers": replacements,
        "synthetic": synthetic,
        "live": {
            "state_db_path": str(state_db_path),
            "state": live_state,
            "action_db_path": str(action_db_path),
            "action": live_action,
        },
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--action-db-path", type=Path)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.db_path is not None and args.action_db_path is not None:
        state_db_path = args.db_path
        action_db_path = args.action_db_path
    else:
        config = get_config()
        state_db_path = args.db_path or Path(config.database_dir) / "producer_consumer_ledger.db"
        action_db_path = args.action_db_path or Path(config.database_dir) / "action_ledger.db"
    try:
        report = build_report(
            state_db_path=state_db_path,
            action_db_path=action_db_path,
        )
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        report = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "ok": False,
            "errors": [str(exc)],
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif report["ok"]:
        print("Cognitive state store audit passed")
    else:
        print("Cognitive state store audit failed")
        for error in report["errors"]:
            print(f"- {error}")
    return 0 if report["ok"] or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
