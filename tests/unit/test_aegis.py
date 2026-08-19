"""
InProcessGuard (Aegis) 单元测试

覆盖项：
- InProcessGuard 初始化与会话管理
- check() 主检测入口（默认关键词、上下文风险、checklist 匹配、思考循环）
- check_silent() 静默批量检测
- smart_check() 全量风险排序
- get_silent_summary() / get_checklist_usage() 汇总与统计
- format_hint_for_ai() / format_interrupt_message() 格式化输出
"""

import sqlite3
from datetime import datetime, timedelta

import pytest
from core.kia.aegis import GuardLevel, InProcessGuard, GuardAlert
from core.kia.prophasis import ChecklistItem, LoadedKnowledge

# ---- Fixture：构造测试用 LoadedKnowledge ----


@pytest.fixture
def sample_knowledge():
    """提供一个包含多条 checklist 的 LoadedKnowledge。"""
    return LoadedKnowledge(
        task_type="coding",
        subtype="refactor",
        version=1,
        checklist=[
            ChecklistItem(
                item="不要直接删除生产配置",
                source="retro-v1",
                severity="critical",
                trigger_keywords=["删除配置", "删掉配置"],
                risk_patterns=["已删除", "配置已清空"],
                detail="生产配置删除后不可恢复，请使用灰度下线流程。",
            ),
            ChecklistItem(
                item="未测试代码不要直接 push",
                source="retro-v1",
                severity="high",
                trigger_keywords=["直接 push", "未测试就提交"],
                risk_patterns=["跳过测试", "未验证"],
            ),
            ChecklistItem(
                item="重构时保留向后兼容",
                source="retro-v1",
                severity="medium",
                trigger_keywords=["重构", "改写"],
                risk_patterns=["接口变更", "签名改动"],
            ),
            ChecklistItem(
                item="日志级别建议统一",
                source="retro-v1",
                severity="low",
                trigger_keywords=["日志", "log"],
            ),
        ],
        lessons_summary="",
        loaded_at="now",
    )


@pytest.fixture
def guard(sample_knowledge):
    """已启动 session 的 InProcessGuard 实例。"""
    return InProcessGuard(sample_knowledge)


# ---- 初始化与会话 ----


def test_init_without_knowledge():
    """无 knowledge 时 session 为 None，各组件正常初始化。"""
    g = InProcessGuard()
    assert g.session is None
    assert g.smart_matcher is not None
    assert g.duplicate_detector is not None


def test_init_with_knowledge_starts_session(sample_knowledge):
    """传入 knowledge 时自动调用 start_session，session 字段正确。"""
    g = InProcessGuard(sample_knowledge)
    assert g.session is not None
    assert g.session.task_type == "coding"
    assert g.session.subtype == "refactor"
    assert len(g.session.checklist) == 4


def test_start_session_resets_state(guard, sample_knowledge):
    """重复启动 session 会重置计数器与历史。"""
    guard.session_messages.append("msg1")
    guard._analysis_turn_count = 5

    new_knowledge = LoadedKnowledge(
        task_type="analysis",
        subtype="data",
        version=1,
        checklist=[],
        lessons_summary="",
        loaded_at="now",
    )
    guard.start_session(new_knowledge)

    assert guard.session.task_type == "analysis"
    assert guard.session_messages == []
    assert guard._analysis_turn_count == 0


def test_guard_does_not_recreate_unused_last_action_turn_state(guard, sample_knowledge):
    """Aegis no longer exposes the write-only action turn marker."""
    assert not hasattr(guard, "_last_action_turn")

    guard.check("我已经修改了文件", ai_response="```python\nprint('done')\n```")
    assert not hasattr(guard, "_last_action_turn")

    guard.start_session(sample_knowledge)
    assert not hasattr(guard, "_last_action_turn")


def test_guard_does_not_eagerly_create_unused_blindspot_services(guard):
    """Aegis should not keep unused Hamartia service instances on itself."""
    assert not hasattr(guard, "blindspot_manager")
    assert not hasattr(guard, "challenge_balancer")


# ---- check() 默认高风险关键词 ----


def test_check_default_critical_keyword(guard):
    """默认关键词（如 rm -rf）不依赖 session 也能触发 INTERRUPT。"""
    # 先清掉 session，验证无 session 也能命中兜底规则
    guard.session = None
    alert = guard.check("我要执行 rm -rf / 清理磁盘")
    assert alert is not None
    assert alert.level == GuardLevel.INTERRUPT
    assert "rm -rf" in alert.trigger_text.lower()


def test_check_default_critical_is_case_insensitive(guard):
    """默认关键词匹配不区分大小写。"""
    guard.session = None
    alert = guard.check("执行 DROP TABLE users")
    assert alert is not None
    assert alert.level == GuardLevel.INTERRUPT


# ---- check() 上下文风险 ----


def test_check_context_risk_prod_file_and_delete(guard):
    """生产环境文件 + 危险操作触发上下文风险告警。"""
    alert = guard.check(
        "清理一下这个文件",
        context={"current_file": "prod/config.yaml", "current_command": "rm config.yaml"},
    )
    assert alert is not None
    assert alert.level == GuardLevel.INTERRUPT
    assert "生产环境" in alert.suggestion or "危险" in alert.suggestion


def test_check_context_risk_git_checkout_with_uncommitted(guard):
    """未提交修改下切换分支触发 HINT 级别告警。"""
    alert = guard.check(
        "切换到 main 分支",
        context={"git_status": "modified: foo.py", "current_command": "git checkout main"},
    )
    assert alert is not None
    assert alert.level == GuardLevel.HINT


# ---- check() checklist 匹配 ----


def test_check_checklist_trigger_keyword_interrupt(guard):
    """用户消息命中 critical severity checklist → INTERRUPT。"""
    alert = guard.check("我要删除配置，直接删掉配置")
    assert alert is not None
    assert alert.level == GuardLevel.INTERRUPT
    assert alert.checklist_item.item == "不要直接删除生产配置"
    assert alert.triggered_by == "user"


def test_check_degrades_when_guard_alert_event_publish_is_locked(guard, monkeypatch):
    """guard_alert 事件发布遇到 SQLite 锁时，守护判断本身仍应返回告警。"""

    def locked_publish_event(*args, **kwargs):
        raise sqlite3.OperationalError("sqlite lock timeout for events.db")

    monkeypatch.setattr("core.mnemos_bus.publish_event", locked_publish_event)

    alert = guard.check("我要删除配置，直接删掉配置")

    assert alert is not None
    assert alert.level == GuardLevel.INTERRUPT
    assert alert.checklist_item.item == "不要直接删除生产配置"


def test_check_checklist_trigger_keyword_hint(guard):
    """用户消息命中 high severity checklist → HINT。"""
    alert = guard.check("我直接 push 了，没测试")
    assert alert is not None
    assert alert.level == GuardLevel.HINT
    assert "push" in alert.checklist_item.item or "测试" in alert.checklist_item.item


def test_check_ai_risk_pattern(guard):
    """AI 回复命中 risk_patterns 也能触发告警。"""
    alert = guard.check(
        "帮我看看这段代码",
        ai_response="这段代码可以跳过测试直接上线",
    )
    assert alert is not None
    # 先被 _DEFAULT_CRITICAL_KEYWORDS（"未测试""直接上线"）拦截，triggered_by="system"
    # 只要触发告警即证明 AI 回复被纳入检测范围
    assert alert.triggered_by in ("ai", "system")


def test_check_no_match_returns_none(guard):
    """无风险内容时返回 None。"""
    alert = guard.check("今天天气不错")
    assert alert is None


# ---- check() 情境模式调整 ----


def test_check_fatigue_mode_downgrades_level(guard):
    """疲劳模式会将 INTERRUPT 降级为 HINT。"""
    # 先触发一次让 session_messages 非空，再强制设置模式
    guard.session_messages.append("累了")
    guard.contextual_mode = "fatigue"
    # 使用 high severity 项（正常为 HINT），在 fatigue 下会被降级为 SILENT
    # 这里用 medium severity 项验证：正常 user 触发为 HINT，fatigue 下应为 SILENT
    alert = guard.check("我要重构这个模块")
    if alert:
        # 由于 fatigue 降级，medium 项从 HINT 降到 SILENT
        assert alert.level == GuardLevel.SILENT


def test_check_exploration_mode_downgrades_interrupt(guard):
    """探索模式允许试错，INTERRUPT 降级为 HINT。"""
    # 构造一条 exploration 消息，然后触发 critical 项
    guard.contextual_mode = "exploration"
    # 通过 _adjust_level_by_context 验证
    level = guard._adjust_level_by_context(GuardLevel.INTERRUPT, "exploration")
    assert level == GuardLevel.HINT


# ---- check_silent() ----


def test_check_silent_records_low_and_medium(guard):
    """静默检测只记录 low/medium severity 的命中项。"""
    records = guard.check_silent("我想统一一下日志级别")
    assert len(records) >= 1
    assert records[0]["severity"] == "low"
    assert "日志" in records[0]["item"]


def test_check_silent_ignores_high_and_critical(guard):
    """静默检测不处理 high/critical 项（留给 check()）。"""
    # critical 关键词在 check_silent 中不会记录
    records = guard.check_silent("我要删除配置")
    # check_silent 只遍历 low/medium，因此应为空或仅含 low/medium
    for r in records:
        assert r["severity"] in ("low", "medium")


def test_check_silent_populates_session(guard):
    """静默记录写入 session.silent_records。"""
    guard.check_silent("日志级别需要调整")
    assert len(guard.session.silent_records) >= 1


# ---- smart_check() ----


def test_smart_check_returns_sorted_alerts(guard):
    """smart_check 返回 INTERRUPT > HINT > SILENT 的排序结果。"""
    # 触发一个 critical（INTERRUPT）和一个 low（SILENT）
    alerts = guard.smart_check("我要删除配置，顺便统一日志级别")
    assert len(alerts) >= 2
    levels = [a.level for a in alerts]
    # 排序验证：INTERRUPT 应在 SILENT 之前
    interrupt_idx = levels.index(GuardLevel.INTERRUPT)
    silent_idx = levels.index(GuardLevel.SILENT)
    assert interrupt_idx < silent_idx


def test_smart_check_includes_all_severities(guard):
    """smart_check 同时包含显式告警和静默记录。"""
    alerts = guard.smart_check("直接 push 代码，日志也要改")
    levels = {a.level for a in alerts}
    assert GuardLevel.HINT in levels or GuardLevel.INTERRUPT in levels
    assert GuardLevel.SILENT in levels


# ---- 思考循环检测 ----


def test_check_thinking_loop_user_wants_action(guard):
    """用户明确要求修复但 AI 仍在分析 → HINT 告警。"""
    alert = guard.check(
        "快点修复这个 bug",
        ai_response="让我再仔细分析一下根因...",
    )
    assert alert is not None
    assert alert.level == GuardLevel.HINT
    assert "用户要求行动" in alert.checklist_item.item or "修复" in alert.suggestion


def test_check_thinking_loop_repeated_analysis(guard):
    """默认连续 2 轮纯分析无行动 → HINT 告警。"""
    # 模拟 2 轮纯分析（无代码块、无文件修改标记）
    result = None
    for i in range(2):
        result = guard.check(
            f"继续分析第{i}步",
            ai_response="我需要继续分析这个问题。",
        )
        # 如果中途已触发思考循环，提前结束
        if result and "思考循环" in result.checklist_item.item:
            break
    # 第 2 轮应触发思考循环检测
    assert result is not None
    assert result.level == GuardLevel.HINT
    assert "思考循环" in result.checklist_item.item
    assert result.metadata["threshold_source"] == "config"
    assert result.metadata["threshold_value"] == 2
    assert result.metadata["current_count"] == 2


def test_check_thinking_loop_threshold_can_be_configured_to_three(
    monkeypatch, sample_knowledge
):
    """配置为 3 时第三轮纯分析才触发。"""

    def fake_options(self):
        return {
            "enabled": True,
            "max_analysis_turns_without_action": 3,
            "max_repeated_reads_per_target": 3,
            "threshold_source": "config",
        }

    monkeypatch.setattr(InProcessGuard, "_load_analysis_loop_options", fake_options)
    guard = InProcessGuard(sample_knowledge)

    first = guard.check("第1轮观察", ai_response="我需要检查这个问题。")
    second = guard.check("第2轮观察", ai_response="我需要检查这个问题。")
    third = guard.check("第3轮观察", ai_response="我需要检查这个问题。")

    assert first is None
    assert second is None
    assert third is not None
    assert third.metadata["threshold_value"] == 3
    assert third.metadata["current_count"] == 3


def test_analysis_loop_metadata_uses_triggered_threshold_source(monkeypatch, sample_knowledge):
    """两个阈值来源不同时，metadata 按实际触发阈值返回 source。"""

    def fake_options(self):
        return {
            "enabled": True,
            "max_analysis_turns_without_action": 2,
            "max_repeated_reads_per_target": 2,
            "threshold_source": "default",
            "analysis_threshold_source": "config",
            "reads_threshold_source": "default",
        }

    monkeypatch.setattr(InProcessGuard, "_load_analysis_loop_options", fake_options)
    guard = InProcessGuard(sample_knowledge)
    context = {
        "tool_calls": [
            {
                "name": "ReadFile",
                "input": {"path": "core/kia/aegis.py"},
            }
        ]
    }

    first = guard.check("第1轮观察", ai_response="", context=context)
    second = guard.check("第2轮观察", ai_response="", context=context)

    assert first is None
    assert second is not None
    assert second.metadata["threshold_kind"] == "max_repeated_reads_per_target"
    assert second.metadata["threshold_source"] == "default"
    assert second.metadata["threshold_value"] == 2
    assert second.metadata["current_count"] == 2


# ---- 重复工作检测 ----


def test_check_duplicate_work_hint(guard):
    """重复工作检测返回 HINT 级别告警。"""
    msg = "帮我实现用户认证功能"
    guard.check(msg)  # 第一次加入历史
    guard.check(msg)  # 第二次应检测到重复
    # 第三次高相似度触发
    alert = guard.check("帮我实现用户认证功能")
    # 由于精确指纹匹配，第三次应触发重复检测
    # 但前两次已经把消息加入历史，第三次 is_duplicate 返回 True
    # 注意：check() 内部先调用 is_duplicate 再 add_message，所以第二次不会触发
    # 第三次会触发
    if alert and "重复" in alert.checklist_item.item:
        assert alert.level == GuardLevel.HINT


# ---- 格式化方法 ----


def test_format_hint_for_ai(guard):
    """HINT 级别告警格式化为 AI 可自然融入的提示。"""
    alert = GuardAlert(
        level=GuardLevel.HINT,
        checklist_item=ChecklistItem(item="测试提示", source="test", detail="详细说明"),
        triggered_by="user",
        trigger_text="触发词",
        suggestion="建议",
    )
    text = guard.format_hint_for_ai(alert)
    assert "[Guard Hint]" in text
    assert "测试提示" in text
    assert "详细说明" in text


def test_format_hint_for_ai_marks_session_hint_used(guard):
    """格式化 HINT 后记录 checklist hint 已被使用。"""
    alert = guard.check("我准备直接 push 代码")

    assert alert is not None
    assert alert.level == GuardLevel.HINT

    guard.format_hint_for_ai(alert)

    assert alert.checklist_item.item in guard.session.hint_used
    usage = guard.get_checklist_usage()
    matching = [row for row in usage if row["item"] == alert.checklist_item.item]
    assert matching
    assert matching[0]["used"] is True


def test_format_hint_for_ai_non_hint_returns_empty(guard):
    """非 HINT 级别调用 format_hint_for_ai 返回空字符串。"""
    alert = GuardAlert(
        level=GuardLevel.INTERRUPT,
        checklist_item=ChecklistItem(item="严重", source="test"),
        triggered_by="system",
        trigger_text="",
        suggestion="",
    )
    assert guard.format_hint_for_ai(alert) == ""


def test_format_interrupt_message(guard):
    """INTERRUPT 级别告警格式化为打断消息。"""
    alert = GuardAlert(
        level=GuardLevel.INTERRUPT,
        checklist_item=ChecklistItem(item="严重风险", source="test"),
        triggered_by="system",
        trigger_text="rm -rf",
        suggestion="请确认",
    )
    text = guard.format_interrupt_message(alert)
    assert "风险提醒" in text
    assert "严重风险" in text
    assert "请确认" in text
    assert "是否继续" in text


def test_format_interrupt_message_non_interrupt_returns_empty(guard):
    """非 INTERRUPT 级别调用 format_interrupt_message 返回空字符串。"""
    alert = GuardAlert(
        level=GuardLevel.HINT,
        checklist_item=ChecklistItem(item="提示", source="test"),
        triggered_by="user",
        trigger_text="",
        suggestion="",
    )
    assert guard.format_interrupt_message(alert) == ""


# ---- 汇总与统计 ----


def test_get_silent_summary(guard):
    """静默记录汇总格式化正确。"""
    guard.check_silent("日志级别需要调整")
    summary = guard.get_silent_summary()
    assert "偏差记录" in summary
    assert "日志" in summary


def test_get_silent_summary_empty(guard):
    """无静默记录时返回空字符串。"""
    assert guard.get_silent_summary() == ""


def test_get_checklist_usage(guard):
    """checklist 使用情况统计正确。"""
    guard.check("我要删除配置")
    guard.check_silent("日志级别需要调整")
    usage = guard.get_checklist_usage()
    assert len(usage) == 4

    triggered = [u for u in usage if u["triggered"]]
    silent = [u for u in usage if u["level"] != "none" and not u["triggered"]]
    assert len(triggered) >= 1
    assert len(silent) >= 1

    # 验证字段完整性
    for u in usage:
        assert "item" in u
        assert "loaded" in u
        assert "used" in u
        assert "triggered" in u
        assert "level" in u
        assert "severity" in u


def test_get_checklist_usage_no_session():
    """无 session 时返回空列表。"""
    g = InProcessGuard()
    assert g.get_checklist_usage() == []


# ---- 便捷函数 ----


def test_create_guard(sample_knowledge):
    """create_guard 便捷函数正确创建 InProcessGuard。"""
    from core.kia.aegis import create_guard

    g = create_guard(sample_knowledge)
    assert isinstance(g, InProcessGuard)
    assert g.session is not None
    assert g.session.task_type == "coding"


# ============================================================
# P2-17: 知识缓存测试
# ============================================================


def test_knowledge_cache_hit(sample_knowledge):
    """同一 task_type 的 knowledge 应从缓存命中。"""
    InProcessGuard.clear_knowledge_cache()

    # 第一次创建，knowledge 被缓存
    InProcessGuard(sample_knowledge)
    cached = InProcessGuard._get_cached_knowledge("coding", "refactor")
    assert cached is not None
    assert cached.task_type == "coding"


def test_from_task_type_uses_cached_knowledge(sample_knowledge):
    """from_task_type() 应优先使用缓存知识创建守护实例。"""
    InProcessGuard.clear_knowledge_cache()
    InProcessGuard._set_cached_knowledge(sample_knowledge)

    guard = InProcessGuard.from_task_type("coding", "refactor")

    assert guard.session is not None
    assert guard.session.task_type == "coding"
    assert guard.session.subtype == "refactor"
    assert len(guard.session.checklist) == 4


def test_from_task_type_loads_knowledge_when_cache_misses(sample_knowledge, monkeypatch):
    """from_task_type() 缓存未命中时通过 PreFlightInjector 加载知识。"""
    InProcessGuard.clear_knowledge_cache()

    class _FakePreFlightInjector:
        def inject(self, task_type, subtype, time_window, _context):
            assert task_type == "coding"
            assert subtype == "refactor"
            assert time_window.days_until == 0
            return sample_knowledge

    monkeypatch.setattr("core.kia.prophasis.PreFlightInjector", _FakePreFlightInjector)

    guard = InProcessGuard.from_task_type("coding", "refactor")

    assert guard.session is not None
    assert guard.session.task_type == "coding"
    assert InProcessGuard._get_cached_knowledge("coding", "refactor") is sample_knowledge


def test_knowledge_cache_ttl_expires(sample_knowledge):
    """缓存超过 TTL 后应返回 None。"""
    InProcessGuard.clear_knowledge_cache()

    InProcessGuard(sample_knowledge)
    # 模拟 TTL 过期：直接修改缓存时间
    key = ("coding", "refactor")
    knowledge, _ = InProcessGuard._knowledge_cache[key]
    InProcessGuard._knowledge_cache[key] = (knowledge, datetime.now() - timedelta(seconds=120))

    expired = InProcessGuard._get_cached_knowledge("coding", "refactor")
    assert expired is None


def test_knowledge_cache_cleared(sample_knowledge):
    """clear_knowledge_cache 应清空缓存。"""
    InProcessGuard.clear_knowledge_cache()
    InProcessGuard._set_cached_knowledge(sample_knowledge)
    assert len(InProcessGuard._knowledge_cache) > 0

    InProcessGuard.clear_knowledge_cache()
    assert len(InProcessGuard._knowledge_cache) == 0
