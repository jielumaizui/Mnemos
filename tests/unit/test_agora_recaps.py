from datetime import datetime


def test_check_pending_recaps_returns_force_decision_fields(monkeypatch):
    from core.app.forced_retrospective import ForceDecision, RecapTask
    import core.app.forced_retrospective as forced_module
    from integrations.agora import MCPServer

    class FakeForcedRetrospective:
        def get_pending_system_recaps(self):
            return [
                RecapTask(
                    task_id="recap-1",
                    severity="high",
                    topic="Docker 配置复盘",
                    source="system",
                    created_at=datetime.now().isoformat(),
                    age_days=4,
                    same_type_count=2,
                )
            ]

        def list_user_reminders(self):
            return []

        def should_force_open(self, recap, user_context=None):
            return ForceDecision(
                should_force_open=True,
                score=5,
                reason="severity=high(+2); age=4d(>=3,+1); same_type=2(>=2,+2)",
                channel="force_open",
            )

    monkeypatch.setattr(forced_module, "ForcedRetrospective", FakeForcedRetrospective)

    result = MCPServer()._tool_check_pending_recaps(limit=1)

    assert result["success"] is True
    assert result["items"][0]["should_force_open"] is True
    assert result["items"][0]["score"] == 5
    assert result["items"][0]["channel"] == "force_open"
    assert result["items"][0]["reasons"] == [
        "severity=high(+2)",
        "age=4d(>=3,+1)",
        "same_type=2(>=2,+2)",
    ]
