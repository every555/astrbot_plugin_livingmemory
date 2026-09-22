"""P1-④ A-MAC 影子评分器测试：四维评分 + verdict 校准。"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.v2.amac_scorer import AMacScorer, score4


class TestScore4:
    """四维：utility=基础分 / novelty=1-jaccard / confidence=信息密度 / recency=当日新鲜度。"""

    def test_dimensions(self):
        r = score4(base_score=0.8, jaccard=0.2, density=0.5, is_today=True)
        assert r["utility"] == 0.8
        assert r["novelty"] == pytest.approx(0.8, abs=1e-3)
        assert r["confidence"] == 0.5
        assert r["recency"] == 1.0
        assert 0 <= r["amac_final"] <= 1

    def test_duplicate_gets_lower_final(self):
        fresh = score4(0.8, 0.0, 0.5)
        dup = score4(0.8, 0.7, 0.5)
        assert fresh["amac_final"] > dup["amac_final"]

    def test_not_today_recency_discount(self):
        today = score4(0.8, 0.0, 0.5, is_today=True)
        old = score4(0.8, 0.0, 0.5, is_today=False)
        assert old["recency"] < today["recency"]
        assert old["amac_final"] < today["amac_final"]

    def test_weights_configurable(self):
        w = {"utility": 1.0, "novelty": 0.0, "confidence": 0.0, "recency": 0.0}
        r = score4(0.8, 0.9, 0.9, weights=w)
        assert r["amac_final"] == pytest.approx(0.8, abs=1e-6)


class TestCalibrate:
    """吃 gate.db 的 verdict 历史，出分数段确认率 + 阈值建议。"""

    SCHEMA = ("CREATE TABLE IF NOT EXISTS gate_candidates ("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT DEFAULT 'candidate', "
              "score REAL DEFAULT 0, verdict TEXT DEFAULT '')")

    def _mkdb(self, path, rows):
        conn = sqlite3.connect(path)
        conn.execute(self.SCHEMA)
        conn.executemany(
            "INSERT INTO gate_candidates (status, score, verdict) VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_confirm_rate_by_bin(self, tmp_path):
        db = str(tmp_path / "gate.db")
        rows = ([("confirmed", 0.9, "升级")] * 9 + [("declined", 0.9, "驳回")] * 1
                + [("confirmed", 0.6, "升级")] * 4 + [("declined", 0.6, "驳回")] * 6)
        self._mkdb(db, rows)
        rep = AMacScorer(db).calibrate()
        high = [b for b in rep["bins"] if b["bin"] == "0.8+"]
        low = [b for b in rep["bins"] if b["bin"] == "0.5-0.8"]
        assert high[0]["total"] == 10
        assert high[0]["confirm_rate"] == pytest.approx(0.9)
        assert low[0]["total"] == 10
        assert low[0]["confirm_rate"] == pytest.approx(0.4)

    def test_suggested_threshold(self, tmp_path):
        db = str(tmp_path / "gate.db")
        rows = ([("confirmed", 0.9, "升级")] * 9 + [("declined", 0.9, "驳回")] * 1
                + [("confirmed", 0.6, "升级")] * 3 + [("declined", 0.6, "驳回")] * 7)
        self._mkdb(db, rows)
        rep = AMacScorer(db).calibrate()
        assert rep["suggested_threshold"] == 0.8

    def test_missing_db_degrades(self, tmp_path):
        rep = AMacScorer(str(tmp_path / "nope.db")).calibrate()
        assert rep == {}


class TestShadowAttach:
    def test_attach_keeps_original_keys(self):
        meta = {"jaccard": 0.3, "penalty": 1.0}
        out = AMacScorer.attach_shadow(meta, base_score=0.75, jaccard=0.3, density=0.6)
        assert out["jaccard"] == 0.3 and out["penalty"] == 1.0
        assert out["amac"]["utility"] == 0.75
        assert "amac_final" in out["amac"]