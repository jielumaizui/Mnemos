from core.kia.aegis import GuardLevel, InProcessGuard, SmartMatcher
from core.kia.prophasis import ChecklistItem, LoadedKnowledge


def _knowledge():
    return LoadedKnowledge(
        task_type="analysis",
        subtype="data",
        version=1,
        checklist=[
            ChecklistItem(
                item="不要修改原始数据",
                source="test",
                severity="critical",
                trigger_keywords=["删除数据"],
                risk_patterns=["修改原始数据"],
            )
        ],
        lessons_summary="",
        loaded_at="now",
    )


def test_smart_matcher_downweights_negated_context():
    matcher = SmartMatcher()

    assert matcher.match_keyword("请不要删除数据，只讨论方案", ["删除数据"]) is None
    assert matcher.match_keyword("现在删除数据并继续", ["删除数据"])[1] >= 0.85


def test_critical_check_ignores_question_and_negation():
    guard = InProcessGuard(_knowledge())

    assert guard.check("不要删除数据，只分析备份方案") is None
    assert guard.check("删除数据会怎么样？") is None

    alert = guard.check("现在删除数据")
    assert alert.level == GuardLevel.INTERRUPT


def test_smart_check_returns_sorted_alerts():
    guard = InProcessGuard(_knowledge())

    alerts = guard.smart_check("现在删除数据")

    assert alerts
    assert alerts[0].level == GuardLevel.INTERRUPT
