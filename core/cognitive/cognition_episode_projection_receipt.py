"""Fixed-family oracle for cognition-episode projection receipts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from core.cognitive.state_contract import CognitiveStateRevision, sha256_json
from core.wiki_projection_lifecycle import resolve_wiki_projection_db_path

COMMAND_TYPE = "project_cognition_episode"
_TARGET_BY_CONSUMER = {
    "wiki": "wiki",
    "knowledge_graph": "evidence_graph",
    "cognitive_graph": "cognitive_graph",
}


def projection_effect_id(command_id: str, consumer_id: str) -> str:
    target = _TARGET_BY_CONSUMER[str(consumer_id)]
    material = {
        "command_id": str(command_id),
        "target": target,
    }
    return "cogprojection-" + str(sha256_json(material)).split(":", 1)[1][:32]


def projection_before_hash(revision_id: str, consumer_id: str) -> str:
    if consumer_id == "wiki":
        payload = {"revision_id": revision_id, "wiki_projection": "unprojected"}
    elif consumer_id == "knowledge_graph":
        payload = {"revision_id": revision_id, "projection_state": "unprojected"}
    elif consumer_id == "cognitive_graph":
        payload = {"revision_id": revision_id, "graph_projection": "unprojected"}
    else:
        raise ValueError("unsupported cognition episode projection consumer")
    return str(sha256_json(payload))


@dataclass(frozen=True)
class CognitionEpisodeProjectionProof:
    consumer_id: str
    revision_id: str
    effect_id: str
    before_hash: str
    after_hash: str


@dataclass(frozen=True)
class CognitionEpisodeProjectionTargets:
    state_db_path: Path
    wiki_root: Path
    wiki_projection_db_path: Path
    evidence_db_path: Path
    cognitive_graph_db_path: Path

    @classmethod
    def from_config(
        cls,
        config: Any,
        *,
        state_db_path: Path,
    ) -> "CognitionEpisodeProjectionTargets":
        if config is None:
            raise RuntimeError("cognition episode projection verification requires runtime config")
        database_dir = Path(getattr(config, "database_dir", "")).expanduser().resolve(strict=False)
        wiki_root = Path(getattr(config, "wiki_dir", "")).expanduser().resolve(strict=False)
        if not str(database_dir) or not str(wiki_root):
            raise ValueError("cognition episode projection config paths are incomplete")
        expected_state = database_dir / "producer_consumer_ledger.db"
        normalized_state = Path(state_db_path).expanduser().resolve(strict=False)
        if normalized_state != expected_state:
            raise ValueError("cognition episode state database differs from runtime config")
        cognitive_graph = (
            Path(
                getattr(config, "cognitive_graph_db_path", None)
                or database_dir / "cognitive_graph.db"
            )
            .expanduser()
            .resolve(strict=False)
        )
        return cls(
            state_db_path=normalized_state,
            wiki_root=wiki_root,
            wiki_projection_db_path=resolve_wiki_projection_db_path(config)
            .expanduser()
            .resolve(strict=False),
            evidence_db_path=database_dir / "evidence_graph.db",
            cognitive_graph_db_path=cognitive_graph,
        )


def verify_cognition_episode_projection(
    *,
    targets: CognitionEpisodeProjectionTargets,
    command: Mapping[str, Any],
    revision: CognitiveStateRevision,
    proof: CognitionEpisodeProjectionProof,
) -> dict[str, Any]:
    """Observe the fixed target family instead of trusting caller evidence."""

    consumer_id = str(command.get("consumer_id") or "")
    command_id = str(command.get("command_id") or "")
    revision_id = str(command.get("revision_id") or "")
    payload = dict(command.get("payload") or {})
    if (
        command.get("command_type") != COMMAND_TYPE
        or consumer_id not in _TARGET_BY_CONSUMER
        or revision.object_type != "cognition_episode"
        or revision.revision_id != revision_id
        or payload
        != {
            "primary_revision_id": revision_id,
            "object_type": "cognition_episode",
            "object_id": revision.object_id,
        }
    ):
        raise ValueError("cognition episode projection command binding mismatch")
    if (
        proof.consumer_id != consumer_id
        or proof.revision_id != revision_id
        or proof.effect_id != projection_effect_id(command_id, consumer_id)
        or proof.before_hash != projection_before_hash(revision_id, consumer_id)
        or not proof.after_hash.startswith("sha256:")
    ):
        raise ValueError("cognition episode projection proof identity mismatch")

    target_db = {
        "wiki": targets.wiki_projection_db_path,
        "knowledge_graph": targets.evidence_db_path,
        "cognitive_graph": targets.cognitive_graph_db_path,
    }[consumer_id]
    if not target_db.is_file():
        raise RuntimeError("cognition episode target effect database is missing")
    with sqlite3.connect(f"file:{target_db.resolve(strict=True)}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM cognition_episode_projection_effects WHERE effect_id=?",
            (proof.effect_id,),
        ).fetchone()
        if row is None:
            if consumer_id == "wiki":
                raise RuntimeError("configured Wiki projection target effect journal is missing")
            raise RuntimeError("cognition episode target effect journal is missing")
        target = dict(row)
        if consumer_id == "wiki":
            return _verify_wiki_projection(
                conn,
                command,
                revision,
                proof,
                target,
                targets,
            )
        if consumer_id == "knowledge_graph":
            omission_count = int(
                conn.execute(
                    """SELECT COUNT(*)
                       FROM cognition_episode_projection_omissions
                       WHERE revision_id=? AND disposition='omitted'""",
                    (revision_id,),
                ).fetchone()[0]
            )
            if omission_count != int(target["omission_count"]):
                raise RuntimeError("cognition episode omission receipt gap")
    if (
        str(target["revision_id"]) != revision_id
        or str(target["effect_id"]) != proof.effect_id
        or str(target["before_hash"]) != proof.before_hash
        or str(target["after_hash"]) != proof.after_hash
    ):
        raise RuntimeError("cognition episode target effect journal drift")
    target_name = _TARGET_BY_CONSUMER[consumer_id].replace("_", "-")
    outcome = (
        "committed cognition episode evidence graph projected"
        if consumer_id == "knowledge_graph"
        else "committed cognition episode cognitive graph projected"
    )
    count_metadata = (
        {
            "node_count": int(target["node_count"]),
            "edge_count": int(target["edge_count"]),
            "omission_count": int(target["omission_count"]),
        }
        if consumer_id == "knowledge_graph"
        else {"relation_count": int(target["relation_count"])}
    )
    return {
        "evidence_refs": (
            f"cognition-episode-command:{command_id}",
            f"cognition-episode-revision:{revision_id}",
            f"target-after:{proof.after_hash}",
            f"target-journal:{target_name}:{proof.effect_id}:{proof.after_hash}",
        ),
        "outcome": outcome,
        "metadata": count_metadata,
    }


def _verify_wiki_projection(
    conn: sqlite3.Connection,
    command: Mapping[str, Any],
    revision: CognitiveStateRevision,
    proof: CognitionEpisodeProjectionProof,
    target: Mapping[str, Any],
    targets: CognitionEpisodeProjectionTargets,
) -> dict[str, Any]:
    try:
        pages = json.loads(str(target["projection_json"]))
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("configured Wiki projection target journal is invalid") from exc
    if not isinstance(pages, list) or not pages or len(pages) != int(target["page_count"]):
        raise RuntimeError("configured Wiki projection target denominator drift")
    expected_keys = {
        "path",
        "content_sha256",
        "page_id",
        "page_revision",
        "mutation_id",
    }
    for raw_page in pages:
        if not isinstance(raw_page, Mapping) or set(raw_page) != expected_keys:
            raise RuntimeError("configured Wiki projection target page contract is invalid")
        page = dict(raw_page)
        candidate = (targets.wiki_root / str(page["path"])).resolve(strict=False)
        try:
            candidate.relative_to(targets.wiki_root)
        except ValueError as exc:
            raise RuntimeError("configured Wiki projection target escaped its vault") from exc
        mutation = conn.execute(
            """SELECT mutation_id, page_id, page_revision, page_path,
                      content_sha256, tombstone
               FROM wiki_mutations WHERE mutation_id=?""",
            (str(page["mutation_id"]),),
        ).fetchone()
        if mutation is None or any(
            (
                str(mutation["page_id"]) != str(page["page_id"]),
                str(mutation["page_revision"]) != str(page["page_revision"]),
                Path(str(mutation["page_path"])).resolve(strict=False) != candidate,
                "sha256:" + str(mutation["content_sha256"]) != str(page["content_sha256"]),
                bool(mutation["tombstone"]),
            )
        ):
            raise RuntimeError("configured Wiki projection mutation receipt drift")
    expected_after = sha256_json(
        {
            "revision_id": revision.revision_id,
            "pages": pages,
        }
    )
    if any(
        (
            str(target["revision_id"]) != revision.revision_id,
            str(target["effect_id"]) != proof.effect_id,
            str(target["before_hash"]) != proof.before_hash,
            str(target["after_hash"]) != proof.after_hash,
            str(target["manifest_hash"]) != expected_after,
            proof.after_hash != expected_after,
        )
    ):
        raise RuntimeError("configured Wiki projection target effect drift")
    command_id = str(command["command_id"])
    return {
        "evidence_refs": (
            f"cognition-episode-command:{command_id}",
            f"cognition-episode-revision:{revision.revision_id}",
            f"target-after:{proof.after_hash}",
            *(
                "target-oracle:wiki-mutation:"
                f"{page['mutation_id']}:{page['page_revision']}:{page['content_sha256']}"
                for page in pages
            ),
        ),
        "outcome": "committed cognition episode Wiki projection verified",
        "metadata": {"page_count": len(pages)},
    }
