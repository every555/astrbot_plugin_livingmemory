"""P0-2 反射弧② 集成测试：L1.5 传递闭包必须正确挂进 conflict_detector.detect_all。

源码级断言（挂载证据）+ 路径推断单测。挂载铁律：try/except 降级、old_memory_id=0、conflict_type=transitive。
"""
import importlib.util
import os
import unittest

LM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CD_PATH = os.path.join(LM_ROOT, "core", "v2", "conflict_detector.py")
TC_PATH = os.path.join(LM_ROOT, "core", "v2", "transitive_closure.py")

_spec = importlib.util.spec_from_file_location("transitive_closure", TC_PATH)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)


class TestReflex2Integration(unittest.TestCase):
    def setUp(self):
        with open(CD_PATH, "r", encoding="utf-8") as f:
            self.src = f.read()

    def test_import_mounted(self):
        """挂载证据1：conflict_detector 导入闭包检查器。"""
        self.assertIn("from .transitive_closure import", self.src)

    def test_transitive_level_present(self):
        """挂载证据2：存在 L1.5 传递闭包级 + transitive 类型 + old=0。"""
        self.assertIn("传递闭包", self.src)
        self.assertIn("transitive", self.src)
        self.assertIn("old_memory_id=0", self.src)

    def test_degradation_guarded(self):
        """挂载证据3：免疫降级铁律——try/except 包裹，失败不炸入库。"""
        self.assertIn("L1.5 传递闭包降级", self.src)

    def test_db_path_inference(self):
        """路径推断：4级上跳命中真库（plugin_data 的 ontology.db）。"""
        p = tc.ontology_db_path()
        self.assertTrue(p.endswith("ontology.db"), f"推断失败: {p}")
        self.assertTrue(os.path.isfile(p), f"真库不存在: {p}")


if __name__ == "__main__":
    unittest.main(verbosity=2)