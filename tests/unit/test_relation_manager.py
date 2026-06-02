"""
P0-1: RelationManager 编译与导入回归测试
确保 relation_manager.py 能正常编译、导入，基础方法可调用。
"""
import py_compile
import unittest
from pathlib import Path


class TestRelationManagerCompileAndImport(unittest.TestCase):
    """最小导入测试：编译通过 + 类可实例化 + 基础方法可调用"""

    def test_relation_manager_py_compiles(self):
        """编译不报错"""
        path = Path("core/kia/relation_manager.py")
        py_compile.compile(str(path), doraise=True)

    def test_relation_manager_imports(self):
        """模块能正常导入"""
        from core.kia.relation_manager import RelationManager
        self.assertTrue(callable(RelationManager))

    def test_relation_manager_basic_ops(self):
        """基础方法不抛异常"""
        from core.kia.relation_manager import RelationManager
        rm = RelationManager()
        # 空输入 distill 应返回空列表
        results = rm.add_from_distill({"entities": [], "relations": []})
        self.assertIsInstance(results, list)


if __name__ == "__main__":
    unittest.main()
