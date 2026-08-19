"""
Tests for core.prometheus_fire

Covers: QueueDistillTask 队列任务 DTO。
旧 AgentDelegate 委托模式已退役，当前路径由 HephaestusWorker 直接消费 DTO。
"""

from core.prometheus_fire import QueueDistillTask


class TestQueueDistillTask:
    def test_init(self):
        task = QueueDistillTask(
            session_id="sess_1",
            messages=[{"role": "user", "content": "hi"}],
            meta={"source": "wiki", "working_dir": "/tmp"},
        )
        assert task.session_id == "sess_1"
        assert len(task.messages) == 1
        assert task.meta["source"] == "wiki"

    def test_to_dict(self):
        task = QueueDistillTask(
            session_id="sess_1",
            messages=[{"role": "user", "content": "hi"}],
            meta={"priority": 1},
        )
        d = task.to_dict()
        assert d["session_id"] == "sess_1"
        assert d["messages"][0]["role"] == "user"
