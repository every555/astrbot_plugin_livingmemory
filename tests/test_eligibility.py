"""P2-09 检索资格与保留解耦测试：死记忆滑出检索池但不删。"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.retrieval.eligibility import (
    EligibilityFilter,
    sync_parent_eligibility,
)


class _FakeResult:
    def __init__(self, doc_id):
        self.doc_id = doc_id
        self.score = 1.0


def _mk_doc_db(path, docs):
    """docs: list[(id, eligible_flag or None)]"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, metadata TEXT)")
    for i, flag in docs:
        meta = json.dumps({"retrieval_eligible": flag}) if flag is not None else json.dumps({"importance": 0.5})
        con.execute("INSERT INTO documents (id, metadata) VALUES (?,?)", (i, meta))
    con.commit(); con.close()


class TestEligibilityFilter:

    def test_removes_ineligible(self, tmp_path):
        db = str(tmp_path / "lm.db")
        _mk_doc_db(db, [(1, True), (2, False), (3, True)])
        f = EligibilityFilter(db)
        out = f.filter_results([_FakeResult(1), _FakeResult(2), _FakeResult(3)])
        assert [r.doc_id for r in out] == [1, 3]

    def test_missing_flag_means_eligible(self, tmp_path):
        db = str(tmp_path / "lm.db")
        _mk_doc_db(db, [(1, None), (2, True)])
        f = EligibilityFilter(db)
        out = f.filter_results([_FakeResult(1), _FakeResult(2)])
        assert len(out) == 2          # 存量记忆无标志=活着，不许误杀

    def test_disabled_passthrough(self, tmp_path):
        db = str(tmp_path / "lm.db")
        _mk_doc_db(db, [(1, False), (2, False)])
        f = EligibilityFilter(db, enabled=False)
        out = f.filter_results([_FakeResult(1), _FakeResult(2)])
        assert len(out) == 2          # 开关关=行为与升级前完全一致

    def test_empty_results_no_query(self, tmp_path):
        db = str(tmp_path / "lm.db")
        _mk_doc_db(db, [])
        f = EligibilityFilter(db)
        assert f.filter_results([]) == []

    def test_fetch_flags_returns_only_requested(self, tmp_path):
        db = str(tmp_path / "lm.db")
        _mk_doc_db(db, [(i, True) for i in range(1, 101)])
        f = EligibilityFilter(db)
        flags = f._fetch_flags([7, 9])
        assert set(flags.keys()) == {7, 9}   # 回表只查请求的id，不许全表扫

    def test_no_db_keeps_all(self, tmp_path):
        f = EligibilityFilter(str(tmp_path / "nonexistent.db"))
        out = f.filter_results([_FakeResult(1), _FakeResult(2)])
        assert len(out) == 2          # 库不存在=降级放行，不许炸检索


def _mk_atom_db(path):
    """最小 memory_atoms + documents 库。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE memory_atoms (id INTEGER PRIMARY KEY, parent_memory_id INTEGER, status TEXT, metadata TEXT)")
    # 列结构贴真库（documents 有 updated_at，实现会顺带刷新它）
    con.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, metadata TEXT, updated_at DATETIME)")
    return con


class TestSyncParentEligibility:

    def test_marks_parent_when_all_children_dead(self, tmp_path):
        db = str(tmp_path / "main.db")
        con = _mk_atom_db(db)
        con.execute("INSERT INTO memory_atoms VALUES (1, 10, 'archived', '{}')")
        con.execute("INSERT INTO memory_atoms VALUES (2, 10, 'archived', '{}')")
        con.execute("INSERT INTO documents (id, metadata) VALUES (10, '{}')")
        con.commit()
        changed = sync_parent_eligibility(con, atom_id=1)
        con.commit()
        meta = json.loads(con.execute("SELECT metadata FROM documents WHERE id=10").fetchone()[0])
        assert changed is True
        assert meta["retrieval_eligible"] is False   # 全死→滑出检索池

    def test_keeps_parent_when_sibling_alive(self, tmp_path):
        db = str(tmp_path / "main.db")
        con = _mk_atom_db(db)
        con.execute("INSERT INTO memory_atoms VALUES (1, 10, 'archived', '{}')")
        con.execute("INSERT INTO memory_atoms VALUES (2, 10, 'active', '{}')")
        con.execute("INSERT INTO documents (id, metadata) VALUES (10, '{}')")
        con.commit()
        changed = sync_parent_eligibility(con, atom_id=1)
        con.commit()
        row = con.execute("SELECT metadata FROM documents WHERE id=10").fetchone()
        assert changed is False
        assert "retrieval_eligible" not in json.loads(row[0])   # 兄弟还活着→不动

    def test_no_parent_noop(self, tmp_path):
        db = str(tmp_path / "main.db")
        con = _mk_atom_db(db)
        con.execute("INSERT INTO memory_atoms VALUES (1, NULL, 'archived', '{}')")
        con.execute("INSERT INTO documents (id, metadata) VALUES (10, '{}')")
        con.commit()
        assert sync_parent_eligibility(con, atom_id=1) is False

    def test_parent_doc_missing_noop(self, tmp_path):
        db = str(tmp_path / "main.db")
        con = _mk_atom_db(db)
        con.execute("INSERT INTO memory_atoms VALUES (1, 99, 'archived', '{}')")
        con.commit()
        assert sync_parent_eligibility(con, atom_id=1) is False   # document不存在=安静跳过
