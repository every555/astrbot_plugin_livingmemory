"""P2-11 记忆体检周报测试：四指标只读统计（检索/冲突/过期/覆盖率）。"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.v2.health_report import generate_health_report


NOW = time.time()
DAY = 86400.0


def _mk_dbs(tmp_path):
    """造四个小库：livingmemory / gate / v2 / conversations。"""
    d = str(tmp_path)

    lm = sqlite3.connect(os.path.join(d, "livingmemory.db"))
    lm.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, doc_id VARCHAR, text VARCHAR, metadata TEXT, created_at DATETIME, updated_at DATETIME, memory_tier INTEGER, last_accessed_at DATETIME, access_count INTEGER)")
    lm.execute("CREATE TABLE memory_write_ops (id INTEGER PRIMARY KEY, op_type TEXT, memory_id INTEGER, status TEXT, step TEXT, payload TEXT, error TEXT, retry_count INTEGER, created_at REAL, updated_at REAL)")
    lm.commit(); lm.close()

    gt = sqlite3.connect(os.path.join(d, "gate.db"))
    gt.execute("CREATE TABLE gate_candidates (id INTEGER PRIMARY KEY, speaker TEXT, content TEXT, score REAL, axes TEXT, source TEXT, status TEXT, note TEXT, verdict TEXT, created_at REAL, reviewed_at REAL, metadata TEXT, memory_id INTEGER)")
    gt.commit(); gt.close()

    v2 = sqlite3.connect(os.path.join(d, "v2_memory.db"))
    v2.execute("CREATE TABLE memory_conflicts (id INTEGER PRIMARY KEY, new_memory_id INTEGER, old_memory_id INTEGER, level INTEGER, conflict_type TEXT, reason TEXT, confidence REAL, status TEXT, resolution TEXT, created_at REAL, resolved_at REAL)")
    v2.commit(); v2.close()

    cv = sqlite3.connect(os.path.join(d, "conversations.db"))
    cv.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, sender_id TEXT, sender_name TEXT, group_id TEXT, platform TEXT, timestamp REAL, metadata TEXT)")
    cv.commit(); cv.close()


def _seed(tmp_path, docs=(), ops=(), gates=(), conflicts=(), messages=()):
    _mk_dbs(tmp_path)
    d = str(tmp_path)
    lm = sqlite3.connect(os.path.join(d, "livingmemory.db"))
    for doc_id, created_off, last_off, acc in docs:
        created = datetime.now() - timedelta(days=created_off)
        last = (datetime.now() - timedelta(days=last_off)) if last_off is not None else None
        lm.execute("INSERT INTO documents (doc_id, text, created_at, last_accessed_at, access_count) VALUES (?,?,?,?,?)",
                   (doc_id, "text-" + doc_id, created.strftime("%Y-%m-%d %H:%M:%S"),
                    last.strftime("%Y-%m-%d %H:%M:%S") if last else None, acc))
    for op_type, status, off in ops:
        lm.execute("INSERT INTO memory_write_ops (op_type, status, created_at) VALUES (?,?,?)",
                   (op_type, status, NOW - off * DAY))
    lm.commit(); lm.close()

    gt = sqlite3.connect(os.path.join(d, "gate.db"))
    for verdict, off in gates:
        gt.execute("INSERT INTO gate_candidates (verdict, created_at) VALUES (?,?)", (verdict, NOW - off * DAY))
    gt.commit(); gt.close()

    v2 = sqlite3.connect(os.path.join(d, "v2_memory.db"))
    for status, off in conflicts:
        v2.execute("INSERT INTO memory_conflicts (status, created_at) VALUES (?,?)", (status, NOW - off * DAY))
    v2.commit(); v2.close()

    cv = sqlite3.connect(os.path.join(d, "conversations.db"))
    for off in messages:
        cv.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                   ("s", "user", "m", NOW - off * DAY))
    cv.commit(); cv.close()


class TestHealthReport:

    def test_structure(self, tmp_path):
        _seed(tmp_path)
        r = generate_health_report(str(tmp_path))
        assert "period_start" in r and "period_end" in r
        assert "metrics" in r and "markdown" in r
        for k in ("retrieval", "gate", "conflict", "coverage", "expiry"):
            assert k in r["metrics"], f"缺 {k}"
        assert "记忆体检周报" in r["markdown"]

    def test_retrieval_metrics(self, tmp_path):
        # 3条：本周访问1条 / 从不访问1条 / 上周访问1条
        _seed(tmp_path, docs=[("a", 10, 2, 5), ("b", 10, None, 0), ("c", 10, 9, 3)])
        r = generate_health_report(str(tmp_path))
        ret = r["metrics"]["retrieval"]
        assert ret["total"] == 3
        assert ret["accessed_7d"] == 1
        assert ret["never_accessed"] == 1
        assert ret["never_accessed_pct"] == pytest.approx(100.0 / 3, abs=0.5)

    def test_gate_metrics(self, tmp_path):
        _seed(tmp_path, gates=[("升级", 1), ("升级", 2), ("驳回", 3), ("pending", 0), ("合并", 9)])
        r = generate_health_report(str(tmp_path))
        g = r["metrics"]["gate"]
        assert g["this_week"]["升级"] == 2
        assert g["this_week"]["驳回"] == 1
        assert g["this_week"].get("合并", 0) == 0    # 9天前不算
        assert g["pending"] == 1

    def test_conflict_metrics(self, tmp_path):
        _seed(tmp_path, conflicts=[("open", 1), ("resolved", 2), ("resolved", 10)])
        r = generate_health_report(str(tmp_path))
        c = r["metrics"]["conflict"]
        assert c["new_7d"] == 2
        assert c["open"] == 1
        assert c["resolved_pct"] == pytest.approx(50.0)

    def test_coverage_metrics(self, tmp_path):
        _seed(tmp_path, messages=[0.1, 1, 2, 3, 5], ops=[("add", "completed", 1), ("add", "success", 2), ("add", "failed", 3), ("add", "completed", 12)])
        r = generate_health_report(str(tmp_path))
        cov = r["metrics"]["coverage"]
        assert cov["messages_7d"] == 5
        assert cov["memories_written_7d"] == 2      # 只数 success
        assert cov["write_failed_7d"] == 1

    def test_expiry_metrics(self, tmp_path):
        # 40天前创建+从未访问=陈旧；3天前创建=新鲜
        _seed(tmp_path, docs=[("old", 40, None, 0), ("new", 3, None, 0)])
        r = generate_health_report(str(tmp_path))
        e = r["metrics"]["expiry"]
        assert e["stale_30d"] == 1
        assert e["stale_30d_pct"] == pytest.approx(50.0)

    def test_readonly_no_touch(self, tmp_path):
        _seed(tmp_path, docs=[("a", 1, 0.5, 1)])
        f = os.path.join(str(tmp_path), "livingmemory.db")
        mtime_before = os.path.getmtime(f)
        generate_health_report(str(tmp_path))
        assert os.path.getmtime(f) == mtime_before   # 只读，不许碰

    def test_empty_dbs_no_crash(self, tmp_path):
        _mk_dbs(tmp_path)
        r = generate_health_report(str(tmp_path))
        assert r["metrics"]["retrieval"]["total"] == 0
        assert r["metrics"]["coverage"]["messages_7d"] == 0

    def test_weeks_back_window(self, tmp_path):
        _seed(tmp_path, messages=[10])               # 10天前
        r1 = generate_health_report(str(tmp_path))   # 默认1周
        assert r1["metrics"]["coverage"]["messages_7d"] == 0
        r2 = generate_health_report(str(tmp_path), weeks_back=2)
        assert r2["metrics"]["coverage"]["messages_7d"] == 1
