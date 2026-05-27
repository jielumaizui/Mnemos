"""
pre_flight_injector 单元测试

覆盖项：
- PreFlightInjector 构建 system prompt
- 画像上下文注入
- 缺省画像兜底
"""

import sys
import tempfile
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from core.pre_flight_injector import PreFlightInjector, PersonaContext


class TestPreFlightInjector(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.persona_dir = Path(self.tmpdir.name) / "persona"
        self.persona_dir.mkdir()

        # 创建画像摘要
        summary = {
            "tech_stack": ["Python", "Redis", "Kubernetes"],
            "work_hours": "21:00-01:00",
            "style": "简洁直接",
            "blindspots": ["前端技术", "UI 设计"],
            "interaction_mode": "连续追问式",
        }
        summary_path = self.persona_dir / "profile_summary.json"
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

        self.injector = PreFlightInjector(persona_dir=self.persona_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_build_system_prompt_contains_profile(self):
        """system prompt 包含画像信息"""
        prompt = self.injector.build_system_prompt()
        self.assertIn("Python", prompt)
        self.assertIn("Redis", prompt)
        self.assertIn("21:00-01:00", prompt)
        self.assertIn("盲区", prompt)

    def test_build_system_prompt_contains_distill_rules(self):
        """system prompt 包含蒸馏原则"""
        prompt = self.injector.build_system_prompt()
        self.assertIn("蒸馏原则", prompt)
        self.assertIn("技术栈", prompt)

    def test_inject_to_prompt_returns_dict(self):
        """inject_to_prompt 返回 system/user 字典"""
        result = self.injector.inject_to_prompt("请帮我总结这段对话")
        self.assertIn("system", result)
        self.assertIn("user", result)
        self.assertIn("总结", result["user"])

    def test_fallback_when_no_profile(self):
        """无画像时使用兜底"""
        injector = PreFlightInjector(persona_dir=Path("/nonexistent"))
        prompt = injector.build_system_prompt()
        self.assertIn("知识蒸馏助手", prompt)
        # 空画像不崩溃
        self.assertNotIn("Python", prompt)


if __name__ == "__main__":
    unittest.main()
