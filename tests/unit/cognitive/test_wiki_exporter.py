"""Unit tests for core.cognitive.wiki_exporter."""

import hashlib
from datetime import datetime, timezone

import pytest

from core.cognitive.auto_calibration import CalibrationReport, ValidationResult
from core.cognitive.models import (
    Dimension,
    Observation,
    ObservationBatch,
    ObservationType,
    SourceType,
)
from core.cognitive.wiki_exporter import WikiExporter


@pytest.fixture
def batch() -> ObservationBatch:
    """Return a small batch with two dimensions."""
    obs_attention = Observation(
        id="obs-att-001",
        dimension=Dimension.ATTENTION,
        observation_type=ObservationType.FREQUENCY,
        value={"concepts": {"ai": 10}, "total_mentions": 10, "dominant": "ai"},
        unit="mentions",
        confidence=0.85,
        source_type=SourceType.WIKI,
        source_path="/wiki/attention.md",
        source_id="session-1",
        evidence=["AI dominates the conversation"],
        observed_at=datetime(2026, 6, 1, 10, 0, 0),
        period_start=datetime(2026, 6, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 2, 0, 0, 0),
        version=1,
    )
    obs_time = Observation(
        id="obs-time-001",
        dimension=Dimension.TIME,
        observation_type=ObservationType.FREQUENCY,
        value={"estimates": 12, "delays": 3},
        unit="mentions",
        confidence=0.7,
        source_type=SourceType.RAW,
        source_path="/raw/session-2.md",
        source_id="session-2",
        evidence=["3 delays out of 12 estimates"],
        period_start=datetime(2026, 6, 2, 0, 0, 0),
        period_end=datetime(2026, 6, 3, 0, 0, 0),
        version=1,
    )
    return ObservationBatch(
        observations=[obs_attention, obs_time],
        period_start=datetime(2026, 6, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 3, 0, 0, 0),
        source_count=2,
        dimension_counts={"attention": 1, "time": 1},
    )


@pytest.fixture
def calibration_reports(batch) -> dict:
    """Return calibration reports keyed by observation id."""
    observation = batch.observations[0]
    observation.calibration_revision_id = "cogrev-calibration-001"
    observation.calibration_input_hash = "sha256:input"
    observation.calibration_spec_hash = "sha256:spec"
    observation.calibration_record_hash = "sha256:record"
    observation.confidence = 0.9
    return {
        "obs-att-001": CalibrationReport(
            observation_id="obs-att-001",
            original_confidence=0.85,
            calibrated_confidence=0.9,
            overall_verdict="confirmed",
            validations=[
                ValidationResult(
                    validator_name="cross_source",
                    score=0.85,
                    verdict="confirmed",
                    reason="Both L1 and L2 support this observation.",
                    confidence_delta=0.05,
                ),
            ],
            suggestions=[],
            calibration_revision_id="cogrev-calibration-001",
            calibration_record_hash="sha256:record",
            calculation_input_hash="sha256:input",
            validator_spec_hash="sha256:spec",
            source_span_ids=["raw-span:raw-1:0:10"],
            omission_receipts=[
                {
                    "receipt_id": "omission:test",
                    "target": "source_span_ids",
                    "displayed_count": 1,
                    "omitted_count": 0,
                    "omitted_hash": "sha256:none",
                }
            ],
        ),
    }


class TestWikiExporter:
    """Tests for WikiExporter.export_batch."""

    def test_export_batch_creates_dimension_files(self, tmp_path, batch):
        exporter = WikiExporter(str(tmp_path))
        exporter.export_batch(batch)

        obs_dir = tmp_path / "L3-Observations"
        assert obs_dir.exists()
        assert (obs_dir / "attention.md").exists()
        assert (obs_dir / "time.md").exists()

    def test_export_batch_writes_observation_values(self, tmp_path, batch):
        exporter = WikiExporter(str(tmp_path))
        exporter.export_batch(batch)

        attention_path = tmp_path / "L3-Observations" / "attention.md"
        content = attention_path.read_text(encoding="utf-8")

        assert "# 关注分布" in content
        assert "obs-att-001" in content
        assert "frequency" in content
        assert '"ai": 10' in content
        assert "mentions" in content
        assert "0.85" in content

    def test_export_batch_writes_calibration_reports(self, tmp_path, batch, calibration_reports):
        exporter = WikiExporter(str(tmp_path))
        exporter.export_batch(batch, calibration_reports=calibration_reports)

        attention_path = tmp_path / "L3-Observations" / "attention.md"
        content = attention_path.read_text(encoding="utf-8")

        # 校准状态保留在观察值主表；逐观察的容器块校准报告已并入表格化详情。
        assert "✅ 已验证" in content
        assert "0.9" in content
        assert "自动校准报告" not in content

    def test_detail_section_is_a_single_table(self, tmp_path, batch):
        WikiExporter(str(tmp_path)).export_batch(batch)

        content = (tmp_path / "L3-Observations" / "attention.md").read_text(
            encoding="utf-8"
        )

        assert "| # | 类型 | 值 | 证据片段 | 来源 | 时间窗口 |" in content
        assert "### #" not in content
        assert "```json" not in content
        assert "| 1 | frequency |" in content

    def test_detail_cell_escapes_pipes_and_flattens_whitespace(self, tmp_path, batch):
        batch.observations[0].value = {"note": "a|b"}
        batch.observations[0].evidence = ["line1\nline2 | pipe"]

        WikiExporter(str(tmp_path)).export_batch(batch)

        content = (tmp_path / "L3-Observations" / "attention.md").read_text(
            encoding="utf-8"
        )
        detail_row = next(
            line for line in content.splitlines() if line.startswith("| 1 | frequency |")
        )
        assert "a\\|b" in detail_row
        assert "line1 line2 \\| pipe" in detail_row

    def test_detail_cell_truncates_overlong_values(self, tmp_path, batch):
        batch.observations[0].value = {"payload": "y" * 1200}

        WikiExporter(str(tmp_path)).export_batch(batch)

        content = (tmp_path / "L3-Observations" / "attention.md").read_text(
            encoding="utf-8"
        )
        detail_row = next(
            line for line in content.splitlines() if line.startswith("| 1 | frequency |")
        )
        assert "…" in detail_row
        assert "y" * 1200 not in detail_row
        assert len(detail_row) < 1000

    def test_detail_cell_empty_evidence_stays_empty(self, tmp_path, batch):
        batch.observations[0].evidence = []

        WikiExporter(str(tmp_path)).export_batch(batch)

        content = (tmp_path / "L3-Observations" / "attention.md").read_text(
            encoding="utf-8"
        )
        detail_row = next(
            line for line in content.splitlines() if line.startswith("| 1 | frequency |")
        )
        cells = [cell.strip() for cell in detail_row.strip().strip("|").split("|")]
        assert cells[0] == "1"
        assert cells[3] == ""

    def test_detail_row_escapes_source_path_pipes(self, tmp_path, batch):
        batch.observations[0].source_path = "/wiki/a|b.md"

        WikiExporter(str(tmp_path)).export_batch(batch)

        content = (tmp_path / "L3-Observations" / "attention.md").read_text(
            encoding="utf-8"
        )
        detail_row = next(
            line for line in content.splitlines() if line.startswith("| 1 | frequency |")
        )
        assert "wiki — /wiki/a\\|b.md" in detail_row

    def test_export_batch_includes_evidence_chain(self, tmp_path, batch):
        exporter = WikiExporter(str(tmp_path))
        exporter.export_batch(batch)

        attention_path = tmp_path / "L3-Observations" / "attention.md"
        content = attention_path.read_text(encoding="utf-8")

        assert "证据链（来源文件）" in content
        assert "attention.md" in content

    def test_export_batch_empty_batch_creates_no_files(self, tmp_path):
        exporter = WikiExporter(str(tmp_path))
        exporter.export_batch(ObservationBatch())

        obs_dir = tmp_path / "L3-Observations"
        assert obs_dir.exists()
        assert list(obs_dir.iterdir()) == []

    def test_full_export_preserves_independent_nested_reports(self, tmp_path):
        report = tmp_path / "L3-Observations" / "immune" / "report.md"
        report.parent.mkdir(parents=True)
        report.write_text("independent report", encoding="utf-8")

        WikiExporter(str(tmp_path)).export_batch(ObservationBatch(), full=True)

        assert report.read_text(encoding="utf-8") == "independent report"

    def test_incremental_empty_dimension_tombstones_its_stale_page(self, tmp_path, batch):
        exporter = WikiExporter(str(tmp_path))
        attention_only = ObservationBatch(observations=[batch.observations[0]])
        exporter.export_batch(attention_only, full=False)
        attention_path = tmp_path / "L3-Observations" / "attention.md"
        assert attention_path.is_file()

        exporter.export_batch(
            ObservationBatch(),
            full=False,
            reconcile_dimensions=(Dimension.ATTENTION,),
        )

        assert not attention_path.exists()
        binding = exporter.lifecycle.binding_for_path(attention_path)
        assert binding is not None
        assert binding["action"] == "delete"
        assert binding["status"] == "published"

    def test_export_batch_uses_absolute_paths_for_non_wiki_links(self, tmp_path, batch):
        exporter = WikiExporter(str(tmp_path))
        exporter.export_batch(batch)

        time_path = tmp_path / "L3-Observations" / "time.md"
        content = time_path.read_text(encoding="utf-8")

        # raw file path is not inside wiki_dir, so it should appear as a code span
        assert "`/raw/session-2.md`" in content

    def test_obs_dir_created_automatically(self, tmp_path):
        exporter = WikiExporter(str(tmp_path))
        assert exporter.obs_dir.exists()

    def test_projection_timestamp_normalizes_mixed_naive_and_aware_history(
        self, tmp_path, batch
    ):
        batch.period_end = datetime(2026, 6, 3, tzinfo=timezone.utc)
        batch.observations[0].updated_at = datetime(2026, 6, 4)

        WikiExporter(str(tmp_path)).export_batch(batch)

        content = (
            tmp_path / "L3-Observations" / "attention.md"
        ).read_text(encoding="utf-8")
        assert 'generated_at: "2026-06-04T00:00:00+00:00"' in content


def _paged_batch(count: int) -> ObservationBatch:
    """Return a single-dimension batch whose observations paginate under a cap."""
    observations = [
        Observation(
            id=f"obs-att-{index:03d}",
            dimension=Dimension.ATTENTION,
            observation_type=ObservationType.FREQUENCY,
            value={"index": index, "payload": "y" * 1200},
            unit="mentions",
            confidence=0.8,
            source_type=SourceType.WIKI,
            source_path="/wiki/attention.md",
            source_id="session-1",
            evidence=[f"evidence for observation {index}"],
            observed_at=datetime(2026, 6, 1, 10, 0, 0),
            period_start=datetime(2026, 6, 1, 0, 0, 0),
            period_end=datetime(2026, 6, 2, 0, 0, 0),
            created_at=datetime(2026, 6, 1, 10, 0, 0),
            updated_at=datetime(2026, 6, 1, 10, 0, 0),
            version=1,
        )
        for index in range(count)
    ]
    return ObservationBatch(
        observations=observations,
        period_start=datetime(2026, 6, 1, 0, 0, 0),
        period_end=datetime(2026, 6, 2, 0, 0, 0),
        source_count=1,
        dimension_counts={"attention": count},
    )


def _frontmatter_block(text: str) -> str:
    assert text.startswith("---\n")
    end = text.index("\n---", 4)
    return text[4:end]


def _part_files(obs_dir) -> list:
    return sorted(obs_dir.glob("attention.part-*.md"))


class TestWikiExporterPagination:
    """Tests for max_file_bytes driven part sharding."""

    # 该上限下每条 Observation（主表行 + 详情表行）恰好独占一个 part。
    CAP = 2400

    def test_small_dimension_matches_unlimited_byte_for_byte(self, tmp_path):
        unlimited_dir = tmp_path / "unlimited"
        capped_dir = tmp_path / "capped"
        WikiExporter(str(unlimited_dir), max_file_bytes=0).export_batch(_paged_batch(3))
        WikiExporter(str(capped_dir), max_file_bytes=10_000_000).export_batch(
            _paged_batch(3)
        )

        unlimited_page = unlimited_dir / "L3-Observations" / "attention.md"
        capped_page = capped_dir / "L3-Observations" / "attention.md"
        assert capped_page.read_bytes() == unlimited_page.read_bytes()
        assert _part_files(capped_dir / "L3-Observations") == []

    def test_oversized_dimension_paginates_at_observation_boundaries(self, tmp_path):
        batch = _paged_batch(5)
        WikiExporter(str(tmp_path), max_file_bytes=self.CAP).export_batch(batch)

        obs_dir = tmp_path / "L3-Observations"
        index_text = (obs_dir / "attention.md").read_text(encoding="utf-8")
        index_frontmatter = _frontmatter_block(index_text)
        parts = _part_files(obs_dir)

        assert len(parts) == 5
        assert [part.name for part in parts] == [
            f"attention.part-{index:03d}.md" for index in range(1, 6)
        ]
        assert "part_count: 5" in index_frontmatter
        assert "paginated: true" in index_frontmatter
        assert "## 观察值" not in index_text
        link_positions = [
            index_text.index(f"[[attention.part-{index:03d}]]")
            for index in range(1, 6)
        ]
        assert link_positions == sorted(link_positions)

        revisions = {
            line
            for text in [index_text, *(p.read_text(encoding="utf-8") for p in parts)]
            for line in _frontmatter_block(text).splitlines()
            if line.startswith("canonical_revision:")
        }
        assert len(revisions) == 1

        for part in parts:
            assert len(part.read_bytes()) <= self.CAP
        for index, observation in enumerate(batch.observations, 1):
            row = f"| `{observation.id}` |"
            detail = f"| {index} | frequency |"
            holders = [
                part.read_text(encoding="utf-8")
                for part in parts
                if observation.id in part.read_text(encoding="utf-8")
            ]
            assert len(holders) == 1
            assert row in holders[0]
            assert detail in holders[0]

        for part in parts:
            part_text = part.read_text(encoding="utf-8")
            main_rows = [
                line for line in part_text.splitlines() if line.startswith("| `obs-att-")
            ]
            detail_rows = [
                line
                for line in part_text.splitlines()
                if line.startswith("| ")
                and " | frequency | " in line
                and not line.startswith("| `")
            ]
            assert len(main_rows) == len(detail_rows) > 0

        part_frontmatter = _frontmatter_block(parts[0].read_text(encoding="utf-8"))
        assert "part_index: 1" in part_frontmatter
        assert "part_count: 5" in part_frontmatter
        assert "calibration_set_hash:" in part_frontmatter

    def test_full_reexport_removes_stale_parts(self, tmp_path):
        exporter = WikiExporter(str(tmp_path), max_file_bytes=self.CAP)
        exporter.export_batch(_paged_batch(12), full=True)
        obs_dir = tmp_path / "L3-Observations"
        assert len(_part_files(obs_dir)) == 12

        exporter.export_batch(_paged_batch(8), full=True)

        parts = _part_files(obs_dir)
        assert [part.name for part in parts] == [
            f"attention.part-{index:03d}.md" for index in range(1, 9)
        ]
        assert not (obs_dir / "attention.part-009.md").exists()
        index_text = (obs_dir / "attention.md").read_text(encoding="utf-8")
        assert "part_count: 8" in _frontmatter_block(index_text)
        for part in parts:
            assert len(part.read_bytes()) <= self.CAP

    def test_incremental_reexport_removes_stale_parts(self, tmp_path):
        exporter = WikiExporter(str(tmp_path), max_file_bytes=self.CAP)
        exporter.export_batch(_paged_batch(4), full=False)
        obs_dir = tmp_path / "L3-Observations"
        assert len(_part_files(obs_dir)) == 4

        exporter.export_batch(_paged_batch(2), full=False)

        assert [part.name for part in _part_files(obs_dir)] == [
            "attention.part-001.md",
            "attention.part-002.md",
        ]

    def test_incremental_reconcile_tombstones_dimension_parts(self, tmp_path):
        exporter = WikiExporter(str(tmp_path), max_file_bytes=self.CAP)
        exporter.export_batch(_paged_batch(3), full=False)
        obs_dir = tmp_path / "L3-Observations"
        assert len(_part_files(obs_dir)) == 3

        exporter.export_batch(
            ObservationBatch(),
            full=False,
            reconcile_dimensions=(Dimension.ATTENTION,),
        )

        assert not (obs_dir / "attention.md").exists()
        assert _part_files(obs_dir) == []

    def test_receipts_bind_the_index_page(self, tmp_path):
        batch = _paged_batch(4)
        calibrated = batch.observations[0]
        calibrated.calibration_revision_id = "cogrev-calibration-001"
        calibrated.calibration_input_hash = "sha256:input"
        calibrated.calibration_spec_hash = "sha256:spec"
        calibrated.calibration_record_hash = "sha256:record"
        calibrated.confidence = 0.9
        reports = {
            calibrated.id: CalibrationReport(
                observation_id=calibrated.id,
                original_confidence=0.8,
                calibrated_confidence=0.9,
                overall_verdict="confirmed",
                validations=[],
                suggestions=[],
                calibration_revision_id="cogrev-calibration-001",
                calibration_record_hash="sha256:record",
                calculation_input_hash="sha256:input",
                validator_spec_hash="sha256:spec",
            ),
        }

        receipts = WikiExporter(str(tmp_path), max_file_bytes=self.CAP).export_batch(
            batch,
            calibration_reports=reports,
        )

        obs_dir = tmp_path / "L3-Observations"
        index_path = obs_dir / "attention.md"
        assert len(_part_files(obs_dir)) == 4
        receipt = receipts[calibrated.id]
        assert receipt.file_path == str(index_path)
        expected_hash = "sha256:" + hashlib.sha256(index_path.read_bytes()).hexdigest()
        assert receipt.content_hash == expected_hash

    def test_zero_cap_matches_legacy_default(self, tmp_path):
        default_dir = tmp_path / "default"
        zero_dir = tmp_path / "zero"
        default_exporter = WikiExporter(str(default_dir))
        assert default_exporter.max_file_bytes == 0
        default_exporter.export_batch(_paged_batch(6))
        WikiExporter(str(zero_dir), max_file_bytes=0).export_batch(_paged_batch(6))

        default_page = default_dir / "L3-Observations" / "attention.md"
        zero_page = zero_dir / "L3-Observations" / "attention.md"
        assert default_page.read_bytes() == zero_page.read_bytes()
        assert _part_files(default_dir / "L3-Observations") == []
        assert _part_files(zero_dir / "L3-Observations") == []
