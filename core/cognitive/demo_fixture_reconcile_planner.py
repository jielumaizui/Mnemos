"""Read-only planner for exact deterministic demo-fixture leaks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, cast

from core.cognitive.demo_fixture_reconcile_contracts import (
    DemoFixtureActionSkip,
    DemoFixtureCommandClosure,
    DemoFixtureEpisodeRetirement,
    DemoFixtureReconciliationPaths,
    DemoFixtureReconciliationPlan,
    FIXTURE_BEHAVIOR_SUMMARY,
    FIXTURE_CLAIM_ID,
    FIXTURE_CLAIM_TEXT,
    FIXTURE_INTENT_REASON,
    FIXTURE_QUOTE,
    FIXTURE_SESSION_ID,
    FIXTURE_TITLE,
    finalize_plan_hashes,
)
from core.cognition_episode_contract import (
    COGNITION_EPISODE_SCHEMA_VERSION,
    LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
)
from core.cognitive.state_contract import sha256_json


_EXPECTED_EPISODE_CONSUMERS = ("cognitive_graph", "knowledge_graph", "wiki")
_QUALITY_CONSUMER = "core/hephaestus/distillation_engine.py"


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _row_hash(row: sqlite3.Row) -> str:
    return sha256_json({str(key): row[key] for key in row.keys()})


def _json_mapping(value: Any) -> dict[str, Any]:
    decoded = json.loads(str(value or "{}"))
    if not isinstance(decoded, Mapping):
        raise ValueError("expected JSON object")
    return dict(decoded)


def _json_strings(value: Any) -> tuple[str, ...]:
    decoded = json.loads(str(value or "[]"))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError("expected JSON string array")
    return tuple(str(item) for item in decoded)


def _fixture_source_hash(path: Path) -> str:
    content = path.read_bytes()
    for marker in (
        b"class DemoConfig",
        FIXTURE_SESSION_ID.encode("utf-8"),
        FIXTURE_TITLE.encode("utf-8"),
        FIXTURE_QUOTE.encode("utf-8"),
    ):
        if marker not in content:
            raise ValueError("tracked demo fixture contract marker is missing")
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _known_fixture_entry(payload: Mapping[str, Any], field: str, expected: str) -> bool:
    values = payload.get(field)
    if not isinstance(values, list):
        return False
    for value in values:
        if not isinstance(value, Mapping):
            continue
        refs = value.get("evidence_refs")
        if (
            value.get("status") == "known"
            and value.get("value") == expected
            and value.get("claim_ids") == [FIXTURE_CLAIM_ID]
            and isinstance(refs, list)
            and any(
                isinstance(ref, Mapping) and ref.get("quote") == FIXTURE_QUOTE
                for ref in refs
            )
        ):
            return True
    return False


def _fixture_evidence_matches(
    value: Any,
    *,
    source_revision_ids: tuple[str, ...],
    authority_ids: set[str],
    reason: str | None = None,
) -> bool:
    if not isinstance(value, list) or len(value) != 1:
        return False
    evidence = value[0]
    if not isinstance(evidence, Mapping):
        return False
    return bool(
        evidence.get("quote") == FIXTURE_QUOTE
        and str(evidence.get("source_event_id") or "") in source_revision_ids
        and str(evidence.get("source_authority_id") or "") in authority_ids
        and evidence.get("source_authority") == "explicit_user"
        and evidence.get("authority_role") == "user"
        and evidence.get("authority_span_status") == "exact"
        and evidence.get("authority_allows_cognitive_update") is True
        and (reason is None or evidence.get("reason") == reason)
    )


def _v2_fixture_metadata_matches(
    payload: Mapping[str, Any],
    *,
    source_revision_ids: tuple[str, ...],
    authority_ids: set[str],
) -> bool:
    claims = payload.get("claims")
    if not isinstance(claims, list) or len(claims) != 1:
        return False
    claim = claims[0]
    if not isinstance(claim, Mapping):
        return False
    if (
        claim.get("claim_id") != FIXTURE_CLAIM_ID
        or claim.get("claim_text") != FIXTURE_CLAIM_TEXT
        or claim.get("claim_type") != "procedure"
        or claim.get("scope") != {"domain": "backend"}
        or claim.get("relation_to_existing")
        != {
            "delta_text": "",
            "reason": "demo vault 中没有同等页面。",
            "target_pages": [],
            "type": "new",
        }
        or claim.get("recommended_action") != "create_page"
        or claim.get("cognitive_actions")
        != ["create_observation", "propose_methodology"]
        or claim.get("confidence") != 0.95
        or not _fixture_evidence_matches(
            claim.get("evidence"),
            source_revision_ids=source_revision_ids,
            authority_ids=authority_ids,
        )
        or payload.get("claim_catalog_hash") != sha256_json(claims)
    ):
        return False

    behavior = payload.get("user_behavior_intent")
    return bool(
        isinstance(behavior, Mapping)
        and behavior.get("content_source") == "native_dialogue"
        and behavior.get("user_intent_signal") == "seeking_judgment"
        and behavior.get("intent_hypothesis") == "seeking_judgment"
        and behavior.get("intent_verification_events") == []
        and behavior.get("intent_confidence") == 0.75
        and behavior.get("intent_status") == "unverified"
        and behavior.get("behavior_summary") == FIXTURE_BEHAVIOR_SUMMARY
        and _fixture_evidence_matches(
            behavior.get("intent_evidence"),
            source_revision_ids=source_revision_ids,
            authority_ids=authority_ids,
            reason=FIXTURE_INTENT_REASON,
        )
    )


def _validate_episode_payload(row: sqlite3.Row) -> tuple[dict[str, Any], tuple[str, ...]]:
    payload = _json_mapping(row["payload_json"])
    if sha256_json(payload) != str(row["payload_hash"]):
        raise ValueError("cognition episode immutable payload hash mismatch")
    source_revision_ids = tuple(str(value) for value in payload.get("source_event_ids", ()))
    if (
        payload.get("schema_version")
        not in {
            LEGACY_COGNITION_EPISODE_SCHEMA_VERSION,
            COGNITION_EPISODE_SCHEMA_VERSION,
        }
        or payload.get("source_session_id") != FIXTURE_SESSION_ID
        or payload.get("source_agent") != "claude"
        or payload.get("extraction_output_hash") != row["source_content_hash"]
        or len(source_revision_ids) != 2
        or any(not value.startswith("rawrev-") for value in source_revision_ids)
        or str(row["source_revision_id"])
        != "distill-input:" + str(payload.get("input_spec_hash") or "").removeprefix("sha256:")
        or not _known_fixture_entry(
            payload,
            "situation",
            "用户正在排查 asyncio.gather 并发请求的 TimeoutError。",
        )
        or not _known_fixture_entry(
            payload,
            "facts",
            "用户确认将采用 return_exceptions=True 和单任务超时包装。",
        )
        or not _known_fixture_entry(
            payload,
            "scope",
            "该结论适用于当前 asyncio 并发请求改造。",
        )
    ):
        raise ValueError("cognition episode does not match the tracked demo fixture")
    authority = payload.get("source_authority_catalog")
    entries = authority.get("entries") if isinstance(authority, Mapping) else None
    authority_sources = {
        str(entry.get("source_event_id") or "")
        for entry in entries or ()
        if isinstance(entry, Mapping)
    }
    authority_ids = {
        str(entry.get("source_authority_id") or "")
        for entry in entries or ()
        if isinstance(entry, Mapping)
    }
    span_sources = {
        str(entry.get("revision_id") or "")
        for entry in payload.get("source_spans", ())
        if isinstance(entry, Mapping)
    }
    if set(source_revision_ids) != authority_sources or set(source_revision_ids) != span_sources:
        raise ValueError("demo cognition source lineage is incomplete")
    if payload.get("schema_version") == COGNITION_EPISODE_SCHEMA_VERSION and not (
        _v2_fixture_metadata_matches(
            payload,
            source_revision_ids=source_revision_ids,
            authority_ids=authority_ids,
        )
    ):
        raise ValueError("cognition episode does not match the tracked demo fixture")
    return payload, source_revision_ids


def _sources_are_absent(raw_path: Path, source_revision_ids: tuple[str, ...]) -> bool:
    with _connect_read_only(raw_path) as conn:
        return all(
            conn.execute(
                "SELECT 1 FROM raw_turn_revisions WHERE revision_id=?",
                (revision_id,),
            ).fetchone()
            is None
            for revision_id in source_revision_ids
        )


def _episode_commands(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> tuple[DemoFixtureCommandClosure, ...]:
    commands = conn.execute(
        "SELECT * FROM cognitive_state_outbox WHERE revision_id=? "
        "ORDER BY consumer_id, command_id",
        (str(row["revision_id"]),),
    ).fetchall()
    if tuple(str(value["consumer_id"]) for value in commands) != _EXPECTED_EPISODE_CONSUMERS:
        raise ValueError("demo cognition outbox consumer set is not exact")
    closures: list[DemoFixtureCommandClosure] = []
    for command in commands:
        payload = _json_mapping(command["payload_json"])
        if (
            command["command_type"] != "project_cognition_episode"
            or payload
            != {
                "object_id": str(row["object_id"]),
                "object_type": "cognition_episode",
                "primary_revision_id": str(row["revision_id"]),
            }
            or sha256_json(payload) != str(command["payload_hash"])
            or conn.execute(
                "SELECT 1 FROM cognitive_state_effect_receipts WHERE command_id=?",
                (str(command["command_id"]),),
            ).fetchone()
            is not None
        ):
            raise ValueError("demo cognition command is not an exact pending projection")
        closures.append(
            DemoFixtureCommandClosure(
                command_id=str(command["command_id"]),
                consumer_id=str(command["consumer_id"]),
                payload_hash=str(command["payload_hash"]),
            )
        )
    return tuple(closures)


def _episode_event(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    event = conn.execute(
        "SELECT * FROM cognitive_data_events WHERE event_id=?",
        (str(row["source_event_id"]),),
    ).fetchone()
    if event is None:
        raise ValueError("demo cognition envelope is missing")
    metadata = _json_mapping(event["metadata"])
    if (
        event["source_id"] != "claude"
        or event["source_kind"] != "distillation_extraction"
        or event["source_uri"] != "mnemos://distillation/" + str(row["object_id"])
        or event["content_hash"] != row["source_content_hash"]
        or event["canonical_subject"] != "cognition_episode:" + str(row["object_id"])
        or event["data_type"] != "cognition_episode"
        or event["producer"] != "cognitive_state_store"
        or sorted(_json_strings(event["intended_consumers"]))
        != list(_EXPECTED_EPISODE_CONSUMERS)
        or metadata.get("revision_ids") != [str(row["revision_id"])]
    ):
        raise ValueError("demo cognition envelope binding is not exact")
    return cast(sqlite3.Row, event)


def _episode_retirements(
    paths: DemoFixtureReconciliationPaths,
) -> tuple[list[DemoFixtureEpisodeRetirement], list[Mapping[str, str]]]:
    retirements: list[DemoFixtureEpisodeRetirement] = []
    blocked: list[Mapping[str, str]] = []
    with _connect_read_only(paths.state_path) as conn:
        rows = conn.execute(
            """
            SELECT r.*
            FROM cognitive_state_heads AS h
            JOIN cognitive_state_revisions AS r ON r.revision_id=h.revision_id
            WHERE h.object_type='cognition_episode'
              AND r.scope_type='session' AND r.scope_id=?
            ORDER BY r.object_id
            """,
            (FIXTURE_SESSION_ID,),
        ).fetchall()
        for row in rows:
            try:
                _payload, source_revision_ids = _validate_episode_payload(row)
                if not _sources_are_absent(paths.raw_path, source_revision_ids):
                    raise ValueError("demo Raw sources still exist; retirement is not allowed")
                event = _episode_event(conn, row)
                commands = _episode_commands(conn, row)
                retirements.append(
                    DemoFixtureEpisodeRetirement(
                        object_id=str(row["object_id"]),
                        revision_id=str(row["revision_id"]),
                        payload_hash=str(row["payload_hash"]),
                        revision_row_hash=_row_hash(row),
                        event_id=str(row["source_event_id"]),
                        event_row_hash=_row_hash(event),
                        source_revision_ids=source_revision_ids,
                        commands=commands,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                blocked.append(
                    {
                        "object_id": str(row["object_id"]),
                        "reason": str(exc),
                    }
                )
    return retirements, blocked


def _action_skips(
    paths: DemoFixtureReconciliationPaths,
) -> tuple[list[DemoFixtureActionSkip], list[Mapping[str, str]]]:
    skips: list[DemoFixtureActionSkip] = []
    blocked: list[Mapping[str, str]] = []
    with _connect_read_only(paths.action_path) as actions, _connect_read_only(
        paths.state_path
    ) as state:
        pending_events = state.execute(
            """
            SELECT e.*
            FROM runtime_flow_events AS e
            WHERE e.flow_id='distill_quality_to_write_admission'
              AND EXISTS (
                  SELECT 1 FROM json_each(e.intended_consumers)
                  WHERE value=?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM runtime_flow_receipts AS r
                  WHERE r.production_event_id=e.event_id
                    AND r.consumer_id=?
                    AND r.status IN ('consumed', 'dead_letter', 'skipped')
              )
            ORDER BY e.item_id, e.event_id
            """,
            (_QUALITY_CONSUMER, _QUALITY_CONSUMER),
        ).fetchall()
        candidates: dict[str, tuple[sqlite3.Row, list[sqlite3.Row]]] = {}
        for event in pending_events:
            action_id = str(event["item_id"])
            row = actions.execute(
                "SELECT * FROM action_ledger WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if row is None or row["target"] != f"distill:{FIXTURE_SESSION_ID}:fragment:0":
                continue
            existing = candidates.get(action_id)
            if existing is None:
                candidates[action_id] = (row, [event])
            else:
                existing[1].append(event)
        for action_id, (row, events) in sorted(candidates.items()):
            if len(events) != 1:
                blocked.append(
                    {"action_id": action_id, "reason": "pending demo quality event is not unique"}
                )
                continue
            event = events[0]
            receipts = state.execute(
                "SELECT consumer_id,status FROM runtime_flow_receipts "
                "WHERE production_event_id=? ORDER BY consumer_id,status",
                (str(event["event_id"]),),
            ).fetchall()
            if receipts:
                blocked.append(
                    {"action_id": action_id, "reason": "demo quality receipt is unexpected"}
                )
                continue
            try:
                verification = _json_mapping(row["verification_json"])
                metadata = _json_mapping(event["metadata"])
                if (
                    row["actor"] != "core.hephaestus.distillation_engine"
                    or row["action_type"] != "quality_gate"
                    or row["status"] != "verified"
                    or verification.get("session_id") != FIXTURE_SESSION_ID
                    or verification.get("fragment_index") != 0
                    or verification.get("title") != FIXTURE_TITLE
                    or verification.get("final_disposition") != "accept"
                    or event["direction"] != "produced"
                    or event["source"] != "core/hephaestus/distillation_quality.py"
                    or _json_strings(event["intended_consumers"]) != (_QUALITY_CONSUMER,)
                    or metadata != {"transition": "quality_gate_decided"}
                ):
                    raise ValueError("quality action does not match the tracked demo fixture")
                skips.append(
                    DemoFixtureActionSkip(
                        action_id=action_id,
                        action_row_hash=_row_hash(row),
                        production_event_id=str(event["event_id"]),
                        runtime_event_row_hash=_row_hash(event),
                        consumer_id=_QUALITY_CONSUMER,
                    )
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                blocked.append({"action_id": action_id, "reason": str(exc)})
    return skips, blocked


def build_demo_fixture_reconciliation_plan(
    paths: DemoFixtureReconciliationPaths,
) -> DemoFixtureReconciliationPlan:
    """Bind exact leaked objects to the tracked deterministic demo fixture."""

    for path in (
        paths.state_path,
        paths.raw_path,
        paths.action_path,
        paths.fixture_source_path,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    fixture_source_hash = _fixture_source_hash(paths.fixture_source_path)
    episodes, episode_blocked = _episode_retirements(paths)
    actions, action_blocked = _action_skips(paths)
    blocked = [*episode_blocked, *action_blocked]
    object_manifest_hash, inventory_hash = finalize_plan_hashes(
        fixture_source_hash=fixture_source_hash,
        episode_manifests=[value.manifest() for value in episodes],
        action_manifests=[value.manifest() for value in actions],
        blocked=blocked,
    )
    return DemoFixtureReconciliationPlan(
        paths=paths,
        fixture_source_hash=fixture_source_hash,
        episodes=tuple(episodes),
        actions=tuple(actions),
        blocked=tuple(blocked),
        object_manifest_hash=object_manifest_hash,
        inventory_hash=inventory_hash,
    )


__all__ = ["build_demo_fixture_reconciliation_plan"]
