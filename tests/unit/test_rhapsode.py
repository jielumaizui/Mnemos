class FakeStore:
    def __init__(self, data=None):
        self.data = data

    def get_latest_persona_version(self):
        return self.data


def test_load_previous_profile_requires_immediate_previous_version():
    from core.persona.rhapsode import SelfReportGenerator

    generator = SelfReportGenerator(store=FakeStore({"version": 2}))

    assert generator._load_previous_profile(4) is None


def test_load_previous_profile_accepts_immediate_previous_version():
    from core.persona.rhapsode import SelfReportGenerator

    generator = SelfReportGenerator(store=FakeStore({"version": 3}))

    previous = generator._load_previous_profile(4)

    assert previous is not None
    assert previous.version == 3


def test_generate_predictions_keeps_mvp_three_rules():
    from core.persona.pythia import PreferenceProfile
    from core.persona.rhapsode import DimensionChange, SelfReportGenerator

    current = PreferenceProfile()
    current.energy.startup_difficulty = 0.9
    current.cognitive.abstraction = 0.9
    current.cognitive.system_view = 0.9
    current.value.depth_vs_breadth = 0.8
    current.value.innovation_vs_safety = 0.9
    changes = [
        DimensionChange("抽象↔具象", "cognitive", 0.5, 0.9, 0.4, "growing", "major"),
        DimensionChange("系统↔单点", "cognitive", 0.5, 0.9, 0.4, "growing", "major"),
        DimensionChange("深度↔广度", "value", 0.5, 0.8, 0.3, "growing", "major"),
    ]

    predictions = SelfReportGenerator(store=FakeStore())._generate_predictions(current, changes)

    assert [p.area for p in predictions] == ["复杂问题解决", "决策质量", "知识视野"]


def test_recommendations_keep_at_most_two_per_tag():
    from core.persona.pythia import PreferenceProfile
    from core.persona.rhapsode import DimensionChange, SelfReportGenerator

    current = PreferenceProfile()
    changes = [
        DimensionChange("专注深度", "energy", 0.1, 0.9, 0.8, "shifted", "major"),
        DimensionChange("启动难度", "energy", 0.1, 0.9, 0.8, "shifted", "major"),
        DimensionChange("续航模式", "energy", 0.1, 0.9, 0.8, "shifted", "major"),
    ]

    recs = SelfReportGenerator(store=FakeStore())._generate_recommendations(current, changes, [])

    assert sum(1 for rec in recs if rec.startswith("【能量调整】")) == 2


def test_save_report_uses_reports_dir(tmp_path, monkeypatch):
    import core.persona.rhapsode as rhapsode
    from core.persona.pythia import PreferenceProfile
    from core.persona.rhapsode import SelfReport, SelfReportGenerator

    monkeypatch.setattr(rhapsode, "REPORTS_DIR", tmp_path / "99-Reports")
    report = SelfReport(
        period_label="2026-Q2",
        generated_at="2026-05-27T00:00:00",
        persona_current=PreferenceProfile(version=1),
        persona_previous=None,
        dimension_changes=[],
        blindspot_changes=[],
        predictions=[],
        recommendations=[],
        raw_markdown="# report",
    )

    path = SelfReportGenerator(store=FakeStore()).save_report(report)

    assert path == tmp_path / "99-Reports" / "画像周报-2026-Q2.md"
    assert path.read_text(encoding="utf-8") == "# report"


def test_markdown_includes_cold_pages(monkeypatch):
    from core.persona.pythia import PreferenceProfile
    from core.persona.rhapsode import SelfReport, SelfReportGenerator

    class ColdPage:
        wiki_path = "cold.md"
        title = "冷页面"
        heat_score = 1.2
        quality_score = 32.0

    monkeypatch.setattr(SelfReportGenerator, "_get_cold_pages_for_report", staticmethod(lambda limit=5: [ColdPage()]))
    report = SelfReport(
        period_label="2026-Q2",
        generated_at="2026-05-27T00:00:00",
        persona_current=PreferenceProfile(version=1),
        persona_previous=None,
        dimension_changes=[],
        blindspot_changes=[],
        predictions=[],
        recommendations=[],
    )

    markdown = SelfReportGenerator(store=FakeStore())._to_markdown(report)

    assert "## 冷却知识" in markdown
    assert "冷页面" in markdown
