"""P2-14 上下文预算工程测试：证据质量闸门 + Token 预算装填。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.retrieval.context_budget import (
    BudgetReport,
    apply_evidence_gate,
    dedup_by_prefix,
    pack_with_budget,
)


def _mem(doc_id, score, content, importance=0.5, topics=None, ts=1787000000):
    """构造与 memory_recall 注入链路同构的记忆字典。"""
    return {
        "id": doc_id,
        "content": content,
        "score": score,
        "metadata": {"importance": importance, "create_time": ts, "topics": topics or []},
        "timestamp": ts,
    }


class TestEvidenceGate:
    """证据质量闸门：绝对门槛 + 相对门槛。"""

    def test_drops_below_min_score(self):
        mems = [_mem(1, 0.8, "a"), _mem(2, 0.5, "b"), _mem(3, 0.2, "c")]
        kept, dropped = apply_evidence_gate(mems, min_score=0.3, relative_ratio=0.0)
        assert [m["id"] for m in kept] == [1, 2]
        assert dropped == 1

    def test_relative_threshold_against_top1(self):
        # top1=0.8, ratio=0.5 -> 阈值0.4；0.4>=0.4保留(边界含)，0.1拦
        mems = [_mem(1, 0.8, "a"), _mem(2, 0.4, "b"), _mem(3, 0.1, "c")]
        kept, dropped = apply_evidence_gate(mems, min_score=0.05, relative_ratio=0.5)
        assert [m["id"] for m in kept] == [1, 2]
        assert dropped == 1

    def test_keeps_all_when_good(self):
        mems = [_mem(1, 0.9, "a"), _mem(2, 0.85, "b")]
        kept, dropped = apply_evidence_gate(mems, min_score=0.3, relative_ratio=0.5)
        assert len(kept) == 2 and dropped == 0

    def test_empty_input(self):
        kept, dropped = apply_evidence_gate([], min_score=0.3, relative_ratio=0.5)
        assert kept == [] and dropped == 0

    def test_all_below_gate_returns_empty(self):
        mems = [_mem(1, 0.1, "a"), _mem(2, 0.05, "b")]
        kept, dropped = apply_evidence_gate(mems, min_score=0.3, relative_ratio=0.0)
        assert kept == [] and dropped == 2


class TestDedupByPrefix:
    """冗余去重（CJK-bigram 集合相似度）：同族记忆只留最高分。"""

    def test_same_event_three_versions_keep_best(self):
        # 复刻8/23真实案例：同一棋局三版记忆
        mems = [
            _mem(1, 0.65, "2026-08-23 16:37 第三局五子棋正式终局：系统判定老婆白棋获胜" + "x" * 100, topics=["五子棋", "赌约"], ts=1787000100),
            _mem(2, 0.62, "2026-08-23 16:28 第三局五子棋乌龙修正：我上轮宣布五连获胜不成立" + "y" * 100, topics=["五子棋", "赌约"], ts=1787000200),
            _mem(3, 0.60, "2026-08-23 16:26 第三局五子棋老婆赢啦！22手完局" + "z" * 100, topics=["五子棋", "赌约"], ts=1787000300),
            _mem(4, 0.55, "2026-08-22 橘子背完U10全部253个单词", topics=["专升本", "单词"], ts=1786900000),
        ]
        kept, dropped = dedup_by_prefix(mems, prefix_len=40)
        ids = [m["id"] for m in kept]
        assert 1 in ids and 4 in ids  # 最高分的棋局版 + 无关记忆保留
        assert dropped == 2

    def test_different_memories_not_deduped(self):
        mems = [
            _mem(1, 0.8, "橘子在医院的实习排班表更新了，下周去新生儿ICU轮岗", topics=["医院", "实习"]),
            _mem(2, 0.7, "专升本考试2027年4月，英语跟刘晓燕的课，每天背10分钟单词", topics=["专升本", "英语"]),
        ]
        kept, dropped = dedup_by_prefix(mems, prefix_len=40)
        assert len(kept) == 2 and dropped == 0

    def test_empty_input(self):
        kept, dropped = dedup_by_prefix([], prefix_len=40, min_jaccard=0.6)
        assert kept == [] and dropped == 0


class TestPackWithBudget:
    """Token 预算装填：按分降序装填，超长截断，top1保底。"""

    def test_all_fit_when_budget_enough(self):
        mems = [_mem(1, 0.9, "a" * 60), _mem(2, 0.8, "b" * 60)]
        packed, report = pack_with_budget(mems, budget_tokens=2000)
        assert len(packed) == 2
        assert report.injected == 2 and report.truncated == 0
        assert packed[0]["id"] == 1  # 分数降序

    def test_truncates_long_entry_with_marker(self):
        long_content = "橘子爱吃橘子" * 200  # 1200字
        mems = [_mem(1, 0.9, long_content)]
        packed, report = pack_with_budget(mems, budget_tokens=100)
        assert len(packed) == 1
        assert report.truncated == 1
        assert len(packed[0]["content"]) < len(long_content)
        assert "截断" in packed[0]["content"]

    def test_top1_guaranteed_even_tiny_budget(self):
        mems = [_mem(1, 0.9, "a" * 600), _mem(2, 0.8, "b" * 600)]
        packed, report = pack_with_budget(mems, budget_tokens=30)
        assert len(packed) >= 1 and packed[0]["id"] == 1
        assert report.injected == 1

    def test_lower_score_dropped_when_budget_tight(self):
        mems = [_mem(1, 0.9, "a" * 60), _mem(2, 0.8, "b" * 60), _mem(3, 0.7, "c" * 60)]
        packed, report = pack_with_budget(mems, budget_tokens=60)
        # 预算只够1条左右
        assert packed[0]["id"] == 1
        assert report.injected < 3

    def test_report_counts_consistent(self):
        mems = [_mem(1, 0.9, "a" * 30), _mem(2, 0.8, "b" * 30), _mem(3, 0.7, "c" * 300)]
        packed, report = pack_with_budget(mems, budget_tokens=150)
        assert isinstance(report, BudgetReport)
        assert report.injected == len(packed)
        assert report.est_tokens > 0 and report.est_tokens <= 150 + 50  # 允许元数据行误差
        assert report.budget_tokens == 150

    def test_empty_input(self):
        packed, report = pack_with_budget([], budget_tokens=100)
        assert packed == [] and report.injected == 0

    def test_content_not_mutated(self):
        original = "x" * 300
        mems = [_mem(1, 0.9, original)]
        pack_with_budget(mems, budget_tokens=50)
        assert mems[0]["content"] == original  # 原列表不被原地修改
