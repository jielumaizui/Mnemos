"""
Observation Engine — 观察引擎

协调数据源读取、维度提取、存储的完整流程。

使用方式：
    engine = ObservationEngine()
    batch = engine.run()           # 提取观察
    engine.persist(batch)          # 存入数据库
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, cast

from core.cognitive.auto_calibration import CalibrationEngine, CalibrationReport
from core.cognitive.calibration_record import CalibrationRecordStore
from core.cognitive.dimension_extractors import ALL_EXTRACTORS
from core.cognitive.models import (
    Dimension,
    Observation,
    ObservationBatch,
    SourceType,
)
from core.cognitive.access_control import make_cognitive_access_envelope
from core.cognitive.observation_store import (
    ObservationIndex,
    ObservationStore,
    _extract_observations,
)
from core.cognitive.sources import SourceItem, SourceReader
from core.cognitive.state_contract import sha256_json
from core.cognitive.state_store import CognitiveStateStore
from core.cognitive.wiki_exporter import WikiExporter
from core.config import get_config
from core.evidence.evidence_graph import EvidenceGraph
from core.privacy.content_redaction import redact_persistence_value

# Constants extracted from magic numbers
# A full Raw replay may process tens of thousands of immutable revisions.  The
# returned batch keeps enough detail for normal callers while exposing the
# exact total separately on ObservationBatch.
MAX_BACKFILL_RETURNED_OBSERVATIONS = 1000
# Calibration evaluates a page as a cross-source evidence set.  Keep a full
# replay page deliberately smaller than the daemon's ordinary incremental
# intake so a historical mixed backlog remains memory-bounded without
# weakening provenance, extraction, or calibration.
FULL_BACKFILL_PAGE_ITEMS = 64

logger = logging.getLogger(__name__)


def _projection_max_file_bytes() -> int:
    """Configured L3 projection page cap; 0 keeps the legacy unbounded export."""

    value = get_config().get("observation.projection_max_file_bytes")
    return max(0, int(value or 0))


def canonical_raw_engine_kwargs(config: Any) -> Dict[str, Any]:
    """Return fail-closed canonical Raw options for a runtime config."""
    database_dir = getattr(config, "database_dir", None)
    if not isinstance(database_dir, (str, Path)):
        raise TypeError("Observation runtime config must expose database_dir")
    return {
        "raw_events_db": str(Path(database_dir) / "raw_events.db"),
        "require_canonical_raw": True,
    }


class ObservationEngine:
    """
    Observation Engine

    从 L1 (raw) + L2 (wiki) 提取客观观察，输出 ObservationBatch。
    持久化顺序固定为：Index (SQLite) → Wiki (只读投影) → Evidence Graph。
    """

    def __init__(
        self,
        wiki_dir: Optional[str] = None,
        raw_events_db: Optional[str] = None,
        require_canonical_raw: bool = False,
        store: Optional[ObservationStore] = None,
        index: Optional[ObservationIndex] = None,
        export_to_wiki: bool = True,
        evidence_graph: Optional[EvidenceGraph] = None,
        cognitive_state_store: Optional[CognitiveStateStore] = None,
        calibration_engine: Optional[CalibrationEngine] = None,
    ):
        """
        初始化引擎

        Args:
            wiki_dir: L2 wiki 仓库路径
            raw_events_db: canonical Raw database; when present it is the
                primary L1 source and Markdown is never a fallback
            require_canonical_raw: fail closed if the canonical Raw source is
                unavailable instead of silently parsing a vault projection
            store: ObservationStore 实例
            index: ObservationIndex 实例（推荐）
            export_to_wiki: 是否导出为 Obsidian Markdown
            evidence_graph: EvidenceGraph 实例，默认新建
        """
        if index:
            self.index = index
            self.store = index.store
        else:
            self.store = store or ObservationStore()
            self.index = ObservationIndex(self.store)
        configured_raw_db = Path(raw_events_db).expanduser() if raw_events_db else None
        self.raw_events_db = configured_raw_db
        self.reader = SourceReader(
            wiki_dir=wiki_dir,
            raw_events_db=str(self.raw_events_db) if self.raw_events_db else None,
            require_canonical_raw=bool(require_canonical_raw or configured_raw_db),
        )
        self.extractors = ALL_EXTRACTORS
        self.wiki_dir = wiki_dir
        self.export_to_wiki = export_to_wiki and wiki_dir is not None
        self.evidence_graph = evidence_graph or EvidenceGraph()
        self.calibration_engine = calibration_engine or CalibrationEngine()
        if cognitive_state_store is not None:
            self.calibration_records: Optional[CalibrationRecordStore] = (
                CalibrationRecordStore(cognitive_state_store)
            )
        else:
            state_db = self.store.db_path.parent / "producer_consumer_ledger.db"
            self.calibration_records = (
                CalibrationRecordStore(CognitiveStateStore(state_db))
                if state_db.is_file()
                else None
            )

    def run(self, persist: bool = True) -> ObservationBatch:
        """
        执行完整的观察提取流程

        Args:
            persist: 是否将结果存入数据库

        Returns:
            ObservationBatch: 提取结果
        """
        logger.info("ObservationEngine: starting full extraction...")

        # Canonical Raw is lossless and may be much larger than available RAM.
        # Full production entrypoints therefore replay cursor-bounded fair
        # pages rather than calling list(reader.read_all()).  Explicitly
        # injected readers retain their bounded one-batch test seam.
        if self.raw_events_db is not None and isinstance(self.reader, SourceReader):
            return self.run_full_backfill(persist=persist)

        # 1. 读取所有来源
        items = list(self.reader.read_all())
        return self._run_extraction(items, persist=persist, is_incremental=False)

    def run_full_backfill(self, persist: bool = True) -> ObservationBatch:
        """Replay every current canonical Raw revision in durable fair pages.

        The replay starts from empty source cursors, but advances a cursor only
        after a Raw revision has an exact Observation edge or a typed terminal
        receipt.  It is therefore safe to resume after an extractor or writer
        failure without synthesizing bulk success evidence.
        """
        if not isinstance(self.reader, SourceReader):
            items = list(self.reader.read_all())
            return self._run_extraction(items, persist=persist, is_incremental=False)

        cursors: Dict[str, Dict[str, str]] = {}
        aggregate = ObservationBatch(extraction_status="running")
        aggregate.persist_stats = {
            "inserted": 0,
            "updated": 0,
            "pages": 0,
            "changed_dimensions": set(),
        }
        self.reader.set_incremental_cursors(cursors)

        while True:
            items = list(self.reader.read_page(max_items=FULL_BACKFILL_PAGE_ITEMS))
            if not items:
                break

            before_cursors = {name: dict(token) for name, token in cursors.items()}
            page = self._run_extraction(
                items,
                persist=persist,
                is_incremental=True,
                export_projection=False,
            )
            self._merge_backfill_page(aggregate, page)
            aggregate.persist_stats["pages"] += 1

            if persist:
                cursors = self.store.get_source_cursors()
            else:
                cursors = self._advance_local_source_cursors(cursors, items)
            if cursors == before_cursors:
                raise RuntimeError(
                    "full observation replay did not advance a source cursor after a page"
                )
            self.reader.set_incremental_cursors(cursors)

        if aggregate.source_count == 0:
            aggregate.extraction_status = "skipped"
            aggregate.extraction_reason = "no_source_items"
        elif aggregate.total_observations:
            aggregate.extraction_status = "ok"
            aggregate.extraction_reason = "observations_extracted"
        else:
            aggregate.extraction_status = "empty"
            aggregate.extraction_reason = "no_observations_extracted"

        changed_dimensions = aggregate.persist_stats["changed_dimensions"]
        if persist and changed_dimensions and self.export_to_wiki and self.wiki_dir:
            self._reexport_all(dimensions=cast(Set[str], changed_dimensions))
        return aggregate

    @staticmethod
    def _advance_local_source_cursors(
        cursors: Dict[str, Dict[str, str]], items: List[SourceItem]
    ) -> Dict[str, Dict[str, str]]:
        """Advance ephemeral dry-run cursors without writing ObservationStore."""
        advanced = {name: dict(token) for name, token in cursors.items()}
        for item in items:
            if not item.source_stream or not item.cursor_token:
                continue
            previous = advanced.get(item.source_stream, {})
            if not previous or SourceReader._cursor_allows(item.cursor_token, previous):
                advanced[item.source_stream] = dict(item.cursor_token)
        return advanced

    @staticmethod
    def _merge_backfill_page(aggregate: ObservationBatch, page: ObservationBatch) -> None:
        """Merge page facts while retaining only a bounded detail sample."""
        aggregate.source_count += int(page.source_count)
        aggregate.observation_total += page.total_observations
        if page.period_start and (
            aggregate.period_start is None
            or ObservationEngine._utc_timestamp(page.period_start)
            < ObservationEngine._utc_timestamp(aggregate.period_start)
        ):
            aggregate.period_start = page.period_start
        if page.period_end and (
            aggregate.period_end is None
            or ObservationEngine._utc_timestamp(page.period_end)
            > ObservationEngine._utc_timestamp(aggregate.period_end)
        ):
            aggregate.period_end = page.period_end
        for dimension, count in page.dimension_counts.items():
            aggregate.dimension_counts[dimension] = (
                aggregate.dimension_counts.get(dimension, 0) + int(count)
            )
        for key in ("inserted", "updated"):
            aggregate.persist_stats[key] += int(page.persist_stats.get(key, 0) or 0)
        aggregate.persist_stats["changed_dimensions"].update(
            page.persist_stats.get("changed_dimensions") or set()
        )

        remaining = MAX_BACKFILL_RETURNED_OBSERVATIONS - len(aggregate.observations)
        if remaining > 0:
            aggregate.observations.extend(page.observations[:remaining])
        if len(page.observations) > max(remaining, 0):
            aggregate.observations_truncated = True

    @staticmethod
    def _utc_timestamp(value: datetime) -> float:
        """Compare timezone-free and timezone-aware source times consistently."""
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).timestamp()

    # 增量模式安全上限：单次最多处理 1000 个来源，最大回退 7 天
    MAX_INCREMENTAL_ITEMS = 1000
    MAX_INCREMENTAL_LOOKBACK_HOURS = 24 * 7

    def run_incremental(self, since: datetime, persist: bool = True) -> ObservationBatch:
        """
        增量提取：只处理 since 之后的新内容

        策略：
        1. 只读取 since 之后的新文件（带 max_items 和 lookback 上限）
        2. 提取新 Observation
        3. 与已有 Observation 合并存储（存储层去重）
        4. 仅当实际有 Observation 插入/更新时，才重新导出完整 Wiki 报告
        """
        logger.info("ObservationEngine: starting incremental extraction since %s...", since)

        if isinstance(self.reader, SourceReader):
            self.reader.set_incremental_cursors(self.store.get_source_cursors())

        # 1. 增量读取（限制数量和时间窗口，防止长期停机后追平拖垮系统）
        items = list(
            self.reader.read_since(
                since,
                max_items=self.MAX_INCREMENTAL_ITEMS,
                max_lookback_hours=self.MAX_INCREMENTAL_LOOKBACK_HOURS,
            )
        )
        if not items:
            logger.info("No new source items since %s", since)
            # P108: 无新内容时不全量重导出，避免无效 IO
            return ObservationBatch(
                source_count=0,
                extraction_status="skipped",
                extraction_reason="no_new_source_items_since",
            )

        return self._run_extraction(items, persist=persist, is_incremental=True)

    def _run_extraction(
        self,
        items: List[SourceItem],
        persist: bool = True,
        is_incremental: bool = False,
        export_projection: bool = True,
    ) -> ObservationBatch:
        """
        核心提取逻辑（被 run 和 run_incremental 共用）
        """
        batch = ObservationBatch(
            source_count=len(items),
            period_start=datetime.now(),
            period_end=datetime.now(),
            extraction_status="running",
        )

        if not items:
            logger.warning("No source items found. Returning empty batch.")
            batch.extraction_status = "skipped"
            batch.extraction_reason = "no_source_items"
            return batch

        self._set_batch_period(batch, items)
        self._extract_dimensions(batch, items)
        self._append_source_signals(batch, items)

        logger.info(
            f"Extraction complete: {len(batch.observations)} observations "
            f"across {len(batch.dimension_counts)} dimensions"
        )

        persist_stats = self._persist(batch) if persist else {"inserted": 0, "updated": 0}
        batch.persist_stats = dict(persist_stats)
        persisted_observations = list(batch.observations)
        if persist:
            persisted_ids = persist_stats.get("persisted_observation_ids")
            if not isinstance(persisted_ids, set):
                raise RuntimeError("ObservationStore did not return persisted observation IDs")
            persisted_observations = [
                observation
                for observation in batch.observations
                if observation.id in persisted_ids
            ]
        calibration_reports, calibration_changed_dimensions = (
            self._calibrate_and_commit(
                persisted_observations,
                batch.observations,
                items,
                persist=persist,
            )
        )
        changed_dimensions = persist_stats.get("changed_dimensions")
        if isinstance(changed_dimensions, set):
            changed_dimensions.update(calibration_changed_dimensions)
        if batch.observations:
            batch.extraction_status = "ok"
            batch.extraction_reason = "observations_extracted"
        else:
            batch.extraction_status = "empty"
            batch.extraction_reason = "no_observations_extracted"
        terminal_raw_revisions: Set[str] = set()
        if persist:
            terminal_raw_revisions = self._record_canonical_raw_terminals(
                persisted_observations, items
            )
        if persist and persisted_observations:
            # 记录 Observation → Source 的证据链
            self._record_observation_evidence(persisted_observations, items)

        if persist and is_incremental:
            self._advance_source_cursors(items, terminal_raw_revisions)

        if export_projection:
            self._export_projection(
                batch,
                calibration_reports,
                persist_stats,
                is_incremental,
                persist=persist,
            )

        return batch

    def _set_batch_period(self, batch: ObservationBatch, items: List[SourceItem]):
        """根据 item 时间戳更新 batch 的统计周期。"""
        timestamps = [item.timestamp for item in items if item.timestamp]
        if timestamps:
            batch.period_start = min(timestamps)
            batch.period_end = max(timestamps)

    def _extract_dimensions(self, batch: ObservationBatch, items: List[SourceItem]):
        """Extract canonical Raw per revision and other declared sources by batch."""
        for extractor in self.extractors:
            logger.info(f"Extracting dimension: {extractor.dimension.value}")

        canonical_raw_items = [
            item
            for item in items
            if item.source_type == "raw" and bool(item.raw_revision_id)
        ]
        other_items = [item for item in items if not self._is_canonical_raw_item(item)]

        # An aggregate Observation cannot truthfully name one Raw revision as
        # its source.  Process one immutable canonical revision at a time and
        # force the extractor result back onto that exact source identity.
        # ``fail_fast`` is essential: an extractor failure leaves the revision
        # retryable rather than manufacturing a no-observation terminal.
        for item in canonical_raw_items:
            for obs in _extract_observations([item], self.extractors, fail_fast=True):
                obs.source_type = SourceType.RAW
                obs.source_path = item.file_path
                obs.source_id = item.raw_revision_id
                obs.content_source = item.content_source
                obs.user_intent_signal = item.user_intent
                obs.source_span_ids = list(item.source_span_ids)
                obs.access_control = self._canonical_raw_observation_access(
                    item,
                    observation_id=obs.id,
                )
                batch.add(obs)

        for obs in _extract_observations(other_items, self.extractors):
            if obs.source_path.startswith("aggregated:"):
                obs.source_span_ids = sorted(
                    {span for item in other_items for span in item.source_span_ids}
                )
            else:
                matching = [item for item in other_items if item.file_path == obs.source_path]
                obs.source_span_ids = sorted(
                    {span for item in matching for span in item.source_span_ids}
                )
            batch.add(obs)

    @staticmethod
    def _is_canonical_raw_item(item: SourceItem) -> bool:
        return item.source_type == "raw" and bool(item.raw_revision_id)

    @staticmethod
    def _canonical_raw_observation_access(
        item: SourceItem,
        *,
        observation_id: str,
    ) -> Dict[str, Any]:
        """Derive an Observation ACL from one immutable canonical Raw turn.

        The engine deliberately extracts canonical Raw one revision at a time,
        so a resulting measurement has exactly one source ACL.  Incomplete or
        ambiguous sources return an empty mapping and become
        restricted-unknown at the ObservationStore boundary.
        """

        agent = str(item.source_agent or "").strip().lower()
        session_id = str(item.session_id or "").strip()
        raw_ref = str(item.raw_revision_id or item.raw_event_id or "").strip()
        if not agent or not session_id or not raw_ref:
            return {}
        project = str(item.frontmatter.get("project") or "").strip().lower()
        source_hash = str(item.raw_content_hash or item.source_content_hash or raw_ref)
        return make_cognitive_access_envelope(
            owner_principal_id=f"source-agent:{agent}",
            owner_agent=agent,
            scope_type="observation",
            scope_id=str(observation_id),
            session_id=session_id,
            project=project,
            purposes=(
                "observation_read",
                "preflight_inject",
                "reflection_read",
                "reflection_feedback",
                "reflection_prompt",
                "reflection_experience_read",
                "reflection_export",
            ),
            consent_provenance_refs=(f"raw:{raw_ref}",),
            sensitivity="sensitive",
            retention_policy="observation_retention",
            source_acl_lineage=(source_hash,),
            visibility="agent",
        )

    def _record_canonical_raw_terminals(
        self,
        observations: List[Observation],
        items: List[SourceItem],
    ) -> Set[str]:
        """Write exact Raw edge or a typed intentional no-observation receipt.

        The cursor is deliberately not advanced here.  The caller advances it
        only after this method returns successfully, which keeps failed
        extractors/writers retryable and prevents a terminal receipt from
        being inferred from a batch count.
        """
        raw_items = [item for item in items if self._is_canonical_raw_item(item)]
        if not raw_items:
            return set()
        if not self.raw_events_db:
            raise RuntimeError("canonical Raw item exists without a canonical Raw database")

        from core.sync_framework.raw_event_store import RawEventStore

        store = RawEventStore(db_path=self.raw_events_db)
        terminal_revisions: Set[str] = set()
        try:
            for item in raw_items:
                revision_id = item.raw_revision_id
                matching = [
                    observation
                    for observation in observations
                    if observation.source_type == SourceType.RAW
                    and observation.source_id == revision_id
                ]
                visible_text = item.content
                if matching:
                    if not visible_text.strip():
                        raise RuntimeError(
                            "an empty canonical Raw revision cannot create an observation edge"
                        )
                    for observation in matching:
                        store.record_provenance_edge(
                            source_revision_id=revision_id,
                            span_start=0,
                            span_end=len(visible_text),
                            consumer_type="observation",
                            consumer_id=observation.id,
                        )
                elif not visible_text.strip():
                    store.record_intentional_no_observation(
                        source_revision_id=revision_id,
                        reason="empty_visible_content",
                    )
                else:
                    store.record_intentional_no_observation(
                        source_revision_id=revision_id,
                        reason="no_supported_signal",
                    )
                terminal_revisions.add(revision_id)
        finally:
            store.close()
        return terminal_revisions

    def _advance_source_cursors(
        self,
        items: List[SourceItem],
        terminal_raw_revisions: Set[str],
    ) -> None:
        """Advance only source work that reached its durable terminal state."""
        terminal_items: List[SourceItem] = []
        for item in items:
            if not item.source_stream or not item.cursor_token:
                continue
            if self._is_canonical_raw_item(item) and item.raw_revision_id not in terminal_raw_revisions:
                continue
            terminal_items.append(item)

        by_stream: Dict[str, List[SourceItem]] = {}
        for item in terminal_items:
            by_stream.setdefault(item.source_stream, []).append(item)
        for source_stream, source_items in by_stream.items():
            latest = source_items[0]
            for candidate in source_items[1:]:
                if SourceReader._cursor_allows(candidate.cursor_token, latest.cursor_token):
                    latest = candidate
            self.store.set_source_cursor(source_stream, latest.cursor_token)

    def _calibrate_and_commit(
        self,
        observations: List[Observation],
        all_observations: List[Observation],
        items: List[SourceItem],
        *,
        persist: bool,
    ) -> tuple[Dict[str, CalibrationReport], Set[str]]:
        """Calibrate stable Observation IDs and persist before binding posterior."""

        calibration_reports: Dict[str, CalibrationReport] = {}
        changed_dimensions: Set[str] = set()
        if not observations:
            return calibration_reports, changed_dimensions
        if persist and self.calibration_records is None:
            bound_observation_ids = [
                observation.id
                for observation in observations
                if any(
                    (
                        observation.calibration_revision_id,
                        observation.calibration_input_hash,
                        observation.calibration_spec_hash,
                        observation.calibration_record_hash,
                    )
                )
            ]
            if bound_observation_ids:
                raise RuntimeError(
                    "Canonical cognition state store unavailable; cannot replay "
                    "existing calibration bindings for Observation IDs: "
                    + ", ".join(sorted(bound_observation_ids))
                )
            logger.warning(
                "Canonical cognition state store unavailable; Observations remain at base confidence"
            )
            for observation in observations:
                observation.confidence = observation.base_confidence_value()
            return calibration_reports, changed_dimensions

        for observation in observations:
            relevant_items = self._calibration_source_items(observation, items)
            report = self.calibration_engine.calibrate(
                observation,
                all_observations,
                relevant_items,
            )
            if not persist:
                calibration_reports[observation.id] = report
                continue
            assert self.calibration_records is not None
            commit, persisted_report = self.calibration_records.commit(observation, report)
            binding = self.calibration_records.apply_to_observation(
                self.store,
                commit,
            )
            if binding["changed"]:
                changed_dimensions.add(observation.dimension.value)
            observation.confidence = persisted_report.calibrated_confidence
            observation.calibration_revision_id = commit.revision_id
            observation.calibration_input_hash = persisted_report.calculation_input_hash
            observation.calibration_spec_hash = persisted_report.validator_spec_hash
            observation.calibration_record_hash = persisted_report.calibration_record_hash
            pending = self.calibration_records.pending_commands("observation_index")
            command = pending.get(commit.revision_id)
            if command is not None:
                before_hash = sha256_json(persisted_report.input_snapshot["observation"])
                after_hash = sha256_json(
                    {
                        "observation_id": observation.id,
                        "calibration_revision_id": commit.revision_id,
                        "calibration_record_hash": persisted_report.calibration_record_hash,
                        "posterior": persisted_report.calibrated_confidence,
                    }
                )
                self.calibration_records.record_command_effect(
                    str(command["command_id"]),
                    target_effect_id=f"observation-calibration:{observation.id}:{commit.revision_id}",
                    before_hash=before_hash,
                    after_hash=after_hash,
                    evidence_refs=(
                        f"observation:{observation.id}",
                        f"calibration-revision:{commit.revision_id}",
                    ),
                )
            calibration_reports[observation.id] = persisted_report

        calibrated_count = sum(
            1 for r in calibration_reports.values() if r.overall_verdict == "confirmed"
        )
        questionable_count = sum(
            1 for r in calibration_reports.values() if r.overall_verdict == "questionable"
        )
        refuted_count = sum(
            1 for r in calibration_reports.values() if r.overall_verdict == "refuted"
        )
        logger.info(
            f"Auto-calibration: {calibrated_count} confirmed, "
            f"{questionable_count} questionable, {refuted_count} refuted"
        )
        return calibration_reports, changed_dimensions

    @staticmethod
    def _calibration_source_items(
        observation: Observation,
        items: List[SourceItem],
    ) -> List[SourceItem]:
        if observation.source_type == SourceType.RAW and observation.source_id:
            matched = [
                item for item in items if item.raw_revision_id == observation.source_id
            ]
            if not matched:
                raise ValueError(
                    "canonical Raw Observation source is absent from calibration input: "
                    f"{observation.source_id}"
                )
            return matched
        observation_spans = set(observation.source_span_ids or [])
        if observation_spans:
            matched = [
                item
                for item in items
                if observation_spans.intersection(item.source_span_ids)
            ]
            matched_spans = {
                span_id for item in matched for span_id in item.source_span_ids
            }
            missing_spans = sorted(observation_spans - matched_spans)
            if missing_spans:
                raise ValueError(
                    "Observation calibration input is missing exact source spans: "
                    + ", ".join(missing_spans)
                )
            return matched
        if observation.source_path and not observation.source_path.startswith(
            ("aggregated:", "system:")
        ):
            matched = [
                item
                for item in items
                if str(redact_persistence_value(item.file_path).value)
                == observation.source_path
            ]
            if not matched:
                raise ValueError(
                    "Observation source path is absent from calibration input: "
                    f"{observation.source_path}"
                )
            return matched
        return list(items)

    def _persist(self, batch: ObservationBatch) -> Dict[str, Any]:
        """持久化到 SQLite（Observation Index 是唯一真实来源）。"""
        persist_stats: Dict[str, Any] = {
            "inserted": 0,
            "updated": 0,
            "persisted_observation_ids": set(),
        }
        if batch.observations:
            persist_stats = self.store.save_batch(batch.observations)
            logger.info("Persisted to Observation Index: %s", persist_stats)
        return persist_stats

    def _export_projection(
        self,
        batch: ObservationBatch,
        calibration_reports: Dict[str, CalibrationReport],
        persist_stats: Dict[str, Any],
        is_incremental: bool,
        *,
        persist: bool,
    ):
        """导出到 Obsidian Wiki（Index 的只读投影，顺序必须在 Index 之后）。"""
        if not self.export_to_wiki or not self.wiki_dir:
            return

        if is_incremental:
            # P108: 增量模式只重新导出发生变化的维度，避免每次插入都读 10000 条
            changed_dims = persist_stats.get("changed_dimensions")
            if changed_dims:
                self._reexport_all(dimensions=cast(Optional[Set[str]], changed_dims))
            else:
                logger.info("No observations changed, skipping Wiki re-export")
        else:
            # 全量模式：直接导出当前 batch
            wiki_dir = self.wiki_dir
            if wiki_dir is None:
                raise ValueError("wiki_dir is not configured")
            if persist:
                self._reexport_all(dimensions=None)
            else:
                exporter = WikiExporter(
                    wiki_dir,
                    max_file_bytes=_projection_max_file_bytes(),
                )
                exporter.export_batch(batch, calibration_reports={}, full=True)
        logger.info("Exported read-only projection to %s/L3-Observations/", self.wiki_dir)
        from core.mnemos_bus import publish_event

        trace_id = publish_event(
            "observation.updated",
            "observation_engine",
            {
                "observation_ids": [o.id for o in batch.observations],
                "wiki_path": str(Path(self.wiki_dir) / "L3-Observations"),
                "session_id": "",
            },
        )
        if not trace_id:
            raise RuntimeError("observation.updated publisher returned no trace id")

    def _reexport_all(
        self,
        dimensions: Optional[Set[str]] = None,
        *,
        record_calibration_effects: bool = True,
    ) -> Dict[str, int]:
        """从数据库读取 Observation 并重新生成 Wiki 报告

        Args:
            dimensions: 仅重新导出的维度集合；None 则导出全部（全量模式兼容）
        """
        requested_dimensions = (
            sorted(dimensions) if dimensions else sorted(value.value for value in Dimension)
        )
        all_obs: List[Observation] = []
        seen_ids: Set[str] = set()
        projection_dimensions: List[Dimension] = []
        for dim_value in requested_dimensions:
            try:
                dim = Dimension(dim_value)
            except ValueError:
                continue
            projection_dimensions.append(dim)
            projection_rows = self.store.query_all_for_projection(dimension=dim)
            for observation in projection_rows:
                if observation.id in seen_ids:
                    continue
                seen_ids.add(observation.id)
                all_obs.append(observation)

        # 重建 batch
        batch = ObservationBatch(observations=all_obs)
        for obs in all_obs:
            batch.dimension_counts[obs.dimension.value] = (
                batch.dimension_counts.get(obs.dimension.value, 0) + 1
            )

        # Projection is a replay of committed records; export never recalibrates.
        wiki_dir = self.wiki_dir
        if wiki_dir is None:
            raise ValueError("wiki_dir is not configured")
        if not all_obs:
            WikiExporter(
                wiki_dir,
                max_file_bytes=_projection_max_file_bytes(),
            ).export_batch(
                batch,
                calibration_reports={},
                full=dimensions is None,
                reconcile_dimensions=projection_dimensions,
            )
            return {"observations": 0, "dimensions": 0}
        calibration_reports: Dict[str, CalibrationReport] = {}
        if self.calibration_records is None:
            bound = [
                observation.id
                for observation in all_obs
                if observation.calibration_revision_id
            ]
            if bound:
                raise RuntimeError(
                    "Canonical cognition state store unavailable; cannot project "
                    "calibrated Observations: " + ", ".join(sorted(bound))
                )
        else:
            calibration_reports = self.calibration_records.current_reports(
                (observation.id for observation in all_obs),
                expected_spec_hash=self.calibration_engine.spec_hash,
            )
            for observation in all_obs:
                report = calibration_reports.get(observation.id)
                if report is None:
                    if observation.calibration_revision_id:
                        raise RuntimeError(
                            "Observation projection has no committed CalibrationRecord: "
                            f"{observation.id}"
                        )
                    continue
                if (
                    observation.calibration_revision_id != report.calibration_revision_id
                    or observation.calibration_input_hash
                    != report.calculation_input_hash
                    or observation.calibration_spec_hash
                    != report.validator_spec_hash
                    or observation.calibration_record_hash
                    != report.calibration_record_hash
                    or abs(
                        observation.base_confidence_value()
                        - float(report.original_confidence)
                    )
                    > 1e-9
                    or abs(
                        float(observation.confidence)
                        - float(report.calibrated_confidence)
                    )
                    > 1e-9
                ):
                    raise RuntimeError(
                        "Observation projection points to a non-current CalibrationRecord"
                    )
        exporter = WikiExporter(
            wiki_dir,
            max_file_bytes=_projection_max_file_bytes(),
        )
        projection_receipts = exporter.export_batch(
            batch,
            calibration_reports=calibration_reports,
            full=dimensions is None,
            reconcile_dimensions=projection_dimensions,
        )
        if record_calibration_effects and self.calibration_records is not None:
            pending = self.calibration_records.pending_commands("wiki_projection")
            for observation_id, projection in projection_receipts.items():
                report = calibration_reports[observation_id]
                command = pending.get(report.calibration_revision_id)
                if command is None:
                    continue
                self.calibration_records.record_command_effect(
                    str(command["command_id"]),
                    target_effect_id=(
                        f"wiki-calibration:{observation_id}:"
                        f"{report.calibration_revision_id}"
                    ),
                    before_hash=sha256_json(
                        {"observation_id": observation_id, "calibration_revision_id": ""}
                    ),
                    after_hash=sha256_json(
                        {
                            "observation_id": observation_id,
                            "calibration_revision_id": report.calibration_revision_id,
                            "calibration_record_hash": report.calibration_record_hash,
                            "calibration_set_hash": projection.calibration_set_hash,
                        }
                    ),
                    evidence_refs=(
                        projection.evidence_ref,
                        *(
                            f"omission-receipt:{receipt_id}"
                            for receipt_id in projection.omission_receipt_ids
                        ),
                    ),
                )
        logger.info(
            "Re-exported %d observations from database (dimensions=%s)",
            len(all_obs),
            dimensions or "all",
        )
        return {
            "observations": len(all_obs),
            "dimensions": len(batch.dimension_counts),
        }

    def rebuild_projection(self) -> Dict[str, int]:
        """Read committed Observation rows and rebuild Wiki without canonical writes."""

        return self._reexport_all(
            dimensions=None,
            record_calibration_effects=False,
        )

    def get_store_stats(self) -> Dict:
        """获取存储统计"""
        return self.index.get_stats()

    def _record_observation_evidence(
        self,
        observations: List[Observation],
        items: List[SourceItem],
    ):
        """
        将每个 Observation 追溯到原始 SourceItem，写入 Evidence Graph。

        - 聚合型 Observation 追溯到本次全部有效来源。
        - 系统级统计 Observation 跳过证据链。
        - 普通 Observation 按 source_path / source_id 匹配来源。
        """
        if not self.evidence_graph or not observations:
            return

        for obs in observations:
            matched = []
            if not obs.source_path or obs.source_path.startswith("system:"):
                continue
            if obs.source_path.startswith("aggregated:"):
                matched = items
            elif obs.source_path or obs.source_id:
                matched = [
                    i
                    for i in items
                    if (
                        obs.source_path
                        and str(redact_persistence_value(i.file_path).value)
                        == obs.source_path
                    )
                    or (obs.source_id and i.session_id == obs.source_id)
                ]

            if not matched:
                continue

            summary = (
                obs.evidence[0]
                if obs.evidence
                else f"{obs.dimension.value} / {obs.observation_type.value}"
            )
            try:
                self.evidence_graph.add_observation_sources(
                    observation_id=obs.id,
                    source_items=matched,
                    observation_summary=summary[:300],
                )
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as e:
                logger.error(f"Failed to record evidence for observation {obs.id}: {e}")

    def _append_source_signals(self, batch: ObservationBatch, items: List[SourceItem]):
        """
        追加内容来源与意图信号统计（行为信号层）

        这些数据不是"用户认知"，而是"用户行为"，
        用于校准器判断 Observation 的可靠性。
        """
        from collections import Counter
        from core.cognitive.models import ObservationType
        from core.cognitive.sources import UserIntent

        if not items:
            return

        # 内容来源分布
        source_counts = Counter()  # type: ignore[var-annotated]
        for item in items:
            source_counts[item.content_source.value] += 1

        if len(source_counts) > 1:
            batch.add(
                Observation(
                    dimension=Dimension.ATTENTION,
                    observation_type=ObservationType.PATTERN,
                    value={
                        "content_source_distribution": dict(source_counts),
                        "total_items": len(items),
                    },
                    unit="items",
                    confidence=0.9,
                    source_type=SourceType.WIKI,
                    source_path="system:content_source_stats",
                    source_id="system:content_source_stats",
                    evidence=[f"{k}: {v} 个来源" for k, v in source_counts.most_common()],
                )
            )

        # 用户意图分布（仅限有明确意图的内容）
        intent_counts = Counter()  # type: ignore[var-annotated]
        for item in items:
            if item.user_intent != UserIntent.UNKNOWN:
                intent_counts[item.user_intent.value] += 1

        if intent_counts:
            batch.add(
                Observation(
                    dimension=Dimension.ATTENTION,
                    observation_type=ObservationType.PATTERN,
                    value={
                        "user_intent_distribution": dict(intent_counts),
                        "total_with_intent": sum(intent_counts.values()),
                    },
                    unit="signals",
                    confidence=0.7,
                    source_type=SourceType.WIKI,
                    source_path="system:user_intent_stats",
                    source_id="system:user_intent_stats",
                    evidence=[f"{k}: {v} 次" for k, v in intent_counts.most_common()],
                )
            )
