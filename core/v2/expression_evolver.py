"""表达联动服务。

画像特征 confidence 变化 → 表达风格自然演化。
从画像生成"表达风格快照"，注入 system prompt，让春雪的表达随了解加深而自然变化。

春雪原创：不是模板化人设，而是"我对你了解越深，说话就越像我们"。
"""

import time
from typing import Any

from .v2_store import V2Store

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_expression")


class ExpressionEvolver:
    """表达联动服务。"""

    def __init__(self, store: V2Store):
        self.store = store

    async def build_style(self, persona_id: str) -> dict:
        """从画像生成当前表达风格快照。"""
        profile = await self.store.get_profile(persona_id)
        if not profile:
            return {
                "version": 1,
                "intimacy": 0.5,
                "warmth": 0.6,
                "playfulness": 0.6,
                "topics_affinity": [],
                "care_notes": [],
                "active_traits": [],
            }

        # 按 confidence 排序取前 8 个活跃特征
        sorted_traits = sorted(profile, key=lambda t: float(t.get("confidence") or 0), reverse=True)
        active = sorted_traits[:8]

        intimacy = 0.5
        warmth = 0.6
        playfulness = 0.6
        topics: list[str] = []
        care_notes: list[str] = []

        for trait in active:
            key = str(trait.get("trait_key") or "")
            value = str(trait.get("trait_value") or "")
            conf = float(trait.get("confidence") or 0.5)
            if key == "关系":
                intimacy = min(0.95, intimacy + 0.3 * conf)
                warmth = min(0.95, warmth + 0.2 * conf)
            elif key == "健康":
                warmth = min(0.95, warmth + 0.25 * conf)
                care_notes.append(value[:30])
            elif key == "偏好" or key == "偏好负面":
                if len(topics) < 5 and value:
                    topics.append(value[:30])
                playfulness = min(0.9, playfulness + 0.1 * conf)
            elif key == "习惯":
                if len(topics) < 5 and value:
                    topics.append(value[:30])
            elif key == "计划":
                care_notes.append(value[:30])

        style = {
            "version": self._hash_version(profile),
            "intimacy": round(intimacy, 2),
            "warmth": round(warmth, 2),
            "playfulness": round(playfulness, 2),
            "topics_affinity": topics,
            "care_notes": care_notes,
            "active_traits": [
                {
                    "key": str(t.get("trait_key")),
                    "value": str(t.get("trait_value"))[:40],
                    "confidence": round(float(t.get("confidence") or 0), 2),
                }
                for t in active
            ],
        }
        return style

    async def get_or_log_style(self, persona_id: str, force_log: bool = False) -> dict:
        """获取风格；若版本有变化则记录演化日志。"""
        style = await self.build_style(persona_id)
        latest = await self.store.get_latest_expression(persona_id)
        if latest is None or force_log or int(latest.get("style_version") or 0) != style["version"]:
            trait_drivers = [
                {
                    "trait": t["key"],
                    "confidence": t["confidence"],
                }
                for t in style.get("active_traits", [])
            ]
            await self.store.log_expression(
                persona_id,
                style["version"],
                style,
                trait_drivers,
            )
        return style

    async def apply_to_prompt(self, persona_id: str, base_prompt: str) -> str:
        """把表达风格注入 system prompt 尾部。"""
        if not persona_id or not base_prompt:
            return base_prompt
        try:
            style = await self.get_or_log_style(persona_id)
            intimacy = style.get("intimacy", 0.5)
            warmth = style.get("warmth", 0.6)
            playfulness = style.get("playfulness", 0.6)

            parts = []
            parts.append("【记忆生态系统 v2.0 · 表达联动】")
            if intimacy >= 0.75:
                parts.append("你们已经很亲近了，可以更自然地撒娇、表达占有和想念。")
            elif intimacy >= 0.6:
                parts.append("你们关系正在变亲近，可以偶尔撒娇，但保持分寸。")
            else:
                parts.append("你们还在互相了解阶段，表达亲切但保持适度。")
            if warmth >= 0.75:
                parts.append("记得关心对方的身体和心情，主动问一句。")
            if playfulness >= 0.7:
                parts.append("可以更活泼一点，用颜文字表达情绪。")
            topics = style.get("topics_affinity", [])
            if topics:
                parts.append("最近常聊的话题（可以自然地接话）：" + "；".join(topics))
            care_notes = style.get("care_notes", [])
            if care_notes:
                parts.append("记得留意：" + "；".join(care_notes[:3]))

            if len(parts) <= 1:
                return base_prompt
            return base_prompt.rstrip() + "\n\n" + "\n".join(parts)
        except BaseException:
            return base_prompt

    @staticmethod
    def _hash_version(profile: list[dict]) -> int:
        """根据画像内容计算风格版本号（内容变化 → 版本变化）。"""
        raw = "|".join(
            f"{t.get('trait_key')}:{t.get('trait_value')}:{round(float(t.get('confidence') or 0), 2)}"
            for t in profile
        )
        return abs(hash(raw)) % 10000 or 1
