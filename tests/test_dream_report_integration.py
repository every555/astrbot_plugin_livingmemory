# -*- coding: utf-8 -*-
"""P0-3 例会整合测试：_build_summary 渲染梦境议题 + _load_dream_report 降级安全"""
from astrbot_plugin_livingmemory.core.v2.family_meeting_manager import FamilyMeetingManager


def _summary_with(drep):
    s = {
        "report_date": "2026-08-22", "family_active": 10, "feedback_total": 100,
        "feedback_events": 5, "profile_updated": 1, "prophecy_created": 0,
        "prophecy_verified": 0, "prophecy_failed": 0, "prophecy_active": 0,
        "conflicts_created": 0, "conflicts_pending": 0, "causality_linked": 0,
        "expression_logged": 0, "archive_archived": 0, "archive_pending": 0,
        "cooperation_edges": 0, "dream_report": drep,
    }
    m = FamilyMeetingManager.__new__(FamilyMeetingManager)
    return m._build_summary(s)


def test_summary_real_dream():
    text = _summary_with({"mode": "real", "consolidated": 3, "pruned": 5, "conflicts_found": 2})
    assert "做梦的孩子" in text and "归并 3 条" in text and "修剪 5 条" in text
    assert "第二议题" in text, "有冲突时应升第二议题"


def test_summary_dry_run_dream():
    text = _summary_with({"mode": "dry_run", "totals": {"merge_candidates": 4, "prune_candidates": 50, "conflict_candidates": 1}})
    assert "dry_run" in text and "待归并 4 对" in text
    assert "第二议题" in text


def test_summary_no_dream_report():
    text = _summary_with(None)
    assert "做梦的孩子" not in text, "无报告时不应出现梦境议题"


def test_load_dream_report_degrades():
    m = FamilyMeetingManager.__new__(FamilyMeetingManager)
    rep = m._load_dream_report()
    # 真身环境可能有报告也可能没有,只要不炸即可
    assert rep is None or isinstance(rep, dict)