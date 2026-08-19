"""Read-only direction and projection-denominator audit for EvidenceGraph."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
from typing import Any

from core.utils import read_text_value

SCHEMA_VERSION = "mnemos.evidence_graph_direction_audit.v1"
_COGNITION_TYPES = frozenset(
    {
        "raw_revision_span",
        "observation",
        "episode",
        "claim",
        "belief",
        "decision",
        "prediction",
        "action",
        "outcome",
    }
)
_RANK = {
    "memory": 0,
    "knowledge": 0,
    "raw_revision_span": 0,
    "observation": 1,
    "mirror": 2,
    "claim": 2,
    "belief": 2,
    "reflection": 3,
    "insight": 4,
    "decision": 4,
    "prediction": 5,
    "action": 6,
    "outcome": 7,
}


def _connect_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.resolve(strict=True)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _has_derived_to_observation_call(source: str) -> bool:
    """Prove the producer emits entry -> observation with revision provenance."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "_evidence_edge" or len(node.args) < 4:
            continue
        source_arg, target_arg, relation_arg = node.args[1:4]
        if (
            isinstance(source_arg, ast.Name)
            and source_arg.id == "entry_id"
            and isinstance(target_arg, ast.Name)
            and target_arg.id == "observation_id"
            and isinstance(relation_arg, ast.Constant)
            and relation_arg.value == "derived_from"
        ):
            return True
    return False


def _static_findings(repo_root: Path) -> list[str]:
    graph_path = repo_root / "core/evidence/evidence_graph.py"
    plan_path = repo_root / "core/cognitive/cognition_episode_dispatch.py"
    graph = read_text_value(graph_path) if graph_path.is_file() else ""
    plan = read_text_value(plan_path) if plan_path.is_file() else ""
    required = (
        (graph_path, graph, "Canonical direction is derived → evidence"),
        (graph_path, graph, "self.get_edges(source_id=node_id)"),
        (plan_path, plan, "matched_raw_span_id"),
        (plan_path, plan, '"observed_in"'),
        (plan_path, plan, '"projection_revision_id"'),
    )
    findings = [
        f"missing_direction_contract:{path.relative_to(repo_root)}:{fragment}"
        for path, content, fragment in required
        if fragment not in content
    ]
    if not _has_derived_to_observation_call(plan):
        findings.append(
            "missing_direction_contract:"
            "core/cognitive/cognition_episode_dispatch.py:"
            '_evidence_edge(revision, entry_id, observation_id, "derived_from")'
        )
    return findings


def _direction_error(row: sqlite3.Row) -> bool:
    relation = str(row["relation_type"])
    source_type = str(row["source_type"])
    target_type = str(row["target_type"])
    if relation == "observed_in":
        return source_type != "observation" or target_type not in {
            "raw_revision_span",
            "memory",
            "knowledge",
        }
    if relation == "generated_from":
        return source_type == "reflection" and target_type == "insight"
    if relation == "derived_from":
        source_rank = _RANK.get(source_type)
        target_rank = _RANK.get(target_type)
        return source_rank is not None and target_rank is not None and source_rank < target_rank
    exact_pairs = {
        "predicted_from": ({"prediction"}, {"decision"}),
        "implements": ({"action"}, {"decision"}),
        "measures": ({"outcome"}, {"action", "prediction"}),
        "contains": ({"episode"}, _COGNITION_TYPES - {"episode", "raw_revision_span"}),
    }
    if relation in exact_pairs:
        sources, targets = exact_pairs[relation]
        return source_type not in sources or target_type not in targets
    if relation == "based_on":
        source_rank = _RANK.get(source_type)
        target_rank = _RANK.get(target_type)
        return source_rank is not None and target_rank is not None and source_rank < target_rank
    return False


def _valid_acl(value: Any) -> bool:
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return (
        parsed.get("schema_version") == "mnemos.cognitive_access.v1"
        and isinstance(parsed.get("owner"), dict)
        and bool(parsed["owner"].get("principal_id"))
        and bool(parsed["owner"].get("agent"))
        and isinstance(parsed.get("scope"), dict)
        and bool(parsed["scope"].get("scope_type"))
        and bool(parsed["scope"].get("scope_id"))
        and isinstance(parsed.get("purposes"), list)
        and isinstance(parsed.get("consent"), dict)
    )


def build_report(
    db_path: Path,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = repo_root or Path(__file__).resolve().parents[2]
    findings = _static_findings(repo_root)
    gaps = {
        "static_contract_gap": len(findings),
        "schema_gap": 0,
        "direction_gap": 0,
        "acl_gap": 0,
        "omission_gap": 0,
        "integrity_gap": 0,
    }
    runtime = {
        "initialized": False,
        "node_count": 0,
        "edge_count": 0,
        "projection_effect_count": 0,
        "legacy_direction_candidates": [],
    }
    db_path = Path(db_path)
    if not db_path.is_file():
        gaps["schema_gap"] += 1
        findings.append("evidence_graph_database_missing")
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "runtime": runtime,
            "gaps": gaps,
            "findings": findings,
        }
    try:
        with _connect_read_only(db_path) as conn:
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            base_tables = {"evidence_nodes", "evidence_edges"}
            projection_tables = {
                "cognition_episode_projection_effects",
                "cognition_episode_projection_omissions",
            }
            if not base_tables <= tables:
                gaps["schema_gap"] += 1
                findings.append("evidence_graph_schema_missing_base_tables")
            else:
                runtime["initialized"] = True
                runtime["node_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM evidence_nodes").fetchone()[0]
                )
                runtime["edge_count"] = int(
                    conn.execute("SELECT COUNT(*) FROM evidence_edges").fetchone()[0]
                )
                node_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(evidence_nodes)").fetchall()
                }
                edge_columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(evidence_edges)").fetchall()
                }
                has_acl = "access_control" in node_columns and "access_control" in edge_columns
                rows = conn.execute(
                    (
                        """SELECT e.id, e.source_id, e.target_id, e.relation_type,
                                  e.access_control, source.node_type AS source_type,
                                  target.node_type AS target_type
                           FROM evidence_edges AS e
                           JOIN evidence_nodes AS source ON source.id=e.source_id
                           JOIN evidence_nodes AS target ON target.id=e.target_id
                           ORDER BY e.id"""
                        if has_acl
                        else """SELECT e.id, e.source_id, e.target_id, e.relation_type,
                                  '' AS access_control,
                                  source.node_type AS source_type,
                                  target.node_type AS target_type
                           FROM evidence_edges AS e
                           JOIN evidence_nodes AS source ON source.id=e.source_id
                           JOIN evidence_nodes AS target ON target.id=e.target_id
                           ORDER BY e.id"""
                    )
                ).fetchall()
                candidates = [
                    {
                        "edge_id": int(row["id"]),
                        "source_id": str(row["source_id"]),
                        "target_id": str(row["target_id"]),
                        "relation_type": str(row["relation_type"]),
                        "source_type": str(row["source_type"]),
                        "target_type": str(row["target_type"]),
                    }
                    for row in rows
                    if _direction_error(row)
                ]
                gaps["direction_gap"] = len(candidates)
                runtime["legacy_direction_candidates"] = candidates
                cognition_nodes = {
                    str(row["id"]) for row in conn.execute("""SELECT id FROM evidence_nodes
                           WHERE node_type IN
                             ('raw_revision_span','observation','episode','claim','belief',
                              'decision','prediction','action','outcome')
                             AND json_valid(metadata)
                             AND COALESCE(
                               json_extract(metadata, '$.projection_revision_id'), ''
                             ) != ''""").fetchall()
                }
                node_acl_gaps = (
                    sum(
                        not _valid_acl(row["access_control"])
                        for row in conn.execute("""SELECT access_control FROM evidence_nodes
                               WHERE node_type IN
                                 ('raw_revision_span','observation','episode','claim','belief',
                                  'decision','prediction','action','outcome')
                                 AND json_valid(metadata)
                                 AND COALESCE(
                                   json_extract(metadata, '$.projection_revision_id'), ''
                                 ) != ''""").fetchall()
                    )
                    if has_acl
                    else len(cognition_nodes)
                )
                edge_acl_gaps = (
                    sum(
                        not _valid_acl(row["access_control"])
                        for row in rows
                        if str(row["source_id"]) in cognition_nodes
                        or str(row["target_id"]) in cognition_nodes
                    )
                    if has_acl
                    else 0
                )
                gaps["acl_gap"] = int(node_acl_gaps + edge_acl_gaps)
                if projection_tables <= tables:
                    runtime["projection_effect_count"] = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM cognition_episode_projection_effects"
                        ).fetchone()[0]
                    )
                    for effect in conn.execute("""SELECT revision_id, omission_count
                           FROM cognition_episode_projection_effects""").fetchall():
                        count = int(
                            conn.execute(
                                """SELECT COUNT(*)
                                   FROM cognition_episode_projection_omissions
                                   WHERE revision_id=? AND disposition='omitted'""",
                                (str(effect["revision_id"]),),
                            ).fetchone()[0]
                        )
                        gaps["omission_gap"] += int(count != int(effect["omission_count"]))
                else:
                    gaps["schema_gap"] += 1
                    findings.append("evidence_graph_schema_missing_projection_tables")
            if str(conn.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                gaps["integrity_gap"] += 1
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        gaps["schema_gap"] += 1
        findings.append(f"runtime_audit_error:{type(exc).__name__}:{exc}")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not any(gaps.values()),
        "runtime": runtime,
        "gaps": gaps,
        "findings": findings,
    }
