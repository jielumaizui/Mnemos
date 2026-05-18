"""冒烟测试 - 验证核心模块可导入且基本功能正常"""

import sys
import os
import tempfile
import unittest
from pathlib import Path

# 确保测试时能找到项目根目录的模块
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestConfig(unittest.TestCase):
    """配置系统冒烟测试"""

    def test_config_import(self):
        """Config 模块可导入"""
        from core.config import Config, get_config
        self.assertIsNotNone(Config)
        self.assertIsNotNone(get_config)

    def test_config_paths(self):
        """配置路径属性返回 Path 对象"""
        from core.config import get_config
        config = get_config()
        self.assertIsInstance(config.wiki_dir, Path)
        self.assertIsInstance(config.data_dir, Path)
        self.assertIsInstance(config.claude_data_dir, Path)
        self.assertIsInstance(config.config_path, Path)

    def test_data_dir_unified(self):
        """data_dir 和 claude_data_dir 是不同路径"""
        from core.config import get_config
        config = get_config()
        # 运行时数据应该在 ~/.mnemos/
        self.assertIn(".mnemos", str(config.data_dir))
        # Claude 数据源应该在 ~/.claude/
        self.assertIn(".claude", str(config.claude_data_dir))


class TestSignalStore(unittest.TestCase):
    """信号存储冒烟测试"""

    def test_signal_store_import(self):
        """SignalStore 模块可导入"""
        from core.persona.signal_store import SignalStore, get_signal_store
        self.assertIsNotNone(SignalStore)
        self.assertIsNotNone(get_signal_store)

    def test_database_path(self):
        """数据库路径在 ~/.mnemos/ 下"""
        from core.persona.signal_store import SIGNAL_DB_PATH
        self.assertIn(".mnemos", str(SIGNAL_DB_PATH))


class TestPersonaImports(unittest.TestCase):
    """用户画像模块冒烟测试"""

    def test_all_persona_modules(self):
        """所有 persona 子模块可导入"""
        modules = [
            "core.persona.signal_store",
            "core.persona.signal_collector",
            "core.persona.preference_analyzer",
            "core.persona.blindspot_analyzer",
            "core.persona.persona_store",
            "core.persona.report_generator",
        ]
        for mod in modules:
            with self.subTest(module=mod):
                __import__(mod)


class TestKIAImports(unittest.TestCase):
    """KIA 模块冒烟测试"""

    def test_all_kia_modules(self):
        """所有 kia 子模块可导入"""
        modules = [
            "core.kia.task_classifier",
            "core.kia.pre_flight_injector",
            "core.kia.auto_retrospective",
            "core.kia.skill_wiki_flywheel",
            "core.kia.connect_worker",
            "core.kia.distillation_queue",
            "core.kia.knowledge_scheduler",
        ]
        for mod in modules:
            with self.subTest(module=mod):
                __import__(mod)


class TestIntegrationImports(unittest.TestCase):
    """集成层模块冒烟测试"""

    def test_all_integration_modules(self):
        """所有 integrations 模块可导入"""
        modules = [
            "integrations.memos_sdk",
            "integrations.wiki_reader",
            "integrations.ai_context_reader",
            "integrations.claude_integration",
            "integrations.mcp_server",
        ]
        for mod in modules:
            with self.subTest(module=mod):
                __import__(mod)


class TestCLI(unittest.TestCase):
    """CLI 冒烟测试"""

    def test_cli_import(self):
        """CLI 模块可导入"""
        import mnemos_cli
        self.assertTrue(hasattr(mnemos_cli, "main"))


class TestNoHardcodedPaths(unittest.TestCase):
    """检查没有遗留的硬编码路径"""

    def test_no_claude_in_runtime_paths(self):
        """运行时数据路径不应该硬编码 ~/.claude"""
        from core.config import get_config
        config = get_config()

        # 运行时数据目录应该是 ~/.mnemos/
        self.assertIn(".mnemos", str(config.data_dir))

        # signal_store 数据库路径
        from core.persona.signal_store import SIGNAL_DB_PATH
        self.assertIn(".mnemos", str(SIGNAL_DB_PATH))


if __name__ == "__main__":
    unittest.main()
