"""
Observation Wiki Exporter — Observation Index 的只读投影

把 Observation 导出为 Obsidian Markdown 文件，实现：
1. 可见 — 用户在 Obsidian 里直接看见 Observation
2. 可验证 — 每条 Observation 附带证据链（链接回原始 wiki）
3. 已校准 — 系统自动校准结果展示，不再依赖用户主观勾选
4. 只读 — 本目录是 Observation Index 的只读投影，用户可写备注，但不影响系统数据

重要：
- 系统不会读取 `L3-Observations/` 下的修改来更新 Index。
- 若用户需要纠错，应通过专用 API（如 dispute_observation）产生 refutation observation，
  系统会将其记录为 `Observation.user_notes`，再重新导出到 Wiki。
- 直接编辑本文件只会在下次导出时被覆盖（除非 `user_notes` 已通过 API 写入 Index）。
"""

import json
import hashlib
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.cognitive.auto_calibration import CalibrationReport
from core.cognitive.models import Dimension, Observation, ObservationBatch
from core.privacy.content_redaction import redact_persistence_value
from core.wiki_derived_projection import (
    DerivedProjectionLifecycle,
    ProjectionPageSpec,
    canonical_projection_revision,
)


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


# 详情表单元格的字符上限；超出部分截断并加省略号（只读视图，canonical 在 db）。
DETAIL_CELL_MAX_CHARS = 500


def _safe_value(value: Any) -> Any:
    """Apply the local-user narrow PII/credential policy before projection."""

    return redact_persistence_value(value).value


def _safe_text(value: object) -> str:
    return str(_safe_value(str(value or "")))


@dataclass(frozen=True)
class WikiCalibrationProjectionReceipt:
    """Bind one calibrated Observation to its published Markdown bytes."""

    observation_id: str
    calibration_revision_id: str
    file_path: str
    calibration_set_hash: str
    content_hash: str
    evidence_ref: str
    omission_receipt_ids: tuple[str, ...]


class WikiExporter:
    """Observation Wiki 导出器 — Index 的只读投影"""

    def __init__(
        self,
        wiki_dir: str,
        *,
        lifecycle: DerivedProjectionLifecycle | None = None,
        max_file_bytes: int = 0,
    ):
        self.wiki_dir = Path(wiki_dir)
        # v2: 认知层投影统一放在主 Vault 的 L3-Observations/
        self.obs_dir = self.wiki_dir / "L3-Observations"
        self.obs_dir.mkdir(parents=True, exist_ok=True)
        self.lifecycle = lifecycle or DerivedProjectionLifecycle(self.wiki_dir)
        # 单页发布字节上限（含 lifecycle 注入的绑定 frontmatter）。超过时维度
        # 拆成 <dim>.md 索引页 + <dim>.part-NNN.md 分片；0 表示不限制。
        self.max_file_bytes = max(0, int(max_file_bytes or 0))

    def export_batch(
        self,
        batch: ObservationBatch,
        calibration_reports: Optional[Dict[str, CalibrationReport]] = None,
        *,
        full: bool = True,
        reconcile_dimensions: Iterable[Dimension] | None = None,
    ) -> Dict[str, WikiCalibrationProjectionReceipt]:
        """导出整批 Observation，按维度分文件"""
        self.calibration_reports = calibration_reports or {}
        for obs in batch.observations:
            report = self.calibration_reports.get(obs.id)
            if report is None:
                continue
            if not report.calibration_revision_id:
                raise ValueError("Wiki calibration projection requires a committed revision")
            if (
                report.observation_id != obs.id
                or obs.base_measurement_status != "verified"
                or obs.calibration_revision_id != report.calibration_revision_id
                or obs.calibration_input_hash != report.calculation_input_hash
                or obs.calibration_spec_hash != report.validator_spec_hash
                or obs.calibration_record_hash != report.calibration_record_hash
                or abs(obs.base_confidence_value() - report.original_confidence) > 1e-9
                or abs(float(obs.confidence) - report.calibrated_confidence) > 1e-9
            ):
                raise ValueError("Observation/calibration record binding mismatch")
        # 按维度分组
        by_dimension: Dict[Dimension, List[Observation]] = {}
        for obs in batch.observations:
            by_dimension.setdefault(obs.dimension, []).append(obs)

        rendered: Dict[
            Dimension,
            tuple[Path, str, List[Observation], str, tuple[Path, ...]],
        ] = {}
        pages: List[ProjectionPageSpec] = []
        for dim, observations in by_dimension.items():
            file_path, content, calibration_set_hash = self._render_dimension_file(
                dim,
                observations,
                batch,
            )
            canonical_revision = canonical_projection_revision(
                {
                    "dimension": dim.value,
                    "observations": [
                        observation.to_dict()
                        for observation in sorted(observations, key=lambda value: value.id)
                    ],
                    "calibration_reports": {
                        observation.id: self.calibration_reports.get(observation.id)
                        for observation in sorted(observations, key=lambda value: value.id)
                    },
                }
            )
            dim_pages = self._dimension_pages(
                dim,
                file_path,
                content,
                observations,
                batch,
                calibration_set_hash=calibration_set_hash,
                canonical_revision=canonical_revision,
            )
            pages.extend(dim_pages)
            rendered[dim] = (
                file_path,
                calibration_set_hash,
                observations,
                canonical_revision,
                tuple(page.path for page in dim_pages),
            )
        generation = self.lifecycle.publish_generation(
            projection_kind="observation",
            scope_root=self.obs_dir,
            pages=pages,
            full=full,
            stale_paths=(
                self._incremental_stale_paths(reconcile_dimensions, by_dimension)
                if not full
                else ()
            ),
            owned_paths=(self._full_owned_paths(pages) if full else None),
        )
        items_by_path = {item.path: item for item in generation.items if item.action == "upsert"}
        receipts: Dict[str, WikiCalibrationProjectionReceipt] = {}
        for (
            file_path,
            calibration_set_hash,
            observations,
            _revision,
            page_paths,
        ) in rendered.values():
            for page_path in page_paths:
                page_item = items_by_path[str(page_path.resolve(strict=False))]
                if page_item.status != "published" or not page_item.event_trace_id:
                    raise RuntimeError(
                        f"Observation projection was not published: {page_path}"
                    )
            item = items_by_path[str(file_path.resolve(strict=False))]
            receipts.update(
                self._calibration_projection_receipts(
                    file_path,
                    observations,
                    calibration_set_hash=calibration_set_hash,
                    content_hash=item.content_sha256,
                )
            )
        return receipts

    def _render_dimension_file(
        self, dim: Dimension, observations: List[Observation], batch: ObservationBatch
    ) -> tuple[Path, str, str]:
        """Render one dimension without mutating the projection target."""
        file_path = self.obs_dir / f"{dim.value}.md"

        lines: List[str] = []
        calibration_set_hash = self._calibration_set_hash(observations)
        lines.extend(
            self._render_frontmatter(
                dim,
                observations,
                batch,
                calibration_set_hash=calibration_set_hash,
            )
        )
        lines.append(f"# {self._dim_title(dim)} — Observation")
        lines.append("")
        lines.append(
            "> 本文件由 Observation Engine 自动生成，是系统 Observation Index 的只读投影。"
        )
        lines.append("> 用户备注不会反向修改系统数据；如需纠错，请通过 `dispute_observation` API。")
        lines.append("")
        lines.extend(self._render_observations(observations))

        source_paths = list({o.source_path for o in observations if o.source_path})
        lines.extend(self._render_evidence_chain(source_paths))
        lines.extend(self._render_summary(observations))
        lines.extend(self._render_user_notes_section())

        content = "\n".join(lines)
        return file_path, content, calibration_set_hash

    def _dimension_pages(
        self,
        dim: Dimension,
        file_path: Path,
        content: str,
        observations: List[Observation],
        batch: ObservationBatch,
        *,
        calibration_set_hash: str,
        canonical_revision: str,
    ) -> List[ProjectionPageSpec]:
        """Return the index page plus any part pages for one dimension.

        Dimensions whose bound bytes fit inside ``max_file_bytes`` keep the
        exact single-file projection; larger dimensions are split at
        observation boundaries into ``<dim>.part-NNN.md`` parts.
        """

        source_refs = tuple(
            f"observation:{observation.id}"
            for observation in sorted(observations, key=lambda value: value.id)
        )
        if self._content_fits_cap(content, canonical_revision=canonical_revision):
            return [
                ProjectionPageSpec(
                    path=file_path,
                    content=content,
                    page_role="formal_derived:observation",
                    canonical_revision=canonical_revision,
                    source_refs=source_refs,
                )
            ]
        parts = self._pack_dimension_parts(
            dim,
            observations,
            batch,
            calibration_set_hash=calibration_set_hash,
            canonical_revision=canonical_revision,
        )
        part_count = len(parts)
        specs = [
            ProjectionPageSpec(
                path=file_path,
                content=self._render_dimension_index(
                    dim,
                    observations,
                    batch,
                    part_count=part_count,
                    calibration_set_hash=calibration_set_hash,
                ),
                page_role="formal_derived:observation",
                canonical_revision=canonical_revision,
                source_refs=source_refs,
            )
        ]
        for part_index, entries in enumerate(parts, 1):
            part_content = "\n".join(
                self._render_dimension_part_lines(
                    dim,
                    entries,
                    observations,
                    batch,
                    part_index=part_index,
                    part_count=part_count,
                    calibration_set_hash=calibration_set_hash,
                )
            )
            if len(entries) > 1 and not self._content_fits_cap(
                part_content,
                canonical_revision=canonical_revision,
            ):
                raise RuntimeError(
                    f"Observation projection part exceeds {self.max_file_bytes} bytes: "
                    f"{dim.value}.part-{part_index:03d}"
                )
            specs.append(
                ProjectionPageSpec(
                    path=self.obs_dir / f"{dim.value}.part-{part_index:03d}.md",
                    content=part_content,
                    page_role="formal_derived:observation",
                    canonical_revision=canonical_revision,
                    source_refs=tuple(
                        f"observation:{observation.id}"
                        for _index, observation in sorted(
                            entries,
                            key=lambda entry: entry[1].id,
                        )
                    ),
                )
            )
        return specs

    def _content_fits_cap(self, content: str, *, canonical_revision: str) -> bool:
        """Measure the exact bytes the lifecycle will publish against the cap."""

        if self.max_file_bytes <= 0:
            return True
        bound = self.lifecycle.bind_content(
            content,
            projection_kind="observation",
            page_role="formal_derived:observation",
            canonical_revision=canonical_revision,
        )
        return len(bound.encode("utf-8")) <= self.max_file_bytes

    def _binding_overhead_bytes(self, canonical_revision: str) -> int:
        """Bytes the lifecycle adds when binding one frontmatter document."""

        probe = "---\nx: 1\n---\n"
        bound = self.lifecycle.bind_content(
            probe,
            projection_kind="observation",
            page_role="formal_derived:observation",
            canonical_revision=canonical_revision,
        )
        return len(bound.encode("utf-8")) - len(probe.encode("utf-8"))

    def _pack_dimension_parts(
        self,
        dim: Dimension,
        observations: List[Observation],
        batch: ObservationBatch,
        *,
        calibration_set_hash: str,
        canonical_revision: str,
    ) -> List[List[tuple[int, Observation]]]:
        """Greedily pack observations into parts that fit the byte cap.

        An observation's summary-table row and detail-table row always stay in
        the same part; a single observation larger than the cap gets its own
        part. Byte accounting is exact against the final rendered part, so the
        fixpoint on ``part_count`` digit length converges in a few passes.
        """

        indexed = list(enumerate(observations, 1))
        unit_bytes: List[int] = []
        for index, observation in indexed:
            size = len(self._render_observation_row(observation).encode("utf-8")) + 1
            size += (
                len(
                    self._render_observation_detail_row(
                        index, observation
                    ).encode("utf-8")
                )
                + 1
            )
            unit_bytes.append(size)
        binding_overhead = self._binding_overhead_bytes(canonical_revision)

        def overhead_adjustment(entry_count: int, part_index: int) -> int:
            # ``base`` is rendered with observation_count=0 / part_index=1 and
            # a 3-digit part slug; correct for the real digit widths.
            return (
                (len(str(entry_count)) - 1)
                + (len(str(part_index)) - 1)
                + (len(f"{part_index:03d}") - 3)
            )

        hint = 1
        while True:
            base = len(
                "\n".join(
                    self._render_dimension_part_lines(
                        dim,
                        [],
                        observations,
                        batch,
                        part_index=1,
                        part_count=hint,
                        calibration_set_hash=calibration_set_hash,
                    )
                ).encode("utf-8")
            )
            parts: List[List[int]] = []
            current: List[int] = []
            current_units = 0
            for position in range(len(indexed)):
                candidate = (
                    base
                    + binding_overhead
                    + current_units
                    + unit_bytes[position]
                    + overhead_adjustment(len(current) + 1, len(parts) + 1)
                )
                if current and candidate > self.max_file_bytes:
                    parts.append(current)
                    current = [position]
                    current_units = unit_bytes[position]
                else:
                    current.append(position)
                    current_units += unit_bytes[position]
            if current:
                parts.append(current)
            if len(parts) == hint:
                return [[indexed[position] for position in part] for part in parts]
            hint = len(parts)

    def _render_dimension_index(
        self,
        dim: Dimension,
        observations: List[Observation],
        batch: ObservationBatch,
        *,
        part_count: int,
        calibration_set_hash: str,
    ) -> str:
        """Render the <dim>.md index page for a paginated dimension."""

        lines: List[str] = []
        lines.extend(
            self._render_frontmatter(
                dim,
                observations,
                batch,
                calibration_set_hash=calibration_set_hash,
                extra={"part_count": part_count, "paginated": True},
            )
        )
        lines.append(f"# {self._dim_title(dim)} — Observation")
        lines.append("")
        lines.append(
            "> 本文件由 Observation Engine 自动生成，是系统 Observation Index 的只读投影。"
        )
        lines.append("> 用户备注不会反向修改系统数据；如需纠错，请通过 `dispute_observation` API。")
        lines.append("")
        lines.append(
            f"本维度共 {len(observations)} 条 Observation，单文件超过 "
            f"{self.max_file_bytes} 字节上限，已分片为 {part_count} 个 part 文件；"
            f"请按 part-001 → part-{part_count:03d} 顺序查看或拼接。"
        )
        lines.append("")
        lines.append("## 分片索引")
        lines.append("")
        for part_index in range(1, part_count + 1):
            lines.append(f"- [[{dim.value}.part-{part_index:03d}]]")
        lines.append("")

        source_paths = list({o.source_path for o in observations if o.source_path})
        lines.extend(self._render_evidence_chain(source_paths))
        lines.extend(self._render_summary(observations))
        lines.extend(self._render_user_notes_section())
        return "\n".join(lines)

    def _render_dimension_part_lines(
        self,
        dim: Dimension,
        entries: List[tuple[int, Observation]],
        observations: List[Observation],
        batch: ObservationBatch,
        *,
        part_index: int,
        part_count: int,
        calibration_set_hash: str,
    ) -> List[str]:
        """Render one <dim>.part-NNN.md shard (table header repeated per part)."""

        frontmatter = {
            "dimension": dim.value,
            "generated_at": self._projection_generated_at(observations, batch),
            "observation_count": len(entries),
            "part_index": part_index,
            "part_count": part_count,
            "calibration_set_hash": calibration_set_hash,
        }
        lines = ["---"]
        for key, value in frontmatter.items():
            lines.extend(self._format_frontmatter_line(key, value))
        lines.append("---")
        lines.append("")
        lines.append(
            f"# {self._dim_title(dim)} — Observation"
            f"（part {part_index:03d}/{part_count:03d}）"
        )
        lines.append("")
        lines.append(
            "> 本文件由 Observation Engine 自动生成，是系统 Observation Index 的只读投影分片。"
        )
        lines.append(
            f"> 索引页：[[{dim.value}]]；请按 part-001 → part-{part_count:03d} 顺序查看或拼接。"
        )
        lines.append("")
        lines.append("## 观察值")
        lines.append("")
        lines.append("| Observation ID | 类型 | 值 | 单位 | 置信度 | 校准状态 |")
        lines.append("|---|------|-----|------|--------|------|")
        for _index, observation in entries:
            lines.append(self._render_observation_row(observation))
        lines.append("")
        lines.append("## 详细")
        lines.append("")
        lines.extend(self._render_detail_table(entries))
        return lines

    def _existing_part_paths(
        self,
        dimensions: Iterable[Dimension] | None = None,
    ) -> set[Path]:
        """Glob existing <dim>.part-*.md shards inside the projection scope."""

        dims = tuple(dimensions) if dimensions is not None else tuple(Dimension)
        paths: set[Path] = set()
        if not self.obs_dir.is_dir():
            return paths
        for dim in dims:
            paths.update(self.obs_dir.glob(f"{dim.value}.part-*.md"))
        return paths

    def _full_owned_paths(self, pages: List[ProjectionPageSpec]) -> tuple[Path, ...]:
        """Owned set for a full generation: index pages plus every shard."""

        owned = {self.obs_dir / f"{dimension.value}.md" for dimension in Dimension}
        owned.update(self._existing_part_paths())
        owned.update(Path(page.path) for page in pages)
        return tuple(sorted(owned))

    def _incremental_stale_paths(
        self,
        reconcile_dimensions: Iterable[Dimension] | None,
        by_dimension: Dict[Dimension, List[Observation]],
    ) -> tuple[Path, ...]:
        """Stale set for an incremental generation.

        Reconciled dimensions that disappeared lose their index page and all
        shards; re-exported dimensions lose shards that are no longer desired
        (desired paths are subtracted again by the lifecycle).
        """

        reconcile = set(reconcile_dimensions or ())
        stale = {
            self.obs_dir / f"{dimension.value}.md"
            for dimension in reconcile - set(by_dimension)
        }
        stale.update(self._existing_part_paths(reconcile | set(by_dimension)))
        return tuple(sorted(stale))

    def _calibration_projection_receipts(
        self,
        file_path: Path,
        observations: List[Observation],
        *,
        calibration_set_hash: str,
        content_hash: str,
    ) -> Dict[str, WikiCalibrationProjectionReceipt]:
        """Bind per-observation calibration effects to one published page."""

        receipts: Dict[str, WikiCalibrationProjectionReceipt] = {}
        for observation in observations:
            report = self.calibration_reports.get(observation.id)
            if report is None or not report.calibration_revision_id:
                continue
            evidence_ref = (
                f"wiki-calibration:{observation.dimension.value}:{calibration_set_hash}:"
                f"{report.calibration_revision_id}"
            )
            receipts[observation.id] = WikiCalibrationProjectionReceipt(
                observation_id=observation.id,
                calibration_revision_id=report.calibration_revision_id,
                file_path=str(file_path),
                calibration_set_hash=calibration_set_hash,
                content_hash=content_hash,
                evidence_ref=evidence_ref,
                omission_receipt_ids=tuple(
                    str(value["receipt_id"]) for value in report.omission_receipts
                ),
            )
        return receipts

    def _render_frontmatter(
        self,
        dim: Dimension,
        observations: List[Observation],
        batch: ObservationBatch,
        *,
        calibration_set_hash: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """渲染 YAML frontmatter 块"""
        avg_confidence = sum(o.confidence for o in observations) / len(observations)
        frontmatter = {
            "dimension": dim.value,
            "generated_at": self._projection_generated_at(observations, batch),
            "version": max(o.version for o in observations),
            "confidence": round(avg_confidence, 2),
            "observation_count": len(observations),
            "calibration_set_hash": calibration_set_hash,
            "source_count": batch.source_count,
            "period_start": batch.period_start.isoformat() if batch.period_start else None,
            "period_end": batch.period_end.isoformat() if batch.period_end else None,
        }
        if extra:
            frontmatter.update(extra)

        lines = ["---"]
        for k, v in frontmatter.items():
            lines.extend(self._format_frontmatter_line(k, v))
        lines.append("---")
        lines.append("")
        return lines

    @staticmethod
    def _projection_generated_at(
        observations: List[Observation],
        batch: ObservationBatch,
    ) -> str:
        """Use committed source time so replay renders byte-identical output."""

        candidates = [
            value
            for value in (
                batch.period_end,
                *(observation.updated_at for observation in observations),
                *(observation.observed_at for observation in observations),
            )
            if value is not None
        ]
        if not candidates:
            raise ValueError("Observation projection has no canonical timestamp")
        normalized = [
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None or value.utcoffset() is None
            else value.astimezone(timezone.utc)
            for value in candidates
        ]
        return max(normalized).isoformat()

    @staticmethod
    def _format_frontmatter_line(key: str, value) -> List[str]:
        """单个 frontmatter 字段格式化为 YAML 行"""
        if value is None:
            return [f"{key}:"]
        if isinstance(value, bool):
            return [f"{key}: {'true' if value else 'false'}"]
        if isinstance(value, str):
            return [f'{key}: "{value}"']
        return [f"{key}: {value}"]

    def _render_observations(self, observations: List[Observation]) -> List[str]:
        """渲染观察值表格与详情"""
        lines = []
        lines.append("## 观察值")
        lines.append("")
        lines.append("| Observation ID | 类型 | 值 | 单位 | 置信度 | 校准状态 |")
        lines.append("|---|------|-----|------|--------|------|")
        for obs in observations:
            lines.append(self._render_observation_row(obs))
        lines.append("")

        lines.append("## 详细")
        lines.append("")
        lines.extend(self._render_detail_table(enumerate(observations, 1)))
        return lines

    def _render_observation_row(self, obs: Observation) -> str:
        """渲染单条 Observation 的观察值表格行"""
        value_str = json.dumps(_safe_value(obs.value), ensure_ascii=False)[:100]
        if len(value_str) >= 100:
            value_str += "..."
        status = self._get_calibration_status(obs.id)
        return (
            f"| `{_safe_text(obs.id)}` | {obs.observation_type.value} | {value_str} | {obs.unit} | {obs.confidence} | {status} |"  # noqa: E501
        )

    def _render_detail_table(
        self,
        entries: Iterable[tuple[int, Observation]],
    ) -> List[str]:
        """把整个 `## 详细` 小节渲染成一个表格，每条 Observation 一行。

        大量标题/围栏/引用容器块的组合会让 Obsidian Renderer 崩溃，因此详情
        信息一律压平进单行单元格；校准状态保留在 `## 观察值` 主表，完整
        canonical 数据始终在 Observation Index 数据库中。
        """
        lines = [
            "| # | 类型 | 值 | 证据片段 | 来源 | 时间窗口 |",
            "|---|------|-----|---------|------|---------|",
        ]
        for index, observation in entries:
            lines.append(self._render_observation_detail_row(index, observation))
        lines.append("")
        return lines

    def _render_observation_detail_row(self, i: int, obs: Observation) -> str:
        """渲染单条 Observation 的详情表行"""
        value_json = json.dumps(_safe_value(obs.value), ensure_ascii=False)
        evidence = " / ".join(_safe_text(item) for item in obs.evidence[:5])
        source = f"{obs.source_type.value} — {_safe_text(obs.source_path)}"
        if obs.calibration_revision_id:
            # 校准 revision 与记录哈希是该行校准后置信度的来源证明，并入来源列。
            source += f" · 校准 {_safe_text(obs.calibration_revision_id)}"
            if obs.calibration_record_hash:
                source += f" 记录 {_safe_text(obs.calibration_record_hash)}"
        if obs.source_span_ids:
            source += " · spans: " + ", ".join(
                _safe_text(value) for value in obs.source_span_ids[:20]
            )
        report = self.calibration_reports.get(obs.id)
        if report and report.calibration_revision_id and report.omission_receipts:
            source += " · " + " / ".join(
                f"省略回执 {_safe_text(receipt['receipt_id'])} "
                f"target={_safe_text(receipt['target'])} "
                f"displayed={receipt['displayed_count']} "
                f"omitted={receipt['omitted_count']}"
                for receipt in report.omission_receipts
            )
        window = ""
        if obs.period_start and obs.period_end:
            window = f"{obs.period_start.date()} ~ {obs.period_end.date()}"
        cells = (
            str(i),
            obs.observation_type.value,
            self._detail_cell(value_json),
            self._detail_cell(evidence),
            self._detail_cell(source),
            self._detail_cell(window),
        )
        return "| " + " | ".join(cells) + " |"

    @staticmethod
    def _detail_cell(text: str) -> str:
        """压平空白、转义管道、超长截断，保证单元格单行且表格安全。"""

        flattened = " ".join(str(text).split())
        if len(flattened) > DETAIL_CELL_MAX_CHARS:
            flattened = flattened[:DETAIL_CELL_MAX_CHARS] + "…"
        return flattened.replace("|", "\\|")

    def _render_evidence_chain(self, source_paths: List[str]) -> List[str]:
        """渲染来源文件证据链"""
        if not source_paths:
            return []

        lines = ["## 证据链（来源文件）", ""]
        unique_paths = sorted(set(source_paths))
        for sp in unique_paths[:20]:
            rel_path = self._make_wiki_link(sp)
            lines.append(f"- {rel_path}")
        if len(unique_paths) > 20:
            omitted = unique_paths[20:]
            payload = {
                "target": "dimension_source_paths",
                "total_count": len(unique_paths),
                "displayed_count": 20,
                "omitted_count": len(omitted),
                "omitted_hash": _sha256(_canonical_list(omitted)),
            }
            receipt_id = "omission:" + _sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            ).split(":", 1)[1][:32]
            lines.append(
                f"- omission receipt `{receipt_id}`: omitted={len(omitted)} "
                f"hash=`{payload['omitted_hash']}`"
            )
        lines.append("")
        return lines

    def _calibration_set_hash(self, observations: List[Observation]) -> str:
        payload = []
        for observation in sorted(observations, key=lambda value: value.id):
            report = self.calibration_reports.get(observation.id)
            payload.append(
                {
                    "observation_id": observation.id,
                    "calibration_revision_id": (
                        report.calibration_revision_id if report is not None else ""
                    ),
                    "calibration_record_hash": (
                        report.calibration_record_hash if report is not None else ""
                    ),
                    "stale": bool(report.stale) if report is not None else False,
                }
            )
        return _sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _render_summary(self, observations: List[Observation]) -> List[str]:
        """渲染自动校准汇总"""
        dim_obs_ids = {obs.id for obs in observations}
        dim_reports = [r for rid, r in self.calibration_reports.items() if rid in dim_obs_ids]
        confirmed = sum(1 for r in dim_reports if r.overall_verdict == "confirmed")
        questionable = sum(1 for r in dim_reports if r.overall_verdict == "questionable")
        refuted = sum(1 for r in dim_reports if r.overall_verdict == "refuted")

        lines = [
            "## 自动校准汇总",
            "",
            "<!-- 系统自动校准结果。用户可在此补充说明，但不需要勾选。 -->",
            "",
            f"此维度共 {len(observations)} 条 Observation：",
            f"- ✅ **confirmed**（确认可信）: {confirmed} 条",
            f"- ⚠️ **questionable**（存疑，需人工审视）: {questionable} 条",
            f"- ❌ **refuted**（被反驳，建议排除）: {refuted} 条",
            "",
        ]
        return lines

    @staticmethod
    def _render_user_notes_section() -> List[str]:
        """渲染底部用户补充备注区"""
        return [
            "### 用户补充备注",
            "",
            "```",
            "# 你可以在这里写补充说明，比如：",
            '# - "AI 3948次" 包含了系统文档，实际讨论中 AI 占比约 60%',
            '# - "延期率15%" 偏低，因为很多估算没有写入文档',
            "```",
            "",
            "**你的备注**:",
            "",
            "_（在此写下你的判断...）_",
            "",
            "<!-- 注意：直接编辑此处不会同步回系统。如需系统级纠错，请使用 dispute_observation API。 -->",
            "",
        ]

    def _dim_title(self, dim: Dimension) -> str:
        """维度中文名"""
        titles = {
            Dimension.ATTENTION: "关注分布",
            Dimension.DECISIONS: "决策模式",
            Dimension.ACTIONS: "行动模式",
            Dimension.TIME: "时间模式",
            Dimension.STRESS: "压力信号",
            Dimension.RELATIONSHIPS: "关系模式",
            Dimension.GROWTH: "成长轨迹",
        }
        return titles.get(dim) or dim.value

    def _make_wiki_link(self, abs_path: str) -> str:
        """把绝对路径转为 Obsidian wiki link"""
        safe_path = _safe_text(abs_path)
        try:
            path = Path(safe_path)
            rel = path.relative_to(self.wiki_dir)
            return f"[[{str(rel).replace('.md', '')}]]"
        except ValueError:
            # 不在 wiki 目录内（如 raw 文件）
            return f"`{safe_path}`"

    def _get_calibration_status(self, obs_id: str) -> str:
        """
        基于自动校准报告返回 Observation 状态。
        返回: ✅ 已验证 / ⚠️ 存疑 / ❌ 被反驳 / ❓ 待校准
        """
        report = self.calibration_reports.get(obs_id)
        if not report or not report.calibration_revision_id:
            return "❓ 待校准"
        if report.stale:
            return "⏳ 规格已过期"

        emoji_map = {
            "confirmed": "✅ 已验证",
            "questionable": "⚠️ 存疑",
            "refuted": "❌ 被反驳",
            "inconclusive": "❓ 待校准",
        }
        return emoji_map.get(report.overall_verdict, "❓ 待校准")


def _canonical_list(values: List[str]) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
