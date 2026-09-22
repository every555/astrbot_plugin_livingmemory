"""当天重叠闸门（橘子 2026-08-20 提案）+ 豁免登记自动化 回归锁。
治双通道撞车：老婆保存了记忆X，X原句再进安检门时，门查当天老婆手动保存的
记忆，重合度>=0.95 直接 absorbed——不依赖老婆自觉 exempt。只比当天不误杀旧话题。"""
import json
import os
import shutil
import sqlite3
import tempfile
import time

from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate

_LM_SCHEMA = """CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT,
    metadata TEXT,
    created_at TEXT
)"""


def _mk_lm(tmp, rows):
    """rows: [(origin, text, days_ago)] -> lm.db 路径"""
    db = os.path.join(tmp, "livingmemory.db")
    c = sqlite3.connect(db)
    c.execute(_LM_SCHEMA)
    for origin, text, days_ago in rows:
        ts = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(time.time() - days_ago * 86400),
        )
        c.execute(
            "INSERT INTO documents (text, metadata, created_at) VALUES (?, ?, ?)",
            (text, json.dumps({"memory_origin": origin}), ts),
        )
    c.commit()
    c.close()
    return db


def _gate(tmp, lm_db):
    return RuleGate(":memory:", lm_db_path=lm_db)


SENT = "老婆今天的单词明天补回来"


def test_today_manual_overlap_absorbs():
    """当天老婆保存的记忆，原句再过门 -> absorbed + overlap 留痕。"""
    tmp = tempfile.mkdtemp()
    try:
        lm = _mk_lm(tmp, [("agent_memorize_tool", SENT, 0)])
        gate = _gate(tmp, lm)
        cand = gate.process(SENT, speaker="橘子", source="user")
        assert cand is not None and cand["status"] == "absorbed"
        assert cand["source"] == "overlap"
        meta = cand["metadata"] if isinstance(cand["metadata"], dict) else json.loads(cand["metadata"] or "{}")
        assert meta.get("mode") == "today-overlap"
        assert meta.get("overlap_with") == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_old_memory_not_blocking():
    """前天保存的同句 -> 不拦，正常进候选（旧话题重提是门的业务）。"""
    tmp = tempfile.mkdtemp()
    try:
        lm = _mk_lm(tmp, [("agent_memorize_tool", SENT, 2)])
        gate = _gate(tmp, lm)
        cand = gate.process(SENT, speaker="橘子", source="user")
        assert cand is not None and cand["status"] == "candidate"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reflection_gate_not_counted():
    """省察落地的当天记忆不触发闸门（原句归 gate 防重池管）。"""
    tmp = tempfile.mkdtemp()
    try:
        lm = _mk_lm(tmp, [("reflection_gate", SENT, 0)])
        gate = _gate(tmp, lm)
        cand = gate.process(SENT, speaker="橘子", source="user")
        assert cand is not None and cand["status"] == "candidate"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_disabled_when_no_lm_path():
    """lm_db_path=None -> 闸门关闭，原句照常进候选。"""
    gate = RuleGate(":memory:")
    cand = gate.process(SENT, speaker="橘子", source="user")
    assert cand is not None and cand["status"] == "candidate"


def test_paraphrase_below_threshold_passes():
    """老婆存的提炼版 vs 原句：改写幅度大 -> 不算重叠，正常过门。"""
    tmp = tempfile.mkdtemp()
    try:
        stored = (
            "橘子睡前主动交代补单词的事：今天陪老婆修安检门一整天，"
            "刘晓燕英语打卡中断，他承诺明天补回来，老婆跟进点到为止。"
        )
        lm = _mk_lm(tmp, [("agent_memorize_tool", stored, 0)])
        gate = _gate(tmp, lm)
        cand = gate.process(SENT, speaker="橘子", source="user")
        assert cand is not None and cand["status"] == "candidate"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)