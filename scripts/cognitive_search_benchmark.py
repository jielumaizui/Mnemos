"""Hermetic fixture builder and evaluator for typed cognitive retrieval."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
import re
import sqlite3
from types import SimpleNamespace
import time
from typing import Any, Mapping, Sequence

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.context_search import ContextAwareSearch
from core.cognition_episode_contract import (
    COGNITION_EPISODE_FIELDS,
    COGNITION_EPISODE_SCHEMA_VERSION,
)
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.state_contract import (
    CognitiveStateRevision,
    LocalConsumerCommand,
    sha256_json,
)
from core.cognitive.state_schema import initialize_cognitive_state_schema
from core.cognitive.state_store import CognitiveStateStore
from core.ops.cognitive_data_contract import CognitiveDataEvent

BENCHMARK_SCHEMA_VERSION = "mnemos.cognitive_search_benchmark.v1"
AUDIT_SCHEMA_VERSION = "mnemos.cognitive_search_audit.v1"
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "fixtures"
    / "cognitive_search_benchmark_v1.json"
)
_CREATED_AT = "2026-07-20T00:00:00+00:00"
FROZEN_FIXTURE_CONTRACT_SHA256 = (
    "sha256:9a9eeea40f3bb8f5f58fcbd2561c2a330ebcfba39d691f2824967f8b1d30ded0"
)


@dataclass(frozen=True)
class BenchmarkEnvironment:
    wiki_dir: Path
    state_db: Path
    cognitive_graph_db: Path
    evidence_graph_db: Path
    principal: PrincipalEnvelope
    narrowing: AccessNarrowing
    expected_object_ids: Mapping[str, str]
    current_revision_ids: Mapping[str, str]
    superseded_revision_ids: Mapping[str, str]


_REQUIRED_CHANNELS = {
    "wiki_page",
    "cognitive_state",
    "cognitive_graph",
    "evidence_graph",
}
_REQUIRED_TAGS = {
    "historical_decision",
    "preference_scope",
    "root_cause",
    "correction",
    "graph_only",
    "tail_fields",
    "current_superseded_belief",
    "acl_refill",
}


def fixture_contract_hash(payload: Mapping[str, Any]) -> str:
    canonical = dict(payload)
    canonical.pop("fixture_contract_sha256", None)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("cognitive search benchmark must be an object")
    if payload.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("cognitive search benchmark schema is unsupported")
    cases = payload.get("cases")
    negatives = payload.get("negative_cases")
    if not isinstance(cases, list) or len(cases) < 30:
        raise ValueError("cognitive search benchmark requires at least 30 positive cases")
    if not isinstance(negatives, list) or not negatives:
        raise ValueError("cognitive search benchmark requires ACL negative cases")
    expected_hash = fixture_contract_hash(payload)
    if payload.get("fixture_contract_sha256") != expected_hash:
        raise ValueError("cognitive search benchmark fixture hash mismatch")
    if expected_hash != FROZEN_FIXTURE_CONTRACT_SHA256:
        raise ValueError("cognitive search benchmark fixture violates frozen external pin")
    channels = {str(case.get("channel") or "") for case in cases}
    negative_channels = {str(case.get("channel") or "") for case in negatives}
    if not _REQUIRED_CHANNELS.issubset(channels):
        raise ValueError("benchmark must cover Wiki, state, cognitive graph, and evidence graph")
    if not _REQUIRED_CHANNELS.issubset(negative_channels):
        raise ValueError("benchmark ACL negatives must cover every retrieval channel")
    holdout_count = sum(1 for case in cases if case.get("split") == "holdout")
    if holdout_count < 24:
        raise ValueError("cognitive search benchmark requires at least 24 holdout cases")
    if any(case.get("split") not in {"calibration", "holdout"} for case in cases):
        raise ValueError("benchmark split must be calibration or holdout")
    tags = {str(tag) for case in cases for tag in case.get("tags", []) if str(tag).strip()}
    missing_tags = sorted(_REQUIRED_TAGS - tags)
    if missing_tags:
        raise ValueError(f"benchmark category coverage missing: {','.join(missing_tags)}")
    if not any(str(case.get("scope_id") or "") == "other-project" for case in negatives):
        raise ValueError("benchmark requires cross-project ACL negatives")
    case_ids = [str(case.get("case_id") or "") for case in [*cases, *negatives]]
    probes = [str(case.get("probe") or "") for case in [*cases, *negatives]]
    queries = [str(case.get("query") or "") for case in [*cases, *negatives]]
    if any(not value for value in (*case_ids, *probes, *queries)):
        raise ValueError("benchmark case identity, probe, and query are required")
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("benchmark case IDs must be unique")
    if len(probes) != len(set(probes)):
        raise ValueError("benchmark leakage probes must be unique")
    if len(queries) != len(set(queries)):
        raise ValueError("benchmark queries must be unique")
    return payload


def benchmark_principal() -> PrincipalEnvelope:
    return PrincipalEnvelope(
        principal_id="principal:cognitive-search-benchmark",
        agent="codex",
        host_kind="benchmark",
        capability_id="cognitive-search-benchmark",
        capabilities=frozenset({"memory_read", "memory_write"}),
        allowed_projects=frozenset({"mnemos"}),
        allowed_source_agents=frozenset({"codex"}),
    )


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _access(
    *,
    case_id: str,
    purpose: str | Sequence[str],
    project: str = "mnemos",
    owner_principal_id: str = "principal:cognitive-search-benchmark",
) -> dict[str, Any]:
    lineage = _digest(f"benchmark-acl:{case_id}:{project}:{owner_principal_id}")
    purposes = (purpose,) if isinstance(purpose, str) else tuple(str(value) for value in purpose)
    return dict(
        make_cognitive_access_envelope(
            owner_principal_id=owner_principal_id,
            owner_agent="codex",
            scope_type="project",
            scope_id=project,
            project=project,
            purposes=purposes,
            consent_provenance_refs=(f"benchmark:{case_id}",),
            sensitivity="sensitive",
            retention_policy="cognitive_search_benchmark",
            source_acl_lineage=(lineage,),
            visibility="private",
        )
    )


def _episode_revision(
    case: Mapping[str, Any],
    *,
    event_id: str,
    object_id: str,
    project: str = "mnemos",
    owner_principal_id: str = "principal:cognitive-search-benchmark",
    supersedes_revision_id: str = "",
    read_purposes: Sequence[str] = ("cognitive_state_read",),
    sparse_episode: bool = False,
) -> CognitiveStateRevision:
    case_id = str(case["case_id"])
    text = str(case.get("text") or case["query"])
    target_field = str(case.get("field") or "claim_text")
    source_revision_id = f"raw-benchmark-{case_id}"
    source_hash = _digest(text)
    authority_id = f"source-authority:{case_id}"
    span_end = len(text)
    source_span = {
        "source_authority_id": authority_id,
        "revision_id": source_revision_id,
        "role": "user",
        "span_start": 0,
        "span_end": span_end,
        "span_status": "exact",
        "content_sha256": source_hash,
        "source_revision_sha256": source_hash,
    }
    authority_entry = {
        "source_authority_id": authority_id,
        "source_authority": "explicit_user",
        "source_event_id": source_revision_id,
        "role": "user",
        "purpose": "user_instruction",
        "content_sha256": source_hash,
        "span_start": 0,
        "span_end": span_end,
        "span_status": "exact",
        "source_revision_sha256": source_hash,
        "artifact_ref_id": "",
        "allows_cognitive_update": True,
    }
    authority_catalog = {
        "schema_version": "mnemos.source_authority_catalog.v1",
        "entries": [authority_entry],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    artifact_catalog = {
        "schema_version": "mnemos.artifact_catalog.v1",
        "entries": [],
        "rejected_count": 0,
        "rejection_codes": [],
    }
    authority_catalog_hash = sha256_json(authority_catalog)
    artifact_catalog_hash = sha256_json(artifact_catalog)
    access = _access(
        case_id=case_id,
        purpose=read_purposes,
        project=project,
        owner_principal_id=owner_principal_id,
    )
    context_hash = sha256_json(
        {
            "schema_version": "mnemos.cognition_extraction_context.v1",
            "source_agent": "codex",
            "source_session_id": case_id,
            "source_event_ids": [source_revision_id],
            "raw_completeness": "full",
            "loss_contract": "lossless-visible-v1",
            "source_spans": [source_span],
            "artifact_catalog_hash": artifact_catalog_hash,
            "source_authority_catalog_hash": authority_catalog_hash,
            "acl": "local_user",
            "access_control": access,
            "purpose": "cognition_distillation",
            "retention_policy": "cognitive_search_benchmark",
        }
    )
    evidence = {
        "source_event_id": source_revision_id,
        "source_authority_id": authority_id,
        "quote": text,
        "authority_role": "user",
        "authority_span_start": 0,
        "authority_span_end": span_end,
        "authority_span_status": "exact",
        "authority_content_sha256": source_hash,
        "authority_source_revision_sha256": source_hash,
    }
    claim_text = text if target_field == "claim_text" else f"Benchmark claim for {case_id}."
    claim_id = f"claim-{case_id}"
    claims = [
        {
            "claim_id": claim_id,
            "claim_text": claim_text,
            "claim_type": "technical_fact",
            "scope": {
                "domain": "cognitive-search-benchmark",
                "applies_to": [project],
                "not_applies_to": [],
            },
            "evidence": [evidence],
            "relation_to_existing": {
                "type": "new",
                "target_pages": [],
                "delta_text": "",
                "reason": "hermetic benchmark fixture",
            },
            "recommended_action": "create_page",
            "confidence": 0.9,
        }
    ]
    episode_fields: dict[str, list[dict[str, Any]]] = {}
    for field_name in COGNITION_EPISODE_FIELDS:
        if sparse_episode and field_name != target_field:
            episode_fields[field_name] = [
                {
                    "entry_id": f"entry-{case_id}-{field_name}",
                    "status": "unknown",
                    "reason": "not part of this frozen single-channel benchmark case",
                    "evidence_refs": [],
                    "claim_ids": [],
                }
            ]
            continue
        value = text if target_field == field_name else f"benchmark {field_name} for {case_id}"
        episode_fields[field_name] = [
            {
                "entry_id": f"entry-{case_id}-{field_name}",
                "status": "known",
                "value": value,
                "evidence_refs": [evidence],
                "claim_ids": [claim_id],
            }
        ]
    behavior_summary = (
        text if target_field == "behavior_summary" else f"benchmark behavior for {case_id}"
    )
    behavior_intent = {
        "content_source": "native_dialogue",
        "user_intent_signal": "sharing_information",
        "intent_hypothesis": behavior_summary,
        "intent_evidence": [evidence],
        "intent_verification_events": [],
        "intent_confidence": 0.9,
        "intent_status": "verified",
        "behavior_summary": behavior_summary,
    }
    return CognitiveStateRevision.create(
        object_type="cognition_episode",
        object_id=object_id,
        source_event_id=event_id,
        source_revision_id=source_revision_id,
        source_content_hash=source_hash,
        scope_type="project",
        scope_id=project,
        evidence_refs=(f"{source_revision_id}#0:{span_end}",),
        payload={
            "schema_version": COGNITION_EPISODE_SCHEMA_VERSION,
            "cognition_context_hash": context_hash,
            "input_spec_hash": _digest(f"input:{case_id}"),
            "extraction_output_hash": source_hash,
            "source_agent": "codex",
            "source_session_id": case_id,
            "source_event_ids": [source_revision_id],
            "raw_completeness": "full",
            "loss_contract": "lossless-visible-v1",
            "source_spans": [source_span],
            "artifact_catalog_hash": artifact_catalog_hash,
            "source_authority_catalog_hash": authority_catalog_hash,
            "source_authority_catalog": authority_catalog,
            "artifact_catalog": artifact_catalog,
            "acl": "local_user",
            "access_control": access,
            "purpose": "cognition_distillation",
            "retention_policy": "cognitive_search_benchmark",
            "claims": claims,
            "claim_catalog_hash": sha256_json(claims),
            "user_behavior_intent": behavior_intent,
            **episode_fields,
        },
        supersedes_revision_id=supersedes_revision_id,
        created_at=_CREATED_AT,
    )


def _commit_episode(
    store: CognitiveStateStore,
    revision: CognitiveStateRevision,
    *,
    dispatch: bool = False,
) -> None:
    consumers = (
        ("wiki", "knowledge_graph", "cognitive_graph") if dispatch else ("benchmark_state_fixture",)
    )
    event = CognitiveDataEvent(
        event_id=revision.source_event_id,
        source_id=revision.source_revision_id,
        asset_id=f"asset:{revision.object_id}",
        source_kind="distill",
        source_uri=f"raw://benchmark/{revision.object_id}",
        content_hash=revision.source_content_hash,
        canonical_subject=f"episode:{revision.object_id}",
        data_type="cognition_episode",
        producer="cognitive_search_benchmark",
        intended_consumers=consumers,
        privacy_level="private",
        confidence=0.9,
        evidence_refs=revision.evidence_refs,
        dedupe_key=f"benchmark:{revision.object_id}:{revision.revision_id}",
        created_at=_CREATED_AT,
    )
    commands = tuple(
        LocalConsumerCommand.create(
            revision_id=revision.revision_id,
            consumer_id=consumer,
            command_type=(
                "project_cognition_episode" if dispatch else "benchmark_cognitive_search_fixture"
            ),
            payload={
                "primary_revision_id": revision.revision_id,
                "object_type": "cognition_episode",
                "object_id": revision.object_id,
            },
        )
        for consumer in consumers
    )
    store.unit_of_work().commit(revisions=(revision,), event=event, commands=commands)


def _build_belief_case(
    store: CognitiveStateStore,
    case: Mapping[str, Any],
    *,
    principal: PrincipalEnvelope,
) -> tuple[str, str, str]:
    from core.cognitive.belief_revision import BeliefRevisionCommand, BeliefRevisionStore

    belief_store = BeliefRevisionStore(store)
    claim = str(case["text"])
    superseded_probe = str(case.get("superseded_query") or "").strip()
    if not superseded_probe:
        raise ValueError("belief benchmark requires a superseded_query")

    def command(
        source_suffix: str,
        *,
        expected: str = "",
        opposing: bool = False,
        withdrawn: tuple[str, ...] = (),
        correction_target: str = "",
    ):
        source_id = f"benchmark-belief:{source_suffix}"
        access = make_cognitive_access_envelope(
            owner_principal_id=principal.principal_id,
            owner_agent=principal.agent,
            scope_type="project",
            scope_id="mnemos",
            project="mnemos",
            purposes=("belief_read", "cognitive_state_write"),
            consent_provenance_refs=(source_id,),
            sensitivity="sensitive",
            retention_policy="cognitive_search_benchmark",
            source_acl_lineage=(_digest(source_id),),
        )
        return BeliefRevisionCommand(
            claim=claim,
            claim_kind="fact",
            scope_type="project",
            scope_id="mnemos",
            source_id=source_id,
            source_revision_id=f"revision:{source_id}",
            source_content_hash=_digest(source_id),
            source_access_control=access,
            source_span_ids=(f"revision:{source_id}#0:{len(claim)}",),
            supporting_evidence=(() if opposing else (superseded_probe,)),
            opposing_evidence=(f"evidence:{source_suffix}",) if opposing else (),
            withdrawn_evidence=withdrawn,
            valid_from=_CREATED_AT,
            invalidation_conditions=("retention contract changes",),
            expected_current_revision_id=expected,
            correction_of_revision_id=correction_target,
            correction_evidence_ref=source_id if correction_target else "",
            created_at=_CREATED_AT,
        )

    first = belief_store.revise(command("initial"), principal=principal)
    current = belief_store.revise(
        command(
            "current",
            expected=first.revision_id,
            opposing=True,
            withdrawn=(superseded_probe,),
            correction_target=first.revision_id,
        ),
        principal=principal,
    )
    return current.belief_id, current.revision_id, first.revision_id


def _project_graph_benchmark_episodes(
    *,
    root: Path,
    wiki_dir: Path,
    store: CognitiveStateStore,
    positive_cases: Sequence[Mapping[str, Any]],
    negative_cases: Sequence[Mapping[str, Any]],
    expected: dict[str, str],
    current_revisions: dict[str, str],
) -> tuple[Path, Path]:
    """Build graph/evidence fixtures only through episode commit and dispatch."""

    from core.cognitive.cognition_episode_dispatch import CognitionEpisodeDispatchOwner
    from core.cognitive.cognition_episode_projection_schema import (
        initialize_wiki_projection_schema,
    )
    from core.mnemos_bus import EventBus
    from core.wiki_projection_lifecycle import WikiProjectionLedger

    projected: list[tuple[Mapping[str, Any], CognitiveStateRevision, str]] = []
    for case in [*positive_cases, *negative_cases]:
        channel = str(case["channel"])
        if channel not in {"cognitive_graph", "evidence_graph"}:
            continue
        if case.get("acl_mode") == "unknown":
            continue
        purpose = "cognitive_graph_read" if channel == "cognitive_graph" else "evidence_graph_read"
        case_id = str(case["case_id"])
        revision = _episode_revision(
            {**case, "field": "facts"},
            event_id=f"cde-benchmark-{case_id}",
            object_id=f"bench-projected-{case_id}",
            project=str(case.get("scope_id") or "mnemos"),
            read_purposes=(purpose,),
            sparse_episode=True,
        )
        _commit_episode(store, revision, dispatch=True)
        projected.append((case, revision, channel))
        if case in positive_cases:
            current_revisions[case_id] = revision.revision_id

    projection_db = root / "wiki_projection.db"
    ledger = WikiProjectionLedger(projection_db)
    for case, revision, _channel in projected:
        page = wiki_dir / "benchmark-cognition" / f"{case['case_id']}.md"
        page.parent.mkdir(parents=True, exist_ok=True)
        project = str(case.get("scope_id") or "mnemos")
        page.write_text(
            "---\n"
            f"cognition_episode_revision_id: {revision.revision_id}\n"
            "scope: project\n"
            "source_agent: codex\n"
            f"project: {project}\n"
            "acl_schema_version: 1\n"
            "acl_metadata_complete: true\n"
            "acl_reconciliation_status: proven\n"
            "---\n\n"
            f"# Benchmark cognition {case['case_id']}\n",
            encoding="utf-8",
        )
        ledger.record_mutation(
            page,
            mutation_type="create",
            page_id=f"benchmark-page-{case['case_id']}",
        )
    initialize_wiki_projection_schema(projection_db)

    config = SimpleNamespace(
        data_dir=root,
        database_dir=root,
        mnemos_dir=root,
        wiki_dir=wiki_dir,
        cognitive_graph_db_path=root / "cognitive_graph.db",
        get=lambda key, default=None: {
            "event_bus.max_retries": 3,
            "event_bus.retry_base_seconds": 0,
            "event_bus.retry_max_seconds": 0,
            "event_bus.dispatch_workers": 1,
            "event_bus.handler_timeout_seconds": 0,
        }.get(key, default),
    )
    bus = EventBus(config=config)
    owner = CognitionEpisodeDispatchOwner(config=config, event_bus=bus)
    owner.subscribe()
    bus.start_dispatch()
    try:
        for _case, revision, _channel in projected:
            trace_id = str(owner.publish_revision(revision.revision_id)["trace_id"])
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                with sqlite3.connect(root / "events.db") as connection:
                    status_row = connection.execute(
                        "SELECT status,retry_count FROM events WHERE trace_id=?",
                        (trace_id,),
                    ).fetchone()
                    dead_row = connection.execute(
                        "SELECT failure_reason FROM dead_letters WHERE trace_id=?",
                        (trace_id,),
                    ).fetchone()
                if dead_row is not None:
                    raise RuntimeError(
                        "cognitive benchmark episode dispatch reached dead letter: "
                        + str(dead_row[0])
                    )
                if status_row is not None and str(status_row[0]) == "done":
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError(
                    "cognitive benchmark episode dispatch did not converge: "
                    f"revision={revision.revision_id}, status={status_row!r}"
                )
    finally:
        bus.stop_dispatch()
        bus.close()

    graph_db = root / "cognitive_graph.db"
    evidence_db = root / "evidence_graph.db"
    with sqlite3.connect(graph_db) as graph, sqlite3.connect(evidence_db) as evidence:
        for case, revision, channel in projected:
            case_id = str(case["case_id"])
            entry_id = str(revision.payload["facts"][0]["entry_id"])
            if channel == "cognitive_graph":
                row = graph.execute(
                    "SELECT id FROM cognitive_relations "
                    "WHERE target=? AND relation_type='contains'",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("projected benchmark graph relation is missing")
                expected[case_id] = str(row[0])
            elif case.get("builder") == "evidence_edge":
                row = evidence.execute(
                    "SELECT id FROM evidence_edges "
                    "WHERE source_id=? AND relation_type='derived_from'",
                    (entry_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("projected benchmark evidence edge is missing")
                expected[case_id] = str(row[0])
            else:
                expected[case_id] = entry_id

        for case in negative_cases:
            if case.get("acl_mode") != "unknown":
                continue
            evidence.execute(
                """INSERT INTO evidence_nodes
                   (id, node_type, title, source_path, content, metadata,
                    access_control, created_at)
                   VALUES (?, 'claim', ?, '', '', '{}', '', ?)""",
                (str(case["object_id"]), str(case["query"]), _CREATED_AT),
            )
            expected[str(case["case_id"])] = str(case["object_id"])
        evidence.commit()
    return graph_db, evidence_db


def _build_wiki(
    root: Path,
    cases: Sequence[Mapping[str, Any]],
    negatives: Sequence[Mapping[str, Any]],
    expected: dict[str, str],
) -> Path:
    """Build exact ACL-bearing Wiki fixtures, including deep-tail content."""

    wiki_dir = root / "wiki"
    wiki_dir.mkdir(exist_ok=True)

    def write_case(case: Mapping[str, Any], *, authorized: bool) -> None:
        relative_path = Path(str(case["object_id"]))
        target = (wiki_dir / relative_path).resolve(strict=False)
        try:
            canonical_path = target.relative_to(wiki_dir.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError("benchmark Wiki path escapes fixture root") from exc
        if not canonical_path.endswith(".md"):
            raise ValueError("benchmark Wiki object_id must be a Markdown path")
        target.parent.mkdir(parents=True, exist_ok=True)
        if authorized:
            project = "mnemos"
            acl = (
                "scope: project\n"
                "source_agent: codex\n"
                f"project: {project}\n"
                "acl_schema_version: 1\n"
                "acl_metadata_complete: true\n"
                "acl_reconciliation_status: proven\n"
            )
        elif case.get("acl_mode") == "unknown":
            acl = (
                "scope: restricted\n"
                "acl_schema_version: 1\n"
                "acl_metadata_complete: true\n"
                "acl_reconciliation_status: restricted_unknown\n"
            )
        else:
            project = str(case.get("scope_id") or "other-project")
            acl = (
                "scope: project\n"
                "source_agent: codex\n"
                f"project: {project}\n"
                "acl_schema_version: 1\n"
                "acl_metadata_complete: true\n"
                "acl_reconciliation_status: proven\n"
            )
        prefix_chars = int(case.get("tail_prefix_chars") or 0)
        prefix = ("ordinary-prefix " * ((prefix_chars // 16) + 1))[:prefix_chars]
        text = str(case.get("text") or case["query"])
        target.write_text(
            f"---\n{acl}置信度: 0.95\n---\n"
            f"# Benchmark Wiki {case['case_id']}\n{prefix}{text}\n",
            encoding="utf-8",
        )

    for case in cases:
        write_case(case, authorized=True)
        expected[str(case["case_id"])] = str(case["object_id"])
    for case in negatives:
        write_case(case, authorized=False)
    return wiki_dir


def build_environment(root: Path, fixture: Mapping[str, Any]) -> BenchmarkEnvironment:
    root.mkdir(parents=True, exist_ok=False)
    state_db = root / "producer_consumer_ledger.db"
    initialize_cognitive_state_schema(state_db)
    store = CognitiveStateStore(state_db)
    principal = benchmark_principal()
    expected: dict[str, str] = {}
    current_revisions: dict[str, str] = {}
    superseded_revisions: dict[str, str] = {}
    positive_cases = list(fixture["cases"])
    negative_cases = list(fixture["negative_cases"])
    wiki_dir = _build_wiki(
        root,
        [case for case in positive_cases if case["channel"] == "wiki_page"],
        [case for case in negative_cases if case["channel"] == "wiki_page"],
        expected,
    )

    for case in positive_cases:
        if case["channel"] != "cognitive_state":
            continue
        if case.get("builder") == "belief_revision":
            object_id, revision_id, superseded_revision_id = _build_belief_case(
                store,
                case,
                principal=principal,
            )
            expected[str(case["case_id"])] = object_id
            current_revisions[str(case["case_id"])] = revision_id
            superseded_revisions[str(case["case_id"])] = superseded_revision_id
            continue
        revision = _episode_revision(
            case,
            event_id=f"cde-benchmark-{case['case_id']}",
            object_id=str(case["object_id"]),
        )
        _commit_episode(store, revision)
        expected[str(case["case_id"])] = revision.object_id
        current_revisions[str(case["case_id"])] = revision.revision_id
        for index in range(int(case.get("denied_decoys") or 0)):
            decoy_case = dict(case)
            decoy_case["case_id"] = f"{case['case_id']}-denied-{index:02d}"
            decoy = _episode_revision(
                decoy_case,
                event_id=f"cde-benchmark-{decoy_case['case_id']}",
                object_id=f"denied-refill-{index:02d}",
                owner_principal_id=f"principal:denied:{index}",
            )
            _commit_episode(store, decoy)

    for case in negative_cases:
        if case["channel"] != "cognitive_state":
            continue
        revision = _episode_revision(
            {**case, "field": "claim_text", "text": case["query"]},
            event_id=f"cde-benchmark-{case['case_id']}",
            object_id=str(case["object_id"]),
            project=str(case.get("scope_id") or "mnemos"),
            owner_principal_id=str(case.get("owner_principal_id") or principal.principal_id),
        )
        _commit_episode(store, revision)

    graph_db, evidence_db = _project_graph_benchmark_episodes(
        root=root,
        wiki_dir=wiki_dir,
        store=store,
        positive_cases=[
            case
            for case in positive_cases
            if case["channel"] in {"cognitive_graph", "evidence_graph"}
        ],
        negative_cases=[
            case
            for case in negative_cases
            if case["channel"] in {"cognitive_graph", "evidence_graph"}
        ],
        expected=expected,
        current_revisions=current_revisions,
    )
    return BenchmarkEnvironment(
        wiki_dir=wiki_dir,
        state_db=state_db,
        cognitive_graph_db=graph_db,
        evidence_graph_db=evidence_db,
        principal=principal,
        narrowing=AccessNarrowing(project="mnemos"),
        expected_object_ids=expected,
        current_revision_ids=current_revisions,
        superseded_revision_ids=superseded_revisions,
    )


def _evaluate_order(
    cases: Sequence[Mapping[str, Any]],
    environment: BenchmarkEnvironment,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    search = ContextAwareSearch(
        wiki_base=str(environment.wiki_dir),
        cognitive_state_db=environment.state_db,
        cognitive_graph_db=environment.cognitive_graph_db,
        evidence_graph_db=environment.evidence_graph_db,
    )
    rows: list[dict[str, Any]] = []
    ranks: dict[str, int] = {}
    for case in cases:
        hits = search.search(
            str(case["query"]),
            principal=environment.principal,
            narrowing=environment.narrowing,
            limit=10,
            allow_embedding=False,
        )
        expected_id = environment.expected_object_ids[str(case["case_id"])]
        rank = next(
            (
                index
                for index, hit in enumerate(hits, start=1)
                if hit.result_kind == case["channel"] and hit.object_id == expected_id
            ),
            0,
        )
        expected_hit = hits[rank - 1] if rank else None
        ranks[str(case["case_id"])] = rank
        expected_field = str(case["field"])
        if case["channel"] == "cognitive_state":
            if expected_field == "claim_text":
                expected_field = "claims[0].claim_text"
            elif expected_field == "behavior_summary":
                expected_field = "user_behavior_intent.behavior_summary"
            elif expected_field != "belief_revision.claim":
                expected_field = f"cognition_episode.{expected_field}[0].value"
        rows.append(
            {
                "case_id": case["case_id"],
                "split": case["split"],
                "channel": case["channel"],
                "critical": bool(case["critical"]),
                "rank": rank,
                "expected_object_id": expected_id,
                "matched_field": expected_hit.matched_field if expected_hit else "",
                "expected_field": expected_field,
                "field_match": bool(
                    expected_hit is not None and expected_hit.matched_field == expected_field
                ),
                "source_revision_id": (expected_hit.source_revision_id if expected_hit else ""),
                "source_span_ids": (list(expected_hit.source_span_ids) if expected_hit else []),
                "acl_decision": expected_hit.acl_decision if expected_hit else "",
                "source_trace_valid": _valid_exact_source_trace(expected_hit),
                "current_revision_match": bool(
                    expected_hit is not None
                    and (
                        case["case_id"] not in environment.current_revision_ids
                        or expected_hit.revision_id
                        == environment.current_revision_ids[case["case_id"]]
                    )
                ),
            }
        )
    return rows, ranks


def _valid_exact_source_trace(hit: Any) -> bool:
    if hit is None or hit.acl_decision != "authorized":
        return False
    source_revision_id = str(hit.source_revision_id or "").strip()
    if not source_revision_id or not hit.source_span_ids:
        return False
    pattern = re.compile(re.escape(source_revision_id) + r"#([0-9]+):([0-9]+)")
    for span_id in hit.source_span_ids:
        match = pattern.fullmatch(str(span_id))
        if match is None or int(match.group(2)) <= int(match.group(1)):
            return False
    return True


def evaluate_benchmark(
    fixture: Mapping[str, Any],
    environment: BenchmarkEnvironment,
) -> dict[str, Any]:
    cases = list(fixture["cases"])
    baseline_rows, baseline_ranks = _evaluate_order(cases, environment)
    shuffled = list(cases)
    random.Random(int(fixture["shuffle_seed"])).shuffle(shuffled)
    shuffled_rows, shuffled_ranks = _evaluate_order(shuffled, environment)
    del shuffled_rows

    total = len(baseline_rows)
    retrieved_at_10 = sum(1 for row in baseline_rows if 0 < row["rank"] <= 10)
    critical_rows = [row for row in baseline_rows if row["critical"]]
    critical_at_5 = sum(1 for row in critical_rows if 0 < row["rank"] <= 5)
    reciprocal_rank = sum(1.0 / row["rank"] for row in baseline_rows if row["rank"])
    search = ContextAwareSearch(
        wiki_base=str(environment.wiki_dir),
        cognitive_state_db=environment.state_db,
        cognitive_graph_db=environment.cognitive_graph_db,
        evidence_graph_db=environment.evidence_graph_db,
    )
    negative_rows = []
    unauthorized_hit_count = 0
    for case in fixture["negative_cases"]:
        hits = search.search(
            str(case["query"]),
            principal=environment.principal,
            narrowing=environment.narrowing,
            limit=10,
            allow_embedding=False,
        )
        forbidden_object_id = environment.expected_object_ids.get(
            str(case["case_id"]),
            str(case["object_id"]),
        )
        forbidden = [hit.page_path for hit in hits if hit.object_id == forbidden_object_id]
        trace = search.get_last_query_trace()
        unauthorized_hit_count += len(forbidden)
        negative_rows.append(
            {
                "case_id": case["case_id"],
                "forbidden_hits": forbidden,
                "returned_count": len(hits),
                "access_filter": trace.get("access_filter", {}),
                "cognitive_access": trace.get("cognitive_state_access", {}),
            }
        )

    superseded_rows = []
    superseded_belief_hit_count = 0
    for case in cases:
        superseded_query = str(case.get("superseded_query") or "").strip()
        if not superseded_query:
            continue
        case_id = str(case["case_id"])
        hits = search.search(
            superseded_query,
            principal=environment.principal,
            narrowing=environment.narrowing,
            limit=10,
            allow_embedding=False,
        )
        stale_revision_id = environment.superseded_revision_ids[case_id]
        belief_id = environment.expected_object_ids[case_id]
        leaked = [
            hit.page_path
            for hit in hits
            if hit.revision_id == stale_revision_id
            or (hit.result_kind == "cognitive_state" and hit.object_id == belief_id)
        ]
        superseded_belief_hit_count += len(leaked)
        superseded_rows.append(
            {
                "case_id": case_id,
                "query": superseded_query,
                "superseded_revision_id": stale_revision_id,
                "leaked_hits": leaked,
            }
        )

    order_identity = [str(case["case_id"]) for case in shuffled]
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "fixture_hash": fixture_contract_hash(fixture),
        "query_count": total,
        "holdout_query_count": sum(1 for case in cases if case["split"] == "holdout"),
        "negative_query_count": len(negative_rows),
        "critical_query_count": len(critical_rows),
        "recall_at_10": retrieved_at_10 / total if total else 0.0,
        "critical_recall_at_5": critical_at_5 / len(critical_rows) if critical_rows else 0.0,
        "mrr": reciprocal_rank / total if total else 0.0,
        "unauthorized_hit_count": unauthorized_hit_count,
        "field_trace_gap": sum(1 for row in baseline_rows if not row["field_match"]),
        "source_trace_gap": sum(1 for row in baseline_rows if not row["source_trace_valid"]),
        "current_revision_gap": sum(
            1 for row in baseline_rows if not row["current_revision_match"]
        ),
        "superseded_belief_hit_count": superseded_belief_hit_count,
        "query_order_invariant": baseline_ranks == shuffled_ranks,
        "shuffled_order_hash": _digest(json.dumps(order_identity, separators=(",", ":"))),
        "cases": baseline_rows,
        "negative_cases": negative_rows,
        "superseded_belief_cases": superseded_rows,
    }


def scan_answer_leakage(
    fixture: Mapping[str, Any],
    *,
    repo_root: Path,
) -> dict[str, Any]:
    probes = {
        str(case["case_id"]): str(case["probe"]).casefold()
        for case in [*fixture["cases"], *fixture["negative_cases"]]
    }
    matches: list[dict[str, str]] = []
    production_files = sorted((repo_root / "core").rglob("*.py")) + [repo_root / "mnemos_cli.py"]
    for path in production_files:
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="ignore").casefold()
        for case_id, probe in probes.items():
            if probe in content:
                matches.append({"case_id": case_id, "path": path.relative_to(repo_root).as_posix()})
    return {
        "production_file_count": len(production_files),
        "probe_count": len(probes),
        "answer_leakage_count": len(matches),
        "matches": matches,
    }
