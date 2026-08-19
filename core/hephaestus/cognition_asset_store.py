"""Durable owner for complete skill cognition and derived proposals.

The legacy ``skill_suggestion`` string is presentation only.  This module owns
the canonical, lossless extracted cognition asset and the independently
versioned proposal derived from it.  Both are idempotent and receipt-bearing;
the proposal table cannot contain a row without its parent asset.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
import uuid

from core.access_policy import complete_write_acl
from core.hephaestus.chunk_checkpoint import fragment_to_dict
from core.hephaestus.distill_input_spec import DistillInputSpec
from core.hephaestus.distillation_models import (
    CognitiveDecisionAssetProposalReceipt,
    CognitionAssetCommitReceipt,
    KnowledgeFragment,
    PipelineLayerResult,
)
from core.privacy.content_redaction import REDACTION_POLICY, redact_persistence_value

ASSET_SCHEMA_VERSION = "mnemos.cognition_asset.v1"
PROPOSAL_SCHEMA_VERSION = "cognitive_decision_asset.v1"
PROPOSAL_VERSION = 1
_PROPOSAL_TYPES = frozenset(
    {
        "methodology",
        "pitfall_pattern",
        "decision_heuristic",
        "verification_recipe",
        "automation_skill_candidate",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_cognition_judgment(result: Any, preliminary_judgment: str) -> bool:
    """Resolve L3 routing against the admitted union without skill downgrade."""

    admitted = str(result.extraction_judgment or "")
    error_code = ""
    reason = ""
    if preliminary_judgment == "skill" and admitted != "skill":
        error_code = "skill_extraction_judgment_mismatch"
        reason = "preliminary skill judgment disagrees with admitted extraction root"
    elif admitted not in {"knowledge", "skill"}:
        error_code = "extraction_judgment_invalid"
        reason = "non-skip extraction lacks an asset-bearing judgment"
    resolved = "error" if error_code else admitted
    result.layer_results.append(
        PipelineLayerResult(
            4,
            "typed_judgment_resolution",
            not error_code,
            {
                "preliminary": preliminary_judgment,
                "admitted": admitted,
                "resolved": resolved,
            },
        )
    )
    result.judgment = resolved
    if error_code:
        result.judgment_reason = reason
        result.error = error_code
        return False
    return True


@dataclass(frozen=True)
class CognitiveDecisionAssetProposal:
    """Versioned, typed derivative of an already committed cognition asset."""

    proposal_id: str
    asset_id: str
    title: str
    decision_context: str
    asset_type: str
    evidence_refs: tuple[str, ...]
    applicability: tuple[str, ...]
    failure_modes: tuple[str, ...]
    verification_recipe: tuple[str, ...]
    automation_derivative_allowed: bool
    proposal_version: int = PROPOSAL_VERSION
    asset_schema: str = PROPOSAL_SCHEMA_VERSION

    @classmethod
    def from_mapping(
        cls,
        *,
        asset_id: str,
        value: Mapping[str, Any],
        allowed_evidence_refs: Sequence[str],
    ) -> "CognitiveDecisionAssetProposal":
        sanitized = redact_persistence_value(value).value
        if not isinstance(sanitized, Mapping):
            raise ValueError("cognitive_proposal_payload_invalid")
        value = sanitized
        schema = _required_string(value.get("asset_schema"))
        asset_type = _required_string(value.get("asset_type"))
        title = _required_string(value.get("skill_name"))
        decision_context = _required_string(value.get("skill_purpose"))
        if schema != PROPOSAL_SCHEMA_VERSION:
            raise ValueError("cognitive_proposal_schema_unsupported")
        if asset_type not in _PROPOSAL_TYPES:
            raise ValueError("cognitive_proposal_asset_type_invalid")
        evidence_refs = _strict_string_tuple(value.get("evidence_refs"))
        allowed = frozenset(str(item) for item in allowed_evidence_refs)
        if not set(evidence_refs).issubset(allowed):
            raise ValueError("cognitive_proposal_evidence_ref_unbound")
        applicability = _strict_string_tuple(value.get("applicability"))
        failure_modes = _strict_string_tuple(value.get("failure_modes"))
        verification_recipe = _strict_string_tuple(value.get("verification_recipe"))
        automation_derivative_allowed = value.get("automation_derivative_allowed")
        if not isinstance(automation_derivative_allowed, bool):
            raise ValueError("cognitive_proposal_automation_flag_invalid")
        base = {
            "asset_id": asset_id,
            "asset_schema": schema,
            "asset_type": asset_type,
            "title": title,
            "decision_context": decision_context,
            "evidence_refs": evidence_refs,
            "applicability": applicability,
            "failure_modes": failure_modes,
            "verification_recipe": verification_recipe,
            "automation_derivative_allowed": automation_derivative_allowed,
            "proposal_version": PROPOSAL_VERSION,
        }
        proposal_id = "cogproposal-" + _sha256(_canonical_json(base)).split(":", 1)[1][:32]
        return cls(
            proposal_id=proposal_id,
            asset_id=asset_id,
            title=title,
            decision_context=decision_context,
            asset_type=asset_type,
            evidence_refs=evidence_refs,
            applicability=applicability,
            failure_modes=failure_modes,
            verification_recipe=verification_recipe,
            automation_derivative_allowed=automation_derivative_allowed,
            proposal_version=PROPOSAL_VERSION,
            asset_schema=schema,
        )

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "evidence_refs",
            "applicability",
            "failure_modes",
            "verification_recipe",
        ):
            payload[key] = list(payload[key])
        return payload

    @property
    def display_text(self) -> str:
        return f"{self.asset_type}: {self.title}: {self.decision_context}"


class CognitionAssetStore:
    """SQLite owner for full assets and separately receipted derivatives."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser()

    @classmethod
    def from_config(
        cls,
        cfg: Any,
        *,
        wiki_base: Path,
    ) -> "CognitionAssetStore":
        configured = cfg.get("distill.action_router.db_path") if hasattr(cfg, "get") else None
        if configured:
            return cls(Path(configured))
        wiki_dir = Path(wiki_base).expanduser()
        configured_wiki = Path(getattr(cfg, "wiki_dir", "") or "").expanduser()
        if configured_wiki and wiki_dir != configured_wiki:
            database_dir = wiki_dir / ".mnemos"
        else:
            database_dir = Path(
                getattr(cfg, "database_dir", "") or (wiki_dir / ".mnemos")
            ).expanduser()
        return cls(database_dir / "distill_actions.db")

    def commit_asset(
        self,
        result: Any,
        fragments: Sequence[KnowledgeFragment],
    ) -> CognitionAssetCommitReceipt:
        """Commit a complete redacted asset, or return a typed retryable failure."""

        try:
            asset_id, payload, content_hash, counts = self._build_asset_payload(
                result,
                fragments,
            )
            payload_json = _canonical_json(payload)
            self._ensure_db()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT content_hash, asset_payload
                    FROM cognition_asset_commits
                    WHERE asset_id=?
                    """,
                    (asset_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["content_hash"]) != content_hash
                        or str(existing["asset_payload"]) != payload_json
                    ):
                        return CognitionAssetCommitReceipt(
                            status="retryable_failed",
                            asset_id=asset_id,
                            error_code="cognition_asset_identity_collision",
                        )
                    return CognitionAssetCommitReceipt(
                        status="existing",
                        asset_id=asset_id,
                        content_hash=content_hash,
                        redaction_counts=counts,
                    )
                acl_json = _canonical_json(payload["acl"])
                redaction_json = _canonical_json(payload["redaction"])
                conn.execute(
                    """
                    INSERT INTO cognition_asset_commits (
                        asset_id, schema_version, created_at, session_id,
                        source_agent, input_spec_hash, extraction_output_hash,
                        content_hash, asset_payload, acl_payload,
                        redaction_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        ASSET_SCHEMA_VERSION,
                        _now(),
                        str(result.session_id),
                        str(result.input_spec.source_agent),
                        str(result.input_spec.input_spec_hash),
                        str(result.extraction_output_hash),
                        content_hash,
                        payload_json,
                        acl_json,
                        redaction_json,
                    ),
                )
                conn.commit()
            return CognitionAssetCommitReceipt(
                status="committed",
                asset_id=asset_id,
                content_hash=content_hash,
                redaction_counts=counts,
            )
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
            return CognitionAssetCommitReceipt(
                status="retryable_failed",
                error_code="cognition_asset_commit_failed",
            )

    def load_asset_payload(self, asset_id: str) -> dict[str, Any]:
        """Load the already-redacted canonical payload for proposal derivation."""

        self._ensure_db()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT asset_payload FROM cognition_asset_commits WHERE asset_id=?",
                (asset_id,),
            ).fetchone()
        if row is None:
            raise ValueError("cognition_asset_missing")
        payload = json.loads(str(row["asset_payload"]))
        if not isinstance(payload, dict):
            raise ValueError("cognition_asset_payload_invalid")
        return payload

    def commit_proposal(
        self,
        proposal: CognitiveDecisionAssetProposal,
    ) -> CognitiveDecisionAssetProposalReceipt:
        """Commit one versioned derivative only when its parent asset exists."""

        payload = proposal.canonical_payload()
        payload_json = _canonical_json(payload)
        content_hash = _sha256(payload_json)
        try:
            self._ensure_db()
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                parent = conn.execute(
                    "SELECT 1 FROM cognition_asset_commits WHERE asset_id=?",
                    (proposal.asset_id,),
                ).fetchone()
                if parent is None:
                    raise ValueError("cognition_asset_missing")
                existing = conn.execute(
                    """
                    SELECT proposal_id, content_hash, proposal_payload
                    FROM cognitive_decision_asset_proposals
                    WHERE asset_id=? AND proposal_version=?
                    """,
                    (proposal.asset_id, proposal.proposal_version),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["proposal_id"]) != proposal.proposal_id
                        or str(existing["content_hash"]) != content_hash
                        or str(existing["proposal_payload"]) != payload_json
                    ):
                        raise ValueError("cognitive_proposal_version_collision")
                    receipt = CognitiveDecisionAssetProposalReceipt(
                        status="existing",
                        asset_id=proposal.asset_id,
                        proposal_id=proposal.proposal_id,
                        content_hash=content_hash,
                    )
                    self._record_attempt(conn, receipt)
                    conn.commit()
                    return receipt
                conn.execute(
                    """
                    INSERT INTO cognitive_decision_asset_proposals (
                        proposal_id, asset_id, schema_version, proposal_version,
                        created_at, content_hash, proposal_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposal_id,
                        proposal.asset_id,
                        PROPOSAL_SCHEMA_VERSION,
                        proposal.proposal_version,
                        _now(),
                        content_hash,
                        payload_json,
                    ),
                )
                receipt = CognitiveDecisionAssetProposalReceipt(
                    status="committed",
                    asset_id=proposal.asset_id,
                    proposal_id=proposal.proposal_id,
                    content_hash=content_hash,
                )
                self._record_attempt(conn, receipt)
                conn.commit()
                return receipt
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return self.record_proposal_failure(
                proposal.asset_id,
                "cognitive_proposal_commit_failed",
            )

    def record_proposal_failure(
        self,
        asset_id: str,
        error_code: str,
    ) -> CognitiveDecisionAssetProposalReceipt:
        """Persist a countable optional failure without storing exception text."""

        safe_code = str(error_code or "cognitive_proposal_failed").strip()
        receipt = CognitiveDecisionAssetProposalReceipt(
            status="optional_failed",
            asset_id=asset_id,
            error_code=safe_code,
        )
        try:
            self._ensure_db()
            with self._connect() as conn:
                self._record_attempt(conn, receipt)
                conn.commit()
        except (OSError, ValueError, TypeError, sqlite3.Error):
            # The returned receipt still prevents the optional derivative from
            # blocking its already-durable parent asset and Wiki projection.
            pass
        return receipt

    def integrity_report(self) -> dict[str, int]:
        """Return the COG-013 orphan invariant as a machine-readable count."""

        if not self.db_path.exists():
            return {"skill_asset_without_cognition": 0}
        self._ensure_db()
        with self._connect() as conn:
            count = conn.execute("""
                SELECT COUNT(*)
                FROM cognitive_decision_asset_proposals AS proposal
                LEFT JOIN cognition_asset_commits AS asset
                  ON asset.asset_id = proposal.asset_id
                WHERE asset.asset_id IS NULL
                """).fetchone()[0]
        return {"skill_asset_without_cognition": int(count)}

    def _build_asset_payload(
        self,
        result: Any,
        fragments: Sequence[KnowledgeFragment],
    ) -> tuple[str, dict[str, Any], str, tuple[tuple[str, int], ...]]:
        if result.judgment != "skill" or result.extraction_judgment != "skill":
            raise ValueError("cognition_asset_requires_skill_admission")
        if not isinstance(result.input_spec, DistillInputSpec):
            raise ValueError("cognition_asset_input_spec_missing")
        if not result.extraction_output_hash or not isinstance(result.extraction_output, Mapping):
            raise ValueError("cognition_asset_extraction_proof_missing")
        if not fragments:
            raise ValueError("cognition_asset_fragments_missing")
        source_agent = str(result.input_spec.source_agent or "")
        if result.source and str(result.source) != source_agent:
            raise ValueError("cognition_asset_source_identity_mismatch")

        final_fragments = [fragment_to_dict(fragment) for fragment in fragments]
        source_spans = self._source_spans(result)
        chunk_aggregate = self._chunk_aggregate_payload(result)
        identity = {
            "schema_version": ASSET_SCHEMA_VERSION,
            "redaction_policy": REDACTION_POLICY,
            "input_spec_hash": result.input_spec.input_spec_hash,
            "extraction_output_hash": str(result.extraction_output_hash),
            "final_fragments_hash": _sha256(_canonical_json(final_fragments)),
            "source_spans_hash": _sha256(_canonical_json(source_spans)),
            "chunk_aggregate_hash": _sha256(_canonical_json(chunk_aggregate)),
        }
        asset_id = "cogasset-" + _sha256(_canonical_json(identity)).split(":", 1)[1][:32]
        exact_source_spans = bool(source_spans) and all(
            span.get("span_status") == "exact"
            and bool(span.get("revision_id"))
            and isinstance(span.get("span_start"), int)
            and isinstance(span.get("span_end"), int)
            and span["span_end"] > span["span_start"] >= 0
            for span in source_spans
        )
        acl = complete_write_acl(
            {
                "source_agent": source_agent,
                "session_id": str(result.session_id),
            },
            default_scope="private",
            session_id=str(result.session_id),
        )
        raw_payload: dict[str, Any] = {
            "schema_version": ASSET_SCHEMA_VERSION,
            "asset_id": asset_id,
            "asset_kind": "cognitive_decision_asset",
            "source": {
                "source_agent": source_agent,
                "session_id": str(result.session_id),
                "input_revision": str(result.input_revision or ""),
                "input_spec": result.input_spec.canonical_payload(),
                "raw_event_refs": list(result.raw_event_refs or []),
            },
            "acl": acl,
            "cognition": {
                "judgment": "skill",
                "judgment_reason": str(result.judgment_reason or ""),
                "extraction_output_hash": str(result.extraction_output_hash),
                "canonical_extraction_output": dict(result.extraction_output),
                "structured_output": dict(result.structured_output or {}),
                "final_fragments": final_fragments,
                "session_coverage": str(result.session_coverage or ""),
                "source_span_contract": {
                    "status": "exact" if exact_source_spans else "input_spec_bound",
                    "count": len(source_spans),
                },
                "source_spans": source_spans,
                "chunk_aggregate": chunk_aggregate,
            },
        }
        redacted = redact_persistence_value(raw_payload)
        payload = dict(redacted.value)
        payload["redaction"] = {
            "policy": REDACTION_POLICY,
            "counts": {name: count for name, count in redacted.counts},
            "total": redacted.total,
        }
        content_hash = _sha256(_canonical_json(payload))
        return asset_id, payload, content_hash, redacted.counts

    @staticmethod
    def _source_spans(result: Any) -> list[dict[str, Any]]:
        spans: list[dict[str, Any]] = []
        for chunk in result.chunk_extraction_results or []:
            for span in chunk.source_span_map:
                spans.append(dict(span))
        if not spans:
            spans.extend(dict(ref) for ref in result.raw_event_refs or [])
        return spans

    @staticmethod
    def _chunk_aggregate_payload(result: Any) -> dict[str, Any] | None:
        aggregate = result.chunk_aggregate
        if aggregate is None:
            return None
        return {
            "aggregate_contract_version": aggregate.aggregate_contract_version,
            "aggregate_contract_hash": aggregate.aggregate_contract_hash,
            "aggregate_root": dict(aggregate.aggregate_root),
            "aggregate_root_hash": aggregate.aggregate_root_hash,
            "episode": dict(aggregate.episode),
            "ordered_chunks": [
                {
                    "chunk_index": chunk.chunk_index,
                    "chunk_hash": chunk.chunk_hash,
                    "input_spec": chunk.input_spec.canonical_payload(),
                    "execution_spec_hash": chunk.execution_spec_hash,
                    "canonical_output": dict(chunk.canonical_output),
                    "canonical_output_hash": chunk.canonical_output_hash,
                    "source_span_map": [dict(span) for span in chunk.source_span_map],
                    "contract_verdict": chunk.contract_verdict,
                    "cache_hit": chunk.cache_hit,
                }
                for chunk in aggregate.ordered_chunks
            ],
        }

    def _ensure_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cognition_asset_commits (
                    asset_id TEXT PRIMARY KEY,
                    schema_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    input_spec_hash TEXT NOT NULL,
                    extraction_output_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    asset_payload TEXT NOT NULL,
                    acl_payload TEXT NOT NULL,
                    redaction_summary TEXT NOT NULL
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cognitive_decision_asset_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    proposal_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    proposal_payload TEXT NOT NULL,
                    FOREIGN KEY(asset_id) REFERENCES cognition_asset_commits(asset_id),
                    UNIQUE(asset_id, proposal_version)
                )
                """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cognitive_decision_proposal_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    asset_id TEXT NOT NULL,
                    proposal_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(asset_id) REFERENCES cognition_asset_commits(asset_id)
                )
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cognition_assets_session
                ON cognition_asset_commits(session_id, created_at)
                """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_cognitive_proposals_asset
                ON cognitive_decision_asset_proposals(asset_id, proposal_version)
                """)
            conn.commit()

    @staticmethod
    def _record_attempt(
        conn: sqlite3.Connection,
        receipt: CognitiveDecisionAssetProposalReceipt,
    ) -> None:
        conn.execute(
            """
            INSERT INTO cognitive_decision_proposal_attempts (
                attempt_id, asset_id, proposal_id, created_at, status, error_code
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "cogattempt-" + uuid.uuid4().hex,
                receipt.asset_id,
                receipt.proposal_id,
                _now(),
                receipt.status,
                receipt.error_code,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn


def proposal_evidence_catalog(asset_payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the admitted evidence identities a derivative may reference."""

    evidence: set[str] = set()

    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in {
                    "revision_id",
                    "logical_event_id",
                    "source_event_id",
                    "claim_id",
                }:
                    if isinstance(child, str) and child.strip():
                        evidence.add(child.strip())
                elif key == "source_event_ids" and isinstance(child, (list, tuple)):
                    evidence.update(
                        item.strip()
                        for item in child
                        if isinstance(item, str) and item.strip()
                    )
                collect(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(asset_payload)
    return tuple(sorted(evidence))


def _required_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("cognitive_proposal_required_string_invalid")
    return value.strip()


def _strict_string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("cognitive_proposal_string_array_invalid")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("cognitive_proposal_string_array_invalid")
    return tuple(dict.fromkeys(item.strip() for item in value))
