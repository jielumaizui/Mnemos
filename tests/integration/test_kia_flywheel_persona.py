"""
Apollon _run_cognitive_decision_flywheel 画像驱动闭环测试

覆盖：
- 加载当前 persona/blindspot 并传给 CognitiveDecisionFlywheel
- 调用 run_cycle() 而非仅扫描 Wiki 候选
- persona_driven 结果出现在输出中
- 画像加载失败时回退到无画像模式
"""


class FakePreferenceProfile:
    version = 1


class FakeBlindSpotProfile:
    pass


class FakePersonaStore:
    def __init__(self, raise_on_load=False):
        self.raise_on_load = raise_on_load

    def load_persona(self):
        if self.raise_on_load:
            raise RuntimeError("画像加载失败")
        return FakePreferenceProfile(), FakeBlindSpotProfile()


class FakeFlywheel:
    """记录初始化参数和调用历史的 fake flywheel"""

    def __init__(self, wiki_base=None, db_path=None, persona=None, blindspot=None):
        self.init_kwargs = {
            "wiki_base": wiki_base,
            "db_path": db_path,
            "persona": persona,
            "blindspot": blindspot,
        }
        self.run_cycle_called = False

    def run_cycle(self):
        self.run_cycle_called = True
        return {
            "wiki_to_cognitive_decision": [],
            "behavior_to_cognitive_decision": [],
            "skill_to_cognitive_decision": [],
            "persona_driven": {"gaps": [], "paths": [], "tasks": []},
            "executed": {"count": 0, "actions": [], "errors": []},
            "report_path": "/tmp/fake_report.md",
        }


def test_run_cognitive_decision_flywheel_passes_persona(monkeypatch):
    """画像正常加载时，persona/blindspot 传给 flywheel 并调用 run_cycle"""
    import core.kia.ixion as ixion_mod
    from integrations import apollon

    captured = {"instance": None}

    def _fake_flywheel(wiki_base=None, db_path=None, persona=None, blindspot=None):
        instance = FakeFlywheel(wiki_base, db_path, persona, blindspot)
        captured["instance"] = instance
        return instance

    monkeypatch.setattr(ixion_mod, "CognitiveDecisionFlywheel", _fake_flywheel)
    monkeypatch.setattr(
        "core.persona.delphi.PersonaStore",
        lambda: FakePersonaStore(),
    )

    results = []
    apollon._run_cognitive_decision_flywheel(results)

    instance = captured["instance"]
    assert instance is not None
    assert instance.run_cycle_called is True
    assert instance.init_kwargs["persona"] is not None
    assert instance.init_kwargs["blindspot"] is not None
    assert any("画像驱动分析已执行" in r for r in results)


def test_run_cognitive_decision_flywheel_falls_back_when_persona_load_fails(monkeypatch):
    """画像加载失败时，仍能用无画像模式继续"""
    import core.kia.ixion as ixion_mod
    from integrations import apollon

    captured = {"instance": None}

    def _fake_flywheel(wiki_base=None, db_path=None, persona=None, blindspot=None):
        instance = FakeFlywheel(wiki_base, db_path, persona, blindspot)
        captured["instance"] = instance
        return instance

    monkeypatch.setattr(ixion_mod, "CognitiveDecisionFlywheel", _fake_flywheel)
    monkeypatch.setattr(
        "core.persona.delphi.PersonaStore",
        lambda: FakePersonaStore(raise_on_load=True),
    )

    results = []
    apollon._run_cognitive_decision_flywheel(results)

    instance = captured["instance"]
    assert instance is not None
    assert instance.run_cycle_called is True
    assert instance.init_kwargs["persona"] is None
    assert instance.init_kwargs["blindspot"] is None
    assert not any("认知决策飞轮: 失败" in r for r in results)


def test_run_cognitive_decision_flywheel_reports_asset_candidates(monkeypatch):
    """run_cycle 返回认知决策资产候选时，结果摘要应包含候选数量"""
    import core.kia.ixion as ixion_mod
    from integrations import apollon

    class FakeFlywheelWithInsights(FakeFlywheel):
        def run_cycle(self):
            return {
                "wiki_to_cognitive_decision": [object(), object()],
                "behavior_to_cognitive_decision": [object()],
                "skill_to_cognitive_decision": [],
                "persona_driven": {},
                "executed": {"count": 0, "actions": [], "errors": []},
                "report_path": "",
            }

    monkeypatch.setattr(ixion_mod, "CognitiveDecisionFlywheel", FakeFlywheelWithInsights)
    monkeypatch.setattr(
        "core.persona.delphi.PersonaStore",
        lambda: FakePersonaStore(),
    )

    results = []
    apollon._run_cognitive_decision_flywheel(results)

    assert any("3 个认知决策资产候选" in r for r in results)
