"""记忆画像服务。

从写入的记忆中提取"核心特征"（core_traits），每个特征带：
- confidence（置信度，随证据累积上升）
- evidence_ids（证据记忆 ID 列表）
- evolution_log（演化历史）

春雪原创：画像不是静态标签，而是随证据演化的活档案。
"""

import re
import time
from typing import Any

from .conflict_detector import STOP_TERMS
from .v2_store import V2Store

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_profile")

# 事件总线（v2.1 家庭协作反馈，独立测试环境降级为无操作）
try:
    from ..events.event_bus import MemoryEvent, MemoryEventType, get_event_bus

    _HAS_BUS = True
except Exception:  # pragma: no cover
    _HAS_BUS = False

    MemoryEventType = None
    MemoryEvent = None

    def get_event_bus():  # type: ignore
        return None


# 特征类别关键词
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("偏好", ["喜欢", "爱吃", "爱喝", "爱看", "爱玩", "爱听", "爱好", "偏爱", "最爱"]),
    ("偏好负面", ["不喜欢", "讨厌", "反感", "厌恶", "受不了", "忌口", "过敏"]),
    ("习惯", ["习惯", "经常", "每次", "总是", "平时", "日常", "规律", "每天早上", "每晚", "每周", "每天", "每月"]),
    ("身份", ["我是", "我叫", "我住在", "我来自", "我是做", "我从事", "职业", "工作"]),
    ("计划", ["备考", "准备", "打算", "计划", "目标", "想考", "要考", "报名"]),
    ("健康", ["胃", "生病", "医院", "吃药", "不舒服", "体检", "养生", "熬夜"]),
    ("关系", ["女朋友", "男朋友", "老婆", "老公", "家人", "父母", "朋友", "同学"]),
    ("重要日期", ["生日", "纪念日", "节日", "周年"]),
    ("学习", ["学习", "背单词", "看书", "复习", "考试", "课程", "上课"]),
    ("游戏", ["打游戏", "游戏", "排位", "上分", "副本", "抽卡"]),
]


def classify_category(text: str) -> str:
    """把记忆内容分类到特征类别。"""
    for category, keywords in CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return category
    return "其他"


# 单字动词（提取名词时跳过）
VERBS = {"去", "打", "吃", "看", "玩", "在", "有", "要", "想", "做",
         "是", "都", "会", "来", "说", "上", "下", "给", "听", "写",
         "背", "学", "读", "加", "买", "喝", "睡", "起", "走"}

# 时间词（作息/日程类画像优先）
TIME_WORDS = ["早上", "上午", "中午", "下午", "晚上", "今晚", "今早",
              "明早", "凌晨", "早晨", "昨天", "今天", "明天", "周末"]


def _extract_first_noun(block: str) -> str:
    """从中文块开头提取第一个有意义的名词短语（跳过停用词和动词前缀/后缀）。"""
    # 先按连接词切分，优先取第一段
    for seg in re.split(r"[和与及、]", block):
        i = 0
        while i < len(seg):
            matched = False
            for ln in (4, 3, 2):
                if i + ln <= len(seg) and seg[i : i + ln] in STOP_TERMS:
                    i += ln
                    matched = True
                    break
            if matched:
                continue
            if seg[i] in VERBS or seg[i] in STOP_TERMS:
                i += 1
                continue
            word = seg[i : i + 4]
            word = word.rstrip("了的地得和与及吧呀啊呢")
            # 去掉尾部动词
            while word and word[-1] in VERBS:
                word = word[:-1]
            # 名词内嵌动词则截断（如"单词学英"→"单词"、"家在重庆"→"重庆"）
            if len(word) >= 3:
                for v in ("学", "背", "读", "看", "在"):
                    pos = word.find(v)
                    if 0 < pos < len(word) - 1:
                        word = word[:pos]
                        break
            # "的"结构截断（如"团子的猫"→"团子"）
            if len(word) >= 3:
                dpos = word.find("的")
                if 0 < dpos < len(word) - 1:
                    word = word[:dpos]
            if len(word) >= 2:
                return word
            i += 1
    return ""


def extract_trait(text: str) -> tuple[str, str]:
    """从记忆文本提取画像特征。

    Returns:
        (trait_key, trait_value) —— key 用主题词或类别，value 是核心句。
    """
    category = classify_category(text)
    key = ""
    # 偏好类：优先取"喜欢/讨厌"等关键词后面的名词短语（更精准）
    if category in ("偏好", "偏好负面"):
        for kw in ["不喜欢", "喜欢", "爱吃", "爱喝", "讨厌", "反感", "最爱", "受不了"]:
            idx = text.find(kw)
            if idx >= 0:
                after = text[idx + len(kw):]
                m = re.search(r"[\u4e00-\u9fff]+", after)
                if m:
                    key = _extract_first_noun(m.group())
                    if key:
                        break
    # 习惯类：周期词后的行为对象
    if not key and category == "习惯":
        for kw in ["每天早上", "每晚", "每周", "每天", "每月", "每次", "经常", "总是", "习惯", "固定", "平时", "日常"]:
            idx = text.find(kw)
            if idx >= 0:
                after = text[idx + len(kw):]
                m = re.search(r"[\u4e00-\u9fff]+", after)
                if m:
                    key = _extract_first_noun(m.group())
                    if key:
                        break
    # 学习类：动词后的学习对象
    if not key and category == "学习":
        for kw in ["背单词", "学习", "复习", "看书", "背", "学"]:
            idx = text.find(kw)
            if idx >= 0:
                after = text[idx + len(kw):]
                m = re.search(r"[\u4e00-\u9fff]+", after)
                if m:
                    key = _extract_first_noun(m.group())
                    if key:
                        break
    # 宠物/命名类：含"叫"且有宠物词 → 取"叫"后面的名字
    if not key and "叫" in text and any(p in text for p in ("猫", "狗", "宠物")):
        idx = text.find("叫")
        m = re.search(r"[\u4e00-\u9fff]+", text[idx + 1:])
        if m:
            key = _extract_first_noun(m.group())
    # 时间词优先（作息/日程类画像主题）
    if not key:
        for tw in TIME_WORDS:
            if tw in text:
                key = tw
                break
    # 通用：整段文本提取第一个名词
    if not key:
        for m in re.finditer(r"[\u4e00-\u9fff]+", text):
            key = _extract_first_noun(m.group())
            if key:
                break
    if not key:
        key = category
    # value：取记忆首句（含主题词的句子），截断 60 字
    value = text.strip()
    if len(value) > 60:
        value = value[:60] + "…"
    return key, value


class ProfileManager:
    """记忆画像服务。"""

    def __init__(self, store: V2Store):
        self.store = store

    async def update_from_memory(
        self,
        memory_id: int,
        persona_id: str | None,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict]:
        """新记忆写入后更新画像。返回更新的特征列表。"""
        if not persona_id or not content or len(content.strip()) < 4:
            return []
        metadata = metadata or {}
        trait_key, trait_value = extract_trait(content)
        importance = float(metadata.get("importance") or 0.5)
        # 置信度：显式工具写入 + 高重要性 → 高置信
        origin = str(metadata.get("memory_origin") or "")
        base_conf = 0.5 + (importance - 0.5) * 0.6
        if "agent" in origin or metadata.get("memorize_reason"):
            base_conf = min(0.95, base_conf + 0.25)
        base_conf = max(0.15, min(0.95, base_conf))

        note = f"记忆#{memory_id} 更新画像: {content[:40]}"
        await self.store.upsert_profile_trait(
            persona_id=persona_id,
            trait_key=trait_key,
            trait_value=trait_value,
            confidence=base_conf,
            evidence_id=memory_id,
            evolution_note=note,
        )
        logger.debug(f"[v2] 画像更新: persona={persona_id} trait={trait_key} conf={base_conf:.2f}")
        # v2.1 家庭反馈 A3：画像更新 → PROFILE_UPDATED（驱动表达联动）
        try:
            if _HAS_BUS and MemoryEventType is not None:
                bus = get_event_bus()
                if bus is not None:
                    await bus.publish(
                        MemoryEvent(
                            type=MemoryEventType.PROFILE_UPDATED,
                            memory_id=memory_id,
                            memory_type="profile",
                            metadata={
                                "persona_id": persona_id,
                                "trait_key": trait_key,
                                "trait_value": trait_value,
                                "confidence": base_conf,
                            },
                        )
                    )
        except BaseException:
            logger.warning("[v2] PROFILE_UPDATED 事件发布失败", exc_info=True)
        try:
            await self.store.record_feedback(
                from_module="profile",
                to_module="expression",
                event_type="profile_updated",
                memory_id=memory_id,
                persona_id=persona_id,
                payload={"trait_key": trait_key, "trait_value": trait_value, "confidence": base_conf},
            )
        except BaseException:
            logger.warning("[v2] feedback_log 记录失败", exc_info=True)
        return [
            {
                "trait_key": trait_key,
                "trait_value": trait_value,
                "confidence": base_conf,
            }
        ]

    async def upsert_from_emotion(self, persona_id: str, trend: dict) -> None:
        """A5：情感趋势反哺画像——主导情绪写成「情绪倾向」画像特征。

        trend 来自 EmotionEngine.get_recent_trend（helper 侧），
        事件经 EventBus EMOTION_TREND 送达。失败不影响情感引擎。
        """
        if not persona_id or not trend:
            return
        dominant = trend.get("dominant") or trend.get("dominant_emotion")
        if not dominant:
            return
        trait_key = "情绪倾向"
        trait_value = str(dominant)
        stability = float(trend.get("stability") or 0.5)
        conf = max(0.35, min(0.8, stability))
        try:
            await self.store.upsert_profile_trait(
                persona_id=persona_id,
                trait_key=trait_key,
                trait_value=trait_value,
                confidence=conf,
                evidence_id=None,
                evolution_note="情感趋势反哺画像（A5）",
            )
            logger.debug(f"[v2] A5 情感→画像: persona={persona_id} dominant={trait_value}")
        except BaseException:
            logger.warning("[v2] A5 情感→画像失败", exc_info=True)

    async def get_summary(self, persona_id: str, limit: int = 10) -> dict:
        """返回画像摘要（供 Agent 使用）。"""
        traits = await self.store.get_profile(persona_id)
        traits = traits[:limit]
        return {
            "persona_id": persona_id,
            "trait_count": len(traits),
            "traits": [
                {
                    "key": t.get("trait_key"),
                    "value": t.get("trait_value"),
                    "confidence": round(float(t.get("confidence") or 0), 3),
                    "evidence_ids": t.get("evidence_ids") or [],
                    "evolution_count": len(t.get("evolution_log") or []),
                    "updated_at": t.get("updated_at"),
                }
                for t in traits
            ],
        }
