"""
情感路由器 — v5.4 老婆创意①
根据用户消息的情感倾向调整检索结果的排序权重。

原理：
- 检测用户消息中的情感信号词
- 对与情感相关的记忆条目给予加成/降权
- 例如：用户"想不起来了吗" → 提升关系类、近期的记忆权重
      用户"好烦啊" → 提升安慰类、偏好类记忆权重
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EmotionSignal:
    """单个情感信号"""
    emotion: str
    keywords: list[str]
    boost_types: list[str]   # 需要加成的 atom_type 列表
    boost_factor: float      # 加成系数 (1.0 = 不变)
    recency_bias: float      # 近期记忆额外加成 (0.0-1.0)


# 情感关键词映射表（中文）
# 每个情感对应一组关键词和对应的加成策略
_EMOTION_PATTERNS: list[EmotionSignal] = [
    EmotionSignal(
        emotion="happy",
        keywords=["开心", "高兴", "快乐", "哈哈", "嘿嘿", "太好了", "棒", "赞", "厉害", "好耶"],
        boost_types=["episodic", "preference"],
        boost_factor=1.15,
        recency_bias=0.2,
    ),
    EmotionSignal(
        emotion="excited",
        keywords=["超级", "激動", "激动", "期待", "终于", "来了来了", "冲冲冲", "冲鸭"],
        boost_types=["episodic", "planned"],
        boost_factor=1.20,
        recency_bias=0.3,
    ),
    EmotionSignal(
        emotion="tired",
        keywords=["好累", "困了", "累了", "疲惫", "不想动", "躺平", "休息", "睡觉", "困"],
        boost_types=["preference", "factual"],
        boost_factor=1.10,
        recency_bias=0.15,
    ),
    EmotionSignal(
        emotion="sad",
        keywords=["难过", "伤心", "想哭", "郁闷", "好烦", "不开心", "生气", "气死", "郁闷", "哭"],
        boost_types=["preference", "relational"],
        boost_factor=1.18,
        recency_bias=0.25,
    ),
    EmotionSignal(
        emotion="curious",
        keywords=["为什么", "怎么", "什么", "哪里", "哪个", "吗？", "呢？", "是不是", "能不能"],
        boost_types=["factual", "relational"],
        boost_factor=1.12,
        recency_bias=0.1,
    ),
    EmotionSignal(
        emotion="nostalgic",
        keywords=["还记得", "上次", "以前", "之前", "那天", "那时候", "回忆", "想起来"],
        boost_types=["episodic", "relational"],
        boost_factor=1.25,
        recency_bias=0.4,  # 怀旧时更倾向近期记忆
    ),
    EmotionSignal(
        emotion="angry",
        keywords=["气死", "烦死", "不玩了", "讨厌", "滚", "闭嘴", "别说了"],
        boost_types=["preference", "factual"],
        boost_factor=0.85,  # 生气时降低一般记忆权重，避免火上浇油
        recency_bias=0.5,
    ),
    EmotionSignal(
        emotion="loving",
        keywords=["老婆", "宝贝", "喜欢你", "爱你", "想你", "亲爱的", "抱抱", "亲亲"],
        boost_types=["relational", "episodic"],
        boost_factor=1.30,
        recency_bias=0.35,
    ),
]


# P3-2 双源融合映射表：LLM 评估标签 → router 检索标签
_MOOD_MAP: dict[str, str] = {
    # user_mood.label（prompt 自由描述，常见值）
    "happy": "happy", "tired": "tired", "frustrated": "angry", "angry": "angry",
    "upset": "sad", "sad": "sad", "affectionate": "loving", "excited": "excited",
    "curious": "curious", "anxious": "sad", "nostalgic": "nostalgic",
    "bored": "tired", "neutral": "neutral",
}
_OCC_MAP: dict[str, str] = {
    # OCC 标签（SYSTEM_PROMPT 定义的 13 个 + fallback 同款）
    "joy": "happy", "pride": "happy", "gratification": "happy", "relief": "happy",
    "hope": "happy", "love": "loving", "jealousy": "loving",  # 吃醋召回"我们"的记忆(春雪特调)
    "anger": "angry", "fear": "sad", "sadness": "sad", "pity": "sad",
    "guilt": "sad", "shame": "sad",
}


class EmotionRouter:
    """情感路由器：根据用户消息情感调整检索结果权重"""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.enabled = self.config.get("emotion_router_enabled", True)
        # 预编译关键词正则
        self._compiled_patterns: list[tuple[EmotionSignal, re.Pattern]] = []
        for signal in _EMOTION_PATTERNS:
            pattern = re.compile(
                "|".join(re.escape(kw) for kw in signal.keywords),
                re.IGNORECASE,
            )
            self._compiled_patterns.append((signal, pattern))

    def detect_emotion(self, text: str) -> str:
        """检测文本的情感倾向

        Returns:
            情感标签: happy/excited/tired/sad/curious/nostalgic/angry/loving/neutral
        """
        if not self.enabled or not text:
            return "neutral"

        # 按优先级匹配（后面的优先级更高，因为它们覆盖前面的）
        detected = "neutral"
        best_score = 0

        for signal, pattern in self._compiled_patterns:
            matches = pattern.findall(text)
            if matches:
                score = len(matches)
                if score > best_score:
                    best_score = score
                    detected = signal.emotion

        return detected

    # P3-3/P3-4: 元数据 occ 情感极性集
    _POSITIVE_OCC = frozenset({"joy", "pride", "gratification", "relief", "hope", "love"})
    _NEGATIVE_OCC = frozenset({"sadness", "fear", "anger", "pity", "guilt", "shame", "jealousy"})

    @classmethod
    def _occ_of(cls, result: Any) -> str | None:
        try:
            md = getattr(result, "metadata", None)
            if not isinstance(md, dict):
                return None
            e = md.get("emotion")
            if isinstance(e, dict):
                return str(e.get("occ") or "").lower() or None
        except BaseException:
            pass
        return None

    def apply_mood_weight(self, results: list[Any], mood_p: float) -> list[Any]:
        """P3-3 Emotional RAG: 按当前 mood 给召回结果调权。

        低落(p<0.4): 同频负向记忆共鸣加分但封顶1.1x(防沉溺霸榜);
        高涨(p>0.7): 正向记忆微加1.05x(锦上添花); 中间地带不干预。
        """
        if not self.enabled or not results:
            return results
        try:
            if mood_p < 0.4:
                for r in results:
                    if self._occ_of(r) in self._NEGATIVE_OCC:
                        s = getattr(r, "final_score", 0.0)
                        if hasattr(r, "final_score"):
                            r.final_score = round(min(s * 1.1, s + s * 0.1), 4)  # 封顶=1.1x
            elif mood_p > 0.7:
                for r in results:
                    if self._occ_of(r) in self._POSITIVE_OCC:
                        if hasattr(r, "final_score"):
                            r.final_score = round(r.final_score * 1.05, 4)
        except BaseException:
            pass
        return results

    def inject_positive_diversity(
        self, results: list[Any], mood_p: float, window: int = 3, quota: int = 1
    ) -> list[Any]:
        """P3-4 正向多样性: 低 mood(p<0.4)时，top 窗口内必须有正向记忆"递糖"。

        库存里没有正向记忆则不强求（原样返回）。
        """
        if not self.enabled or not results or mood_p >= 0.4:
            return results
        try:
            top = results[:window]
            has_pos = any(self._occ_of(r) in self._POSITIVE_OCC for r in top)
            if has_pos:
                return results
            # 从窗口外挑最高分的正向记忆插到第2位
            rest = results[window:]
            best_pos = None
            for r in rest:
                if self._occ_of(r) in self._POSITIVE_OCC:
                    if best_pos is None or getattr(r, "final_score", 0) > getattr(best_pos, "final_score", 0):
                        best_pos = r
            if best_pos is None:
                return results
            rest = [r for r in rest if r is not best_pos]
            return [top[0], best_pos, *top[1:], *rest][:max(len(results), 0)]
        except BaseException:
            return results

    def detect_emotion_dual(
        self, text: str, appraisal_rec: dict | None = None
    ) -> tuple[str, str]:
        """P3-2 双源检测：user_mood.label → occ 映射 → 词法兜底。

        Returns:
            (router情感标签, 来源): 来源 ∈ user_mood/occ/lexical
        """
        if not self.enabled:
            return "neutral", "none"
        ap = (appraisal_rec or {}).get("appraisal") or {}
        if ap.get("source") == "llm":
            mood = (ap.get("user_mood") or {}).get("label") or ""
            mapped = _MOOD_MAP.get(str(mood).strip().lower())
            if mapped:
                return mapped, "user_mood"
            occ_mapped = _OCC_MAP.get(str(ap.get("occ_label", "")).strip().lower())
            if occ_mapped:
                return occ_mapped, "occ"
        return self.detect_emotion(text), "lexical"

    def apply_routing(
        self,
        results: list[Any],
        emotion: str,
        reference_time: float | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        """对检索结果应用情感路由

        Args:
            results: 检索结果列表（需要有 final_score 和 atom_type 属性）
            emotion: 检测到的情感
            reference_time: 参考时间戳

        Returns:
            (调整后的结果列表, 调整详情列表)
        """
        if not self.enabled or emotion == "neutral" or not results:
            return results, []

        # 找到对应的情感信号
        signal = None
        for s, _ in self._compiled_patterns:
            if s.emotion == emotion:
                signal = s
                break

        if not signal:
            return results, []

        import time as _time
        now = reference_time or _time.time()

        boost_details: list[dict[str, Any]] = []
        adjusted = []

        for result in results:
            original_score = getattr(result, "final_score", 0.0)
            atom_type = getattr(result, "atom_type", "unknown")
            importance = getattr(result, "importance", 0.5)

            # 基础加成：如果 atom_type 在 boost_types 中
            boost = 1.0
            if atom_type in signal.boost_types:
                boost *= signal.boost_factor

            # 近期加成：根据 importance 和 recency_bias
            if signal.recency_bias > 0:
                # importance 越高（通常越近期被访问），加成越大
                recency_factor = 1.0 + signal.recency_bias * importance
                boost *= recency_factor

            new_score = original_score * boost

            # 更新 final_score
            if hasattr(result, "final_score"):
                result.final_score = round(new_score, 4)

            adjusted.append(result)

            if boost != 1.0:
                boost_details.append({
                    "doc_id": getattr(result, "doc_id", getattr(result, "atom_id", 0)),
                    "atom_type": atom_type,
                    "original_score": round(original_score, 4),
                    "boosted_score": round(new_score, 4),
                    "boost_factor": round(boost, 4),
                    "emotion": emotion,
                })

        # 重新排序
        adjusted.sort(
            key=lambda r: getattr(r, "final_score", 0.0),
            reverse=True,
        )

        logger.debug(
            f"[EmotionRouter] emotion={emotion}, "
            f"boosted={len(boost_details)}/{len(results)} results"
        )

        return adjusted, boost_details


__all__ = ["EmotionRouter", "EmotionSignal"]
