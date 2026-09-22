"""P0-2 传递闭包单元测试（TDD RED 先行）。

喜鹊测试原子化：图上 测试A -is_a-> 鸟、鸟 -has-> 翅膀，注入「测试A没有翅膀」必须被抓。
用例：全链冲突 / 无矛盾放行 / 肯定句不误报 / 3跳截断 + is_a 传递。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

LM_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TC_PATH = os.path.join(LM_ROOT, chr(99)+chr(111)+chr(114)+chr(101), 'v2', 'transitive_closure.py')

# 零依赖加载：绕过 core.v2.__init__ 的插件依赖链（family_bus 同款教训）
import importlib.util
_spec = importlib.util.spec_from_file_location('transitive_closure', TC_PATH)
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

SQL_ENT = 'CREATE TABLE entities (id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, properties TEXT, created_at TEXT, updated_at TEXT)'
SQL_REL = 'CREATE TABLE relations (id TEXT PRIMARY KEY, from_id TEXT NOT NULL, relation_type TEXT NOT NULL, to_id TEXT NOT NULL, properties TEXT, created_at TEXT)'


def _build_test_db(path: str) -> None:
    """建测试图（与 ontology.db 同构的双表，无 DEFAULT 简化建表）。"""
    con = sqlite3.connect(path)
    con.execute(SQL_ENT)
    con.execute(SQL_REL)
    ents = {
        "ea": ("concept", {"name": "测试A"}),
        "eb": ("concept", {"name": "鸟"}),
        "ew": ("concept", {"name": "翅膀"}),
        "eb1": ("concept", {"name": "链B"}),
        "ec1": ("concept", {"name": "链C"}),
        "ed1": ("concept", {"name": "链D"}),
        "ee1": ("concept", {"name": "链E"}),
    }
    for eid, (etype, props) in ents.items():
        con.execute(
            "INSERT INTO entities (id, entity_type, properties) VALUES (?, ?, ?)",
            (eid, etype, json.dumps(props, ensure_ascii=False)),
        )
    rels = [
        ("r1", "ea", "is_a", "eb"),
        ("r2", "eb", "has", "ew"),
        ("r3", "ea", "is_a", "eb1"),
        ("r4", "eb1", "is_a", "ec1"),
        ("r5", "ec1", "is_a", "ed1"),
        ("r6", "ed1", "has", "ee1"),
    ]
    for rid, f, rt, t2 in rels:
        con.execute(
            "INSERT INTO relations (id, from_id, relation_type, to_id) VALUES (?, ?, ?, ?)",
            (rid, f, rt, t2),
        )
    con.commit()
    con.close()


class TestTransitiveClosure(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        _build_test_db(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_magpie_full_chain(self):
        """用例1 喜鹊全链：2跳推导 测试A-has->翅膀 vs 新记忆「没有翅膀」=> 必须出冲突。"""
        hits = tc.check_transitive("测试A没有翅膀", self.db_path)
        self.assertEqual(len(hits), 1, f"应恰好1条冲突候选, got {hits}")
        hit = hits[0]
        self.assertIn("翅膀", hit["reason"])
        self.assertGreaterEqual(hit["confidence"], 0.7)
        self.assertEqual(len(hit["evidence_chain"]), 2)
        self.assertTrue(any("鸟" in step for step in hit["evidence_chain"]))

    def test_no_triple_pass(self):
        """用例2 无三元组放行：「测试A会唱歌」抽不出图上三元组 => 零候选。"""
        hits = tc.check_transitive("测试A会唱歌", self.db_path)
        self.assertEqual(hits, [])

    def test_affirmative_no_false_alarm(self):
        """用例3 肯定句不误报：「鸟有翅膀」与图一致（非否定）=> 零候选。"""
        hits = tc.check_transitive("鸟有翅膀", self.db_path)
        self.assertEqual(hits, [])

    def test_hop_cutoff_and_is_a_chain(self):
        """用例4 3跳截断 + is_a 传递：A is_a 链D(3段)可推导；A has 链E(4段)被截断。"""
        neg_e = tc.check_transitive("测试A没有链E", self.db_path)
        self.assertEqual(neg_e, [], "4跳推导必须被 max_hops=3 截断")
        neg_d = tc.check_transitive("测试A不是链D", self.db_path)
        self.assertEqual(len(neg_d), 1, "is_a 三段传递 + 「不是」否定 => 应出冲突")
        self.assertEqual(len(neg_d[0]["evidence_chain"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)