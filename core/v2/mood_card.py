"""P0 心情卡片注入——把 EmotionCore 实时心情变成 ~40 token 系统层卡片。

设计: 桌宠大杂烩终版设计v1.0 §2.2 | 橘子已批 2026-09-04
原则: 写状态不写命令（报实况，不说"你必须开心"）；失败全静默绝不阻塞主链。
数据: get_snapshot() 四轴扁平快照 + get_last_appraisal(max_age=600) occ/brief。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("livingmemory.mood_card")

# OCC 13 标签中文映射（与 appraisal_engine.py:59 prompt 枚举对齐）
OCC_ZH: dict[str, str] = {
    "joy": "开心",
    "pride": "骄傲",
    "gratification": "满足",
    "love": "心动",
    "hope": "期待",
    "fear": "担心",
    "anger": "生气",
    "sadness": "难过",
    "pity": "心疼",
    "jealousy": "吃醋",
    "relief": "松了口气",
    "guilt": "愧疚",
    "shame": "害羞",
}


def _pleasure_word(p: float) -> str:
    if p >= 0.70:
        return "雀跃"
    if p >= 0.55:
        return "舒心"
    if p >= 0.45:
        return "平静"
    return "蔫蔫的"


def _arousal_word(a: float) -> str:
    if a >= 0.65:
        return "精神"
    if a >= 0.35:
        return "平稳"
    return "困困的"


def build_mood_card(snapshot: dict | None, appraisal: dict | None) -> str | None:
    """纯函数：扁平快照 + 最近评估 → 心情卡片。输入不对返回 None。"""
    try:
        if not isinstance(snapshot, dict) or "mood_pleasure" not in snapshot:
            return None
        p = float(snapshot.get("mood_pleasure", 0.5))
        a = float(snapshot.get("mood_arousal", 0.5))
        t = float(snapshot.get("mood_trust", 0.82))
        parts = [
            "【春雪此刻心情】"
            f"{_pleasure_word(p)}{_arousal_word(a)}"
            f"(P{p:.2f}/A{a:.2f}/信{t:.2f})"
        ]
        ap = appraisal if isinstance(appraisal, dict) else None
        if ap:
            occ = str(ap.get("occ_label", "") or "").strip()
            occ_zh = OCC_ZH.get(occ, occ)
            if occ_zh:
                parts.append(f"最近情绪:{occ_zh}")
            brief = str(ap.get("brief", "") or "").strip()
            if brief:
                parts.append(f"刚发生:{brief[:60]}")
        return "·".join(parts)
    except BaseException:
        return None


def make_card(
    emotion_core: Any,
    appraisal_engine: Any,
    persona_id: str = "default",
) -> str | None:
    """接线层：从引擎取数并生成卡片。任何失败静默返回 None（不阻塞 LLM 请求）。"""
    try:
        if emotion_core is None:
            return None
        snap = emotion_core.get_snapshot(persona_id)
        ap = None
        if appraisal_engine is not None:
            try:
                ap = appraisal_engine.get_last_appraisal(persona_id, max_age=600)
            except BaseException:
                ap = None
        return build_mood_card(snap, ap)
    except BaseException:
        logger.debug("[MoodCard] 生成失败(静默)", exc_info=True)
        return None
