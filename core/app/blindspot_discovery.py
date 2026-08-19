# -*- coding: utf-8 -*-
"""
BlindspotDiscovery — 盲点主动发现

搜索时实时检测知识空白，在当前对话中轻量提示用户。

设计原则：
- 不在 Obsidian 中写静态 stub，避免污染 Wiki 和交互断裂
- 检测到盲区后，由宿主 Agent 在当前对话中询问用户
- 用户同意"记录"后，AI 搜索资料并继续对话；对话进入 raw → 蒸馏 → wiki
- 蒸馏完成后自动关闭对应盲区，形成闭环

状态机：
    detected → reminded → investigating → resolved
                      ↓
                  ignored (7 天内不再提醒)
                  mitigated (部分解决)

触发策略：
- 同一 topic 在同一 session_id 内只提醒一次
- 未提供 session_id 时，使用 5 分钟短冷却兜底
- 已解决 / 已忽略（冷却期内）不再提醒
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.access_policy import AccessNarrowing, PrincipalEnvelope
from core.app.blindspot_asset_schema import (
    HEAD_TABLE,
    REVISION_TABLE,
    BlindspotAssetSchemaError,
    initialize_blindspot_asset_schema,
    inspect_blindspot_asset_schema,
    read_blindspot_schema_status,
)
from core.cognitive.user_model_assets import (
    AssetScope,
    KnowledgeCoverageGap,
    KnowledgeCoverageResolutionEvidence,
)
from core.utils import read_bytes_value
from core.config import get_config
from core.kia.hygieia import KnowledgeImmuneSystem, QueryCoverageObservation

# Constants extracted from magic numbers
BLINDSPOT_DISCOVERY_IGNORE_COOLDOWN_SEC = 604800  # 忽略后 7 天冷却
BLINDSPOT_FALLBACK_COOLDOWN_SEC = 300  # 无 session_id 时 5 分钟冷却
WEEK_AGO_DAYS = 7
BLINDSPOT_MIN_ADMISSION_CONFIDENCE = 0.60
BLINDSPOT_DAILY_DETECTION_BUDGET = 100

logger = logging.getLogger(__name__)


@dataclass
class BlindSpotReminder:
    """Knowledge-coverage reminder projected to a host Agent."""

    topic: str
    description: str
    confidence: float
    status: str  # detected / reminded / investigating / resolved / mitigated / ignored
    detected_at: str
    reminded_at: Optional[str] = None
    asset_id: str = ""
    revision_id: str = ""
    asset_type: str = "knowledge_coverage_gap"
    dimension: str = "missing_topic"
    evidence_refs: tuple[str, ...] = ()
    scope_type: str = "vault"
    scope_id: str = "default"
    purpose: str = "knowledge_coverage_assistance"
    principal_id: str = ""
    expires_at: str = ""
    resolution_condition: str = "verified_knowledge_coverage_recheck"
    consumers: tuple[str, ...] = ("knowledge_retrieval", "verification_queue")

    @property
    def is_actionable(self) -> bool:
        return self.status in ("detected", "reminded")


@dataclass
class BlindSpotCheckResult:
    """盲点检查结果（含降级信息）"""

    reminder: Optional[BlindSpotReminder]
    suggested_query: str = ""  # 建议 AI 搜索用的查询词
    degraded: bool = False
    degraded_reasons: List[str] = field(default_factory=list)


class BlindspotDiscovery:
    """盲点主动发现"""

    # 忽略的 topic 7 天内不再提醒
    IGNORE_COOLDOWN_SEC = BLINDSPOT_DISCOVERY_IGNORE_COOLDOWN_SEC
    # 无 session_id 时的兜底冷却
    FALLBACK_COOLDOWN_SEC = BLINDSPOT_FALLBACK_COOLDOWN_SEC

    def __init__(
        self,
        wiki_base: Optional[str] = None,
        db_path: Optional[str] = None,
        *,
        initialize: bool = True,
        min_admission_confidence: float = BLINDSPOT_MIN_ADMISSION_CONFIDENCE,
        daily_detection_budget: int = BLINDSPOT_DAILY_DETECTION_BUDGET,
    ):
        if wiki_base:
            self.wiki_base = Path(wiki_base).expanduser()
        else:
            self.wiki_base = get_config().wiki_dir

        if db_path:
            self.DB_PATH = Path(db_path).expanduser()
        else:
            self.DB_PATH = get_config().database_dir / "blindspots.db"
        if initialize:
            initialize_blindspot_asset_schema(self.DB_PATH)
        self._min_admission_confidence = float(min_admission_confidence)
        self._daily_detection_budget = max(0, int(daily_detection_budget))
        self._preflight_lock = threading.RLock()
        self._preflight_cache: dict[tuple[str, str], BlindSpotCheckResult] = {}
        self._detection_budget_day = ""
        self._detection_budget_used = 0

    def _init_db(self):
        """Compatibility bootstrap for a fresh DB; never migrates an existing DB."""

        initialize_blindspot_asset_schema(self.DB_PATH)

    def schema_status(self) -> Dict[str, Any]:
        """Return schema state without creating or mutating any path."""

        return read_blindspot_schema_status(self.DB_PATH).as_dict()

    def _connect(self) -> sqlite3.Connection:
        if not self.DB_PATH.exists():
            raise BlindspotAssetSchemaError("blindspot asset store is uninitialized")
        conn = sqlite3.connect(str(self.DB_PATH), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        state = inspect_blindspot_asset_schema(conn)
        if not state.ok:
            conn.close()
            raise BlindspotAssetSchemaError(
                "blindspot asset schema is unavailable; run explicit reconciliation"
            )
        return conn

    def _read_connect(self) -> sqlite3.Connection:
        if not self.DB_PATH.exists():
            raise BlindspotAssetSchemaError("blindspot asset store is uninitialized")
        conn = sqlite3.connect(
            f"file:{self.DB_PATH}?mode=ro&immutable=1",
            uri=True,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        state = inspect_blindspot_asset_schema(conn)
        if not state.ok:
            conn.close()
            raise BlindspotAssetSchemaError(
                "blindspot asset schema is unavailable; run explicit reconciliation"
            )
        return conn

    def list_current(self, status_filter: str = "") -> List[Dict[str, Any]]:
        """Read current knowledge gaps without mutating the database."""

        with self._read_connect() as conn:
            where = "WHERE r.status=?" if status_filter else ""
            params: tuple[str, ...] = (status_filter,) if status_filter else ()
            rows = conn.execute(
                f"""SELECT r.* FROM {HEAD_TABLE} h
                    JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                    {where}
                    ORDER BY r.detected_at DESC, r.asset_id""",  # nosec B608
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def status_counts(self) -> Dict[str, int]:
        """Read current counts without constructing a writer or creating schema."""

        with self._read_connect() as conn:
            rows = conn.execute(f"""SELECT r.status, COUNT(*) FROM {HEAD_TABLE} h
                    JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                    GROUP BY r.status""").fetchall()  # nosec B608
        return {str(status): int(count) for status, count in rows}

    def expire_resolved_before(self, cutoff: datetime) -> int:
        """Append expiry revisions; never delete the immutable history."""

        candidates = [
            row
            for row in self.list_current(status_filter="resolved")
            if str(row.get("resolved_at") or "") < cutoff.isoformat()
        ]
        for row in candidates:
            self._append_status_revision(
                row,
                status="expired",
                ts=datetime.now(timezone.utc),
                resolution_evidence=(
                    f"retention-transition:{cutoff.isoformat()}",
                    str(row.get("revision_id") or ""),
                ),
            )
        return len(candidates)

    def check_blind_spot(
        self,
        query: str,
        session_id: Optional[str] = None,
        *,
        principal: PrincipalEnvelope | None = None,
        narrowing: AccessNarrowing | None = None,
    ) -> BlindSpotCheckResult:
        """
        搜索时检查盲点。

        Args:
            query: 用户查询
            session_id: 当前会话 ID。同一 topic 在同一 session 内只提醒一次。

        Returns:
            BlindSpotCheckResult（含降级信息与建议搜索词）
        """
        if principal is None:
            return BlindSpotCheckResult(
                reminder=None,
                suggested_query=query,
                degraded=True,
                degraded_reasons=["access principal required"],
            )
        effective_narrowing = narrowing or AccessNarrowing()
        scope = self._asset_scope(principal=principal, narrowing=effective_narrowing)
        normalized_query = self._normalize_query(query)
        if not self._is_informative_query(normalized_query):
            return BlindSpotCheckResult(reminder=None, suggested_query=query)

        cache_key = (scope.key, normalized_query)
        with self._preflight_lock:
            cached = self._preflight_cache.get(cache_key)
            if cached is not None:
                return cached
            if self._query_is_preflight_blocked(normalized_query, scope, session_id):
                result = BlindSpotCheckResult(reminder=None, suggested_query=query)
                self._preflight_cache[cache_key] = result
                return result
            if not self._reserve_detection_budget():
                return BlindSpotCheckResult(
                    reminder=None,
                    suggested_query=query,
                    degraded=True,
                    degraded_reasons=["blindspot daily detection budget exhausted"],
                )
            blindspots, degraded_notes = self._detect_blindspots(
                query,
                principal=principal,
                narrowing=effective_narrowing,
                scope=scope,
            )
        if not blindspots:
            result = BlindSpotCheckResult(
                reminder=None,
                suggested_query=query,
                degraded=bool(degraded_notes),
                degraded_reasons=degraded_notes,
            )
            with self._preflight_lock:
                self._preflight_cache[cache_key] = result
            return result

        now = datetime.now(timezone.utc)

        for bs in blindspots:
            # 检查冷却/会话去重
            if self._is_in_cooldown(bs.topic, session_id, asset_id=bs.asset_id):
                continue

            # 更新状态为 reminded，并记录 session_id
            updated = self._update_status(
                bs.topic,
                "reminded",
                now,
                session_id=session_id,
                asset_id=bs.asset_id,
            )
            if updated is None:
                continue

            result = BlindSpotCheckResult(
                reminder=updated,
                suggested_query=self._build_suggested_query(updated.topic, query),
                degraded=bool(degraded_notes),
                degraded_reasons=degraded_notes,
            )
            return result

        result = BlindSpotCheckResult(
            reminder=None,
            suggested_query=query,
            degraded=bool(degraded_notes),
            degraded_reasons=degraded_notes,
        )
        with self._preflight_lock:
            self._preflight_cache[cache_key] = result
        return result

    def mark_investigating(self, topic: str, *, asset_id: str = "") -> bool:
        """用户同意搜索/记录后，标记为 investigating。"""
        return self._set_status(topic, "investigating", asset_id=asset_id)

    def mark_ignored(self, topic: str, *, asset_id: str = "") -> bool:
        """用户忽略后，标记为 ignored 并进入 7 天冷却。"""
        return self._set_status(topic, "ignored", asset_id=asset_id)

    def mark_resolved(
        self,
        topic: str,
        resolved_by_page: Optional[str] = None,
        *,
        resolution_evidence: tuple[str, ...] = (),
        asset_id: str = "",
    ) -> bool:
        """Reject manual/self-signed closure; use ``resolve_by_wiki_page``.

        A bare CLI string or caller-generated prefix is not independent
        coverage evidence.  The compatibility method remains fail closed so
        older callers cannot silently close canonical assets.
        """

        del topic, resolved_by_page, resolution_evidence, asset_id
        return False

    def resolve_by_wiki_page(
        self,
        page_path: str,
        *,
        canonical_revision_id: str = "",
        projection_receipt_id: str = "",
        content_hash: str = "",
        coverage_evidence: tuple[Mapping[str, Any], ...] = (),
    ) -> int:
        """
        当新 wiki 页面生成时，自动关闭相关的 pending 盲区。

        Returns:
            关闭的盲区数量
        """
        resolved_count = 0
        typed_evidence: list[KnowledgeCoverageResolutionEvidence] = []
        try:
            typed_evidence = [
                KnowledgeCoverageResolutionEvidence.from_mapping(dict(item))
                for item in coverage_evidence
            ]
        except (TypeError, ValueError):
            return 0
        if (
            not all(
                value.strip()
                for value in (
                    canonical_revision_id,
                    projection_receipt_id,
                    content_hash,
                )
            )
            or not typed_evidence
        ):
            return 0
        try:
            page = Path(page_path)
            if not page.is_file():
                return 0
            actual_hash = "sha256:" + hashlib.sha256(
                read_bytes_value(page)
            ).hexdigest()
            if actual_hash != content_hash:
                return 0
            resolved_page = page.resolve(strict=True)
            if not resolved_page.is_relative_to(self.wiki_base.resolve(strict=False)):
                return 0
            # A page title or topic similarity is never resolution evidence.
            # Only an independent coverage recheck may name exact asset IDs.
            seen_asset_ids: set[str] = set()
            for evidence in typed_evidence:
                asset_id = evidence.asset_id
                if asset_id in seen_asset_ids or evidence.content_hash != content_hash:
                    continue
                seen_asset_ids.add(asset_id)
                rows = self._current_rows("", asset_id=asset_id)
                if len(rows) != 1 or str(rows[0]["status"]) not in {
                    "detected",
                    "reminded",
                    "investigating",
                    "mitigated",
                }:
                    continue
                row_scope = AssetScope(
                    scope_type=str(rows[0]["scope_type"]),
                    scope_id=str(rows[0]["scope_id"]),
                    purpose=str(rows[0]["purpose"]),
                    principal_id=str(rows[0]["principal_id"]),
                )
                if (
                    evidence.gap_revision_id != str(rows[0]["revision_id"])
                    or evidence.scope_key != row_scope.key
                ):
                    continue
                topic = str(rows[0]["topic"])
                self._append_status_revision(
                    dict(rows[0]),
                    status="resolved",
                    ts=datetime.now(timezone.utc),
                    resolution_evidence=(
                        f"coverage-recheck:{evidence.receipt_id}",
                        evidence.evidence_ref,
                        f"canonical-revision:{canonical_revision_id}",
                        f"projection-receipt:{projection_receipt_id}",
                        f"content:{content_hash}",
                        f"wiki-page:{resolved_page}",
                    ),
                )
                resolved_count += 1
                logger.info(
                    "[BlindspotDiscovery] 知识缺口 '%s' (%s) 通过覆盖复核关闭",
                    topic,
                    asset_id,
                )

            return resolved_count
        except (ValueError, TypeError) as e:
            logger.warning("[BlindspotDiscovery] 按 wiki 页面关闭盲区失败: %s", e, exc_info=True)
            return 0

    def get_weekly_summary(self) -> List[Dict]:
        """获取本周盲点汇总（供周报使用）"""
        week_ago = (datetime.now(timezone.utc) - timedelta(days=WEEK_AGO_DAYS)).isoformat()

        with self._read_connect() as conn:
            cursor = conn.execute(
                f"""
                SELECT r.topic, r.description, r.confidence, r.status,
                       r.detected_at, r.reminded_at, r.resolution_evidence_json,
                       r.asset_id, r.revision_id, r.dimension, r.scope_type, r.scope_id
                FROM {HEAD_TABLE} h
                JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                WHERE r.detected_at >= ?
                   OR (r.status IN ('detected', 'reminded', 'investigating') AND r.detected_at < ?)
                ORDER BY r.confidence DESC
            """,  # nosec B608
                (week_ago, week_ago),
            )

            return [dict(row) for row in cursor.fetchall()]

    def record_feedback(self, topic: str, action: str) -> None:
        """
        记录用户反馈。

        Args:
            topic: 盲点主题
            action: resolved / mitigated / ignored
        """
        if action == "resolved":
            logger.info(
                "[BlindspotDiscovery] resolved feedback requires typed resolution evidence: %s",
                topic,
            )
        elif action == "ignored":
            self.mark_ignored(topic)
        else:
            self._set_status(topic, action)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _asset_scope(
        self,
        *,
        principal: PrincipalEnvelope,
        narrowing: AccessNarrowing,
    ) -> AssetScope:
        if narrowing.session_id:
            return AssetScope(
                scope_type="session",
                scope_id=narrowing.session_id,
                purpose="knowledge_coverage_assistance",
                principal_id=principal.principal_id,
            )
        project = narrowing.project
        if not project and len(principal.allowed_projects) == 1:
            project = next(iter(principal.allowed_projects))
        if project:
            return AssetScope(
                scope_type="project",
                scope_id=project,
                purpose="knowledge_coverage_assistance",
                principal_id=principal.principal_id,
            )
        vault_digest = hashlib.sha256(
            str(self.wiki_base.resolve(strict=False)).encode("utf-8")
        ).hexdigest()[:24]
        return AssetScope(
            scope_type="vault",
            scope_id=f"vault:{vault_digest}",
            purpose="knowledge_coverage_assistance",
            principal_id=principal.principal_id,
        )

    @staticmethod
    def _query_evidence_ref(query: str, scope: AssetScope) -> str:
        digest = hashlib.sha256(
            json.dumps(
                {"query": query, "scope": scope.key, "authorized_hits": 0},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return f"authorized-context-search:sha256:{digest}"

    @staticmethod
    def _reminder_from_gap(gap: KnowledgeCoverageGap) -> BlindSpotReminder:
        return BlindSpotReminder(
            topic=gap.topic,
            description=gap.description,
            confidence=gap.confidence,
            status=gap.status,
            detected_at=gap.detected_at,
            asset_id=gap.asset_id,
            revision_id=gap.revision_id,
            dimension=gap.dimension,
            evidence_refs=gap.evidence_refs,
            scope_type=gap.scope.scope_type,
            scope_id=gap.scope.scope_id,
            purpose=gap.scope.purpose,
            principal_id=gap.scope.principal_id,
            expires_at=gap.expires_at,
            resolution_condition=gap.resolution_condition,
            consumers=gap.consumers,
        )

    def _detect_blindspots(
        self,
        query: str,
        *,
        principal: PrincipalEnvelope | None,
        narrowing: AccessNarrowing | None = None,
        scope: AssetScope | None = None,
    ) -> tuple[List[BlindSpotReminder], List[str]]:
        """从知识图谱和画像检测盲点（降级策略：组件缺失时不静默失败）

        Returns:
            (reminders, degraded_notes)
        """
        results: List[BlindSpotReminder] = []
        degraded_notes: List[str] = []
        if principal is None:
            return results, ["access principal required"]

        # 1. Search only through the authorization-first context seam.  This
        # prevents the blindspot decision itself from becoming an oracle for
        # private embeddings, graph relations, or Wiki bodies.
        authorized_hits = []
        retrieval_available = True
        try:
            from core.app.context_search import ContextAwareSearch

            authorized_hits = ContextAwareSearch(wiki_base=str(self.wiki_base)).search(
                query,
                limit=5,
                principal=principal,
                narrowing=narrowing or AccessNarrowing(),
            )
        except (ImportError, RuntimeError, OSError, ValueError) as e:
            retrieval_available = False
            degraded_notes.append(f"授权知识搜索不可用: {e}")

        if retrieval_available:
            scope = scope or self._asset_scope(
                principal=principal, narrowing=narrowing or AccessNarrowing()
            )
            evidence_ref = self._query_evidence_ref(query, scope)
            expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
            issues = KnowledgeImmuneSystem(wiki_base=str(self.wiki_base)).detect_knowledge_gaps(
                pages=[],
                query_observation=QueryCoverageObservation(
                    query=query,
                    authorized_hit_count=len(authorized_hits),
                    evidence_ref=evidence_ref,
                    scope_key=scope.key,
                ),
            )
            for issue in issues:
                if (
                    issue.issue_type != "knowledge_gap"
                    or issue.dimension != "missing_topic"
                    or issue.scope_key != scope.key
                ):
                    continue
                confidence = self._admission_confidence(issue)
                if confidence < self._min_admission_confidence:
                    continue
                gap = KnowledgeCoverageGap.create(
                    topic=issue.page,
                    dimension=issue.dimension,
                    description=issue.description,
                    evidence_refs=issue.evidence_refs,
                    scope=scope,
                    confidence=confidence,
                    expires_at=expires_at,
                )
                results.append(self._reminder_from_gap(gap))

        if degraded_notes:
            logger.info("[BlindspotDiscovery] 降级运行: %s", "; ".join(degraded_notes))

        # 保存新发现的盲点
        persisted: List[BlindSpotReminder] = []
        for reminder in results:
            persisted.append(self._upsert_blindspot(reminder))

        return persisted, degraded_notes

    @staticmethod
    def _row_to_reminder(row: Mapping[str, Any]) -> BlindSpotReminder:
        return BlindSpotReminder(
            topic=str(row["topic"]),
            description=str(row["description"]),
            confidence=float(row["confidence"]),
            status=str(row["status"]),
            detected_at=str(row["detected_at"]),
            reminded_at=str(row["reminded_at"] or "") or None,
            asset_id=str(row["asset_id"]),
            revision_id=str(row["revision_id"]),
            dimension=str(row["dimension"]),
            evidence_refs=tuple(json.loads(str(row["evidence_refs_json"]))),
            scope_type=str(row["scope_type"]),
            scope_id=str(row["scope_id"]),
            purpose=str(row["purpose"]),
            principal_id=str(row["principal_id"]),
            expires_at=str(row["expires_at"]),
            resolution_condition=str(row["resolution_condition"]),
            consumers=tuple(json.loads(str(row["consumers_json"]))),
        )

    def _upsert_blindspot(self, bs: BlindSpotReminder) -> BlindSpotReminder:
        with self._connect() as conn:
            existing = conn.execute(
                f"""SELECT r.* FROM {HEAD_TABLE} h
                    JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                    WHERE h.asset_id=?""",  # nosec B608
                (bs.asset_id,),
            ).fetchone()
            if existing is not None:
                return self._row_to_reminder(existing)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO {REVISION_TABLE} (
                    revision_id, asset_id, revision_number, topic, normalized_topic,
                    dimension, description, confidence, status, detected_at,
                    reminded_at, last_reminded_at, last_session_id, expires_at,
                    scope_type, scope_id, purpose, principal_id,
                    evidence_refs_json, resolution_condition,
                    resolution_evidence_json, resolved_at,
                    supersedes_revision_id, consumers_json, created_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, '', '', '', ?, ?, ?, ?, ?, ?, ?,
                          '[]', '', NULL, ?, ?)""",  # nosec B608
                (
                    bs.revision_id,
                    bs.asset_id,
                    bs.topic,
                    bs.topic.casefold(),
                    bs.dimension,
                    bs.description,
                    bs.confidence,
                    bs.status,
                    bs.detected_at,
                    bs.expires_at,
                    bs.scope_type,
                    bs.scope_id,
                    bs.purpose,
                    bs.principal_id,
                    json.dumps(list(bs.evidence_refs), ensure_ascii=False),
                    bs.resolution_condition,
                    json.dumps(list(bs.consumers), ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                f"INSERT INTO {HEAD_TABLE}(asset_id, revision_id) VALUES (?, ?)",  # nosec B608
                (bs.asset_id, bs.revision_id),
            )
            conn.commit()
            return bs

    def _current_rows(self, topic: str, *, asset_id: str = "") -> list[sqlite3.Row]:
        with self._connect() as conn:
            if asset_id:
                identity_clause = "r.asset_id=?"
                parameters: tuple[str, ...] = (asset_id,)
            else:
                identity_clause = "r.normalized_topic=?"
                parameters = (topic.casefold(),)
            return list(
                conn.execute(
                    f"""SELECT r.* FROM {HEAD_TABLE} h
                        JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                        WHERE {identity_clause}
                        ORDER BY r.asset_id""",  # nosec B608
                    parameters,
                ).fetchall()
            )

    def _append_status_revision(
        self,
        row: Mapping[str, Any],
        *,
        status: str,
        ts: datetime,
        session_id: str = "",
        resolution_evidence: tuple[str, ...] = (),
    ) -> BlindSpotReminder:
        revision_number = int(row["revision_number"]) + 1
        revision_id = f"{row['asset_id']}:r{revision_number}"
        reminded_at = ts.isoformat() if status == "reminded" else str(row["reminded_at"] or "")
        last_reminded_at = (
            ts.isoformat()
            if status in {"reminded", "ignored"}
            else str(row["last_reminded_at"] or "")
        )
        resolved_at = (
            ts.isoformat() if status in {"resolved", "mitigated"} else str(row["resolved_at"] or "")
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT INTO {REVISION_TABLE} (
                    revision_id, asset_id, revision_number, topic, normalized_topic,
                    dimension, description, confidence, status, detected_at,
                    reminded_at, last_reminded_at, last_session_id, expires_at,
                    scope_type, scope_id, purpose, principal_id,
                    evidence_refs_json, resolution_condition,
                    resolution_evidence_json, resolved_at,
                    supersedes_revision_id, consumers_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",  # nosec B608
                (
                    revision_id,
                    row["asset_id"],
                    revision_number,
                    row["topic"],
                    row["normalized_topic"],
                    row["dimension"],
                    row["description"],
                    row["confidence"],
                    status,
                    row["detected_at"],
                    reminded_at,
                    last_reminded_at,
                    session_id or str(row["last_session_id"] or ""),
                    row["expires_at"],
                    row["scope_type"],
                    row["scope_id"],
                    row["purpose"],
                    row["principal_id"],
                    row["evidence_refs_json"],
                    row["resolution_condition"],
                    json.dumps(list(resolution_evidence), ensure_ascii=False),
                    resolved_at,
                    row["revision_id"],
                    row["consumers_json"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                f"UPDATE {HEAD_TABLE} SET revision_id=? WHERE asset_id=?",  # nosec B608
                (revision_id, row["asset_id"]),
            )
            conn.commit()
            next_row = dict(row)
            next_row.update(
                {
                    "revision_id": revision_id,
                    "revision_number": revision_number,
                    "status": status,
                    "reminded_at": reminded_at,
                    "last_reminded_at": last_reminded_at,
                    "last_session_id": session_id or row["last_session_id"],
                    "resolved_at": resolved_at,
                    "resolution_evidence_json": json.dumps(list(resolution_evidence)),
                }
            )
            return self._row_to_reminder(next_row)

    def _update_status(
        self,
        topic: str,
        status: str,
        ts: datetime,
        session_id: Optional[str] = None,
        asset_id: str = "",
    ) -> BlindSpotReminder | None:
        rows = self._current_rows(topic, asset_id=asset_id)
        if len(rows) != 1:
            return None
        return self._append_status_revision(
            dict(rows[0]),
            status=status,
            ts=ts,
            session_id=session_id or "",
        )

    def _set_status(self, topic: str, status: str, *, asset_id: str = "") -> bool:
        """仅更新状态，不触碰 reminded_at。"""
        try:
            rows = self._current_rows(topic, asset_id=asset_id)
            if len(rows) != 1:
                return False
            self._append_status_revision(
                dict(rows[0]),
                status=status,
                ts=datetime.now(timezone.utc),
            )
            return True
        except (sqlite3.Error, BlindspotAssetSchemaError) as e:
            logger.warning("[BlindspotDiscovery] 设置状态失败: %s", e)
            return False

    @staticmethod
    def _normalize_query(query: str) -> str:
        """Return the stable identity used by pre-billable admission checks."""

        return " ".join(unicodedata.normalize("NFKC", query).casefold().split())

    @staticmethod
    def _is_informative_query(normalized_query: str) -> bool:
        return bool(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", normalized_query))

    @staticmethod
    def _query_topics(normalized_query: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    token
                    for token in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]{2,}", normalized_query)
                    if len(token) >= 2
                }
            )
        )

    def _query_is_preflight_blocked(
        self, normalized_query: str, scope: AssetScope, session_id: Optional[str]
    ) -> bool:
        """Reject known cooldown/resolved assets before search can bill work."""

        topics = self._query_topics(normalized_query)
        if not topics:
            return True
        placeholders = ", ".join("?" for _ in topics)
        try:
            with self._read_connect() as conn:
                rows = conn.execute(
                    f"""SELECT r.* FROM {HEAD_TABLE} h
                        JOIN {REVISION_TABLE} r ON r.revision_id=h.revision_id
                        WHERE r.scope_type=? AND r.scope_id=?
                          AND r.normalized_topic IN ({placeholders})""",  # nosec B608
                    (scope.scope_type, scope.scope_id, *topics),
                ).fetchall()
        except (sqlite3.Error, BlindspotAssetSchemaError):
            # The caller cannot safely prove an admission decision without the
            # typed store, so do not spend on speculative coverage detection.
            return True
        return any(self._row_is_in_cooldown_or_terminal(dict(row), session_id) for row in rows)

    def _row_is_in_cooldown_or_terminal(
        self, row: Mapping[str, Any], session_id: Optional[str]
    ) -> bool:
        status = str(row.get("status") or "")
        if status in {"resolved", "mitigated", "expired"}:
            return True
        last_reminded = str(row.get("last_reminded_at") or "")
        if session_id and str(row.get("last_session_id") or "") == session_id:
            return True
        if not last_reminded:
            return False
        try:
            timestamp = datetime.fromisoformat(last_reminded)
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - timestamp).total_seconds()
        except ValueError:
            return True
        if status == "ignored":
            return elapsed < self.IGNORE_COOLDOWN_SEC
        return not session_id and elapsed < self.FALLBACK_COOLDOWN_SEC

    def _reserve_detection_budget(self) -> bool:
        """Reserve one local detection attempt before any billable search seam."""

        today = datetime.now(timezone.utc).date().isoformat()
        if self._detection_budget_day != today:
            self._detection_budget_day = today
            self._detection_budget_used = 0
        if self._detection_budget_used >= self._daily_detection_budget:
            return False
        self._detection_budget_used += 1
        return True

    @staticmethod
    def _admission_confidence(issue: Any) -> float:
        """Score only evidence the canonical detector actually emitted."""

        evidence_count = len(tuple(getattr(issue, "evidence_refs", ())))
        return min(0.80, 0.55 + (0.05 * evidence_count))

    def _is_in_cooldown(
        self,
        topic: str,
        session_id: Optional[str] = None,
        *,
        asset_id: str = "",
    ) -> bool:
        now = datetime.now(timezone.utc)
        rows = self._current_rows(topic, asset_id=asset_id)
        if len(rows) != 1:
            return len(rows) > 1
        row = rows[0]

        status = str(row["status"])
        last_reminded = str(row["last_reminded_at"] or "")
        last_session_id_db = str(row["last_session_id"] or "")

        # 已解决的不提醒
        if status in ("resolved", "mitigated"):
            return True

        # 忽略的 7 天冷却
        if status == "ignored":
            if last_reminded:
                try:
                    reminded_at = datetime.fromisoformat(last_reminded)
                    if reminded_at.tzinfo is None:
                        reminded_at = reminded_at.replace(tzinfo=timezone.utc)
                    elapsed = (now - reminded_at).total_seconds()
                    if elapsed < self.IGNORE_COOLDOWN_SEC:
                        return True
                except ValueError:
                    logger.warning("[blindspot_discovery] ValueError suppressed", exc_info=True)
            return False

        # 有 session_id 时：同会话内不重复提醒
        if session_id and last_session_id_db == session_id:
            return True

        # 无 session_id 时：使用兜底短冷却
        if not session_id and last_reminded:
            try:
                reminded_at = datetime.fromisoformat(last_reminded)
                if reminded_at.tzinfo is None:
                    reminded_at = reminded_at.replace(tzinfo=timezone.utc)
                elapsed = (now - reminded_at).total_seconds()
                if elapsed < self.FALLBACK_COOLDOWN_SEC:
                    return True
            except ValueError:
                logger.warning("[blindspot_discovery] ValueError suppressed", exc_info=True)

        return False

    def _build_suggested_query(self, topic: str, original_query: str) -> str:
        """为 AI 搜索构建建议查询词。"""
        # 如果 topic 已经在 original_query 里，直接用 original_query
        if topic.lower() in original_query.lower():
            return original_query
        return f"{topic} {original_query}".strip()
