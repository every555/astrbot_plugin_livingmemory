"""三级冲突检测器 —— 抓住橘子真实的立场变化，而不是字面撞车。

【这个功能的目的】
记忆系统真正关心的"矛盾"只有一种：**橘子的立场/偏好/态度发生了变化**
（例如：以前讨厌香菜 → 现在喜欢香菜；先同意买椅子 → 后反对）。
这类矛盾值得抓，因为确认后能触发预言重估（A2）与知识沉淀（A8），
让春雪理解"橘子变了"，而不是死守旧画像。

以下情况**不是矛盾**，绝不报告：
- 技术事实的时序差异（昨天失败今天成功 = 正常演进）
- 疑问句 vs 陈述句（"是不是X？"≠ 否认X）
- 长存档之间的字面巧合（两篇长文各含"是"和"不是"）
- 立场一致但措辞不同（"不喜欢"和"讨厌"是同一立场）

【判定规则（两级门，全过才报）】
第一门·同话题门 has_shared_topic：
  ① 双方共享至少 1 个完整词段（被标点/数字切断的汉字串或英文词）
     → 长存档之间几乎不可能碰巧共享，共享即真同话题；
  ② 或双方均为短文本（bigram≤20，如单句偏好记忆）且共享 ≥2 个二元词
     → 短句之间碎片少，2 个共享词足以认定同话题。
  长文本对只认 ①：两条长存档靠语法碎片凑数过门的历史教训见下。
第二门·立场门 find_possible_conflict：
  立场词对仅保留 3 组「肯定词 vs 负面词」：喜欢/爱/同意。
  否定语境保护：「不喜欢」不算「喜欢」命中（lookbehind 排除 不/没/别/非/未/无 前缀），
  双重否定（不喜欢 vs 讨厌）静默。
  翻转检测：同一立场词一方肯定、一方带否定前缀 → 报"偏好翻转"（橘子改口了）。

【词对瘦身史（每一条都是尸体堆出来的教训）】
2026-08-07  清 1485 条噪音：删 有/想/要/忙，主题门槛 1 词→2 词
2026-08-18  清 4780 条冤案：「是/不是」2397 条、「能/不能」461、「会/不会」171、
            「需要/不需要」119、「可以/不可以」117、「成功/失败」108（技术语境常态对立）。
            根因：负面词「不X」包含正面词「X」造成子串自撞 + bigram 语法碎片
            （"了一/子说/的是"）让长存档对虚假共享主题。
            本次删词对：是/会/能/可以/需要/习惯/正确/真/成功/记得（含全部"不X⊃X"灾区）；
            「支持/反对」同日二次删除——技术语境"支持某功能"是兼容性描述不是人的立场，
            回放验证 3482 条历史冤案中存活 35 条全是它。

【设计哲学】高精度、低召回。词法层只出候选，宁漏勿滥——
漏掉的真矛盾可以靠 LLM 语义层（后续）补，误报则直接淹死确认流程：
8/7 定的 A 方案（确认→预言重估→知识沉淀）在 4780:0 的信噪比下从未被使用过。

三级结构：
L1 内容冲突：新旧记忆词法级立场对立
L2 因果冲突：L1 命中的旧记忆处于因果链中 → 升级关注
L3 画像冲突：新记忆与画像核心特征矛盾（带特征质量门：碎片键/超长复述值直接跳过）

写入前执行，检测结果记录到 memory_conflicts 表。
"""

import re
import time
from typing import Any

from .v2_store import V2Store
from .transitive_closure import check_transitive, ontology_db_path, sakura_heartbeat

try:
    from astrbot.api import logger
except ImportError:  # 独立测试环境降级
    import logging

    logger = logging.getLogger("v2_conflict")

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


# ── 立场词对（2026-08-18 大瘦身：仅保留"人的立场"类词对，历史教训见文件头）──
# 只保留肯定词 vs 明确负面词；否定形式（不喜欢/不爱/不同意/不支持）不入负面词表，
# 由 _has_negated 翻转检测单独处理，避免「不X⊃X」子串自撞（曾日产数百条冤案）。
CONTRADICTION_PAIRS: list[tuple[str, list[str]]] = [
    ["喜欢", ["讨厌", "反感", "厌恶", "没兴趣", "无感"]],
    ["爱", ["讨厌", "恨", "厌恶"]],
    ["同意", ["反对", "否决"]],
]

# 否定前缀：正面词紧跟这些字时不算肯定命中（「不喜欢」≠「喜欢」）
NEG_PREFIX = "不没别非未无"

# 称呼词：在 bigram 层参与共享计数（短句对"橘子+实词"共享=真同话题），
# 但仍留在 STOP_TERMS 不作为独立主题词。
ADDRESS_TERMS = {"橘子", "老婆", "老公", "春雪", "小桔橘", "明江", "小雪"}

STOP_TERMS = {
    "用户", "一个", "一种", "这个", "那个", "自己", "因为", "所以",
    "但是", "没有", "不是", "不会", "不能", "喜欢", "讨厌", "反感",
    "厌恶", "不爱", "不想", "不要", "不行", "不忙", "觉得", "感觉",
    "今天", "昨天", "明天", "现在", "之前", "然后", "但是", "而且",
    "就是", "其实", "真的",
    # 称呼词（避免被当作主题）
    "橘子", "老婆", "老公", "春雪", "小桔橘", "明江", "小雪",
    "我", "你", "他", "她", "我们", "你们", "他们",
    # 高频虚词/动词/动作词
    "都", "会", "去", "打", "吃", "看", "玩", "是", "在", "有",
    "要", "想", "就", "也", "和", "与", "了", "的", "吧", "呀",
    # 计划/习惯类动作词（让名词成为主题）
    "备考", "准备", "打算", "计划", "目标", "报名", "学习", "复习",
    "考试", "每周", "每天", "每月", "每次", "经常", "总是", "习惯",
}


def _normalize(text: str) -> str:
    return text.lower()


def _extract_terms(text: str) -> tuple[set[str], set[str]]:
    """提取 (完整词段, bigram 碎片)。

    完整词段 = 被标点/数字切断的汉字串（≥2字）或英文数字串（≥3字符），
    两条不同记忆碰巧共享完整词段的概率极低 → 是真同话题的强信号。
    bigram = 汉字段内拆出的二元碎片，仅用于短文本对的共享判定。
    """
    full: set[str] = set()
    bi: set[str] = set()
    matches = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9]{3,}", text) or []
    for raw in matches:
        term = raw.lower()
        if term in STOP_TERMS:
            continue
        full.add(term)
        if re.fullmatch(r"[\u4e00-\u9fff]+", term) and len(term) > 2:
            for i in range(len(term) - 1):
                gram = term[i : i + 2]
                # 称呼词放行参与共享计数（见 ADDRESS_TERMS 注释）
                if gram not in STOP_TERMS or gram in ADDRESS_TERMS:
                    bi.add(gram)
    return full, bi


def has_shared_topic(text_a: str, text_b: str) -> bool:
    """同话题门（双轨）：
    ① 共享完整词段 ≥1 → 过（长短通吃的强信号）
    ② 双方均为短文本（bigram≤20）且共享 bigram ≥2 → 过
    长文本对只认 ①——长存档靠语法碎片（了一/子说/的是）凑数过门是 4780 条冤案的根因。
    """
    a_full, a_bi = _extract_terms(text_a)
    b_full, b_bi = _extract_terms(text_b)
    if a_full & b_full:
        return True
    shared = a_bi & b_bi
    if not shared:
        return False
    return len(a_bi) <= 20 and len(b_bi) <= 20 and len(shared) >= 2


def _has_positive(text: str, word: str) -> bool:
    """肯定命中检测（带否定语境保护）：「不喜欢」「没兴趣」不算「喜欢」命中。"""
    return re.search(rf"(?<![{NEG_PREFIX}]){re.escape(word)}", text) is not None


def _has_negated(text: str, word: str) -> bool:
    """翻转检测：word 带否定前缀出现（「喜欢」→「不喜欢」= 立场翻转信号）。"""
    return re.search(rf"[{NEG_PREFIX}]{re.escape(word)}", text) is not None


def find_possible_conflict(new_content: str, existing_content: str) -> dict | None:
    """立场门：同话题前提下，检测立场词对立或偏好翻转。返回 None 或 {reason, confidence, pair}。"""
    if not has_shared_topic(new_content, existing_content):
        return None
    a = _normalize(new_content)
    b = _normalize(existing_content)
    for positive, negatives in CONTRADICTION_PAIRS:
        a_pos = _has_positive(a, positive)
        b_pos = _has_positive(b, positive)
        a_neg = any(neg in a for neg in negatives)
        b_neg = any(neg in b for neg in negatives)
        # 立场对立：一方肯定，另一方明确负面词
        if (a_pos and b_neg) or (b_pos and a_neg):
            return {
                "reason": f"立场对立: 一方「{positive}」另一方负面词「{negatives[0]}」",
                "confidence": 0.65,
                "pair": positive,
            }
        # 偏好翻转：同一立场词，一方肯定一方否定（橘子改口了）
        if (a_pos and _has_negated(b, positive)) or (b_pos and _has_negated(a, positive)):
            return {
                "reason": f"偏好翻转: 「{positive}」一方肯定一方否定",
                "confidence": 0.65,
                "pair": positive,
            }
    return None


class ConflictDetector:
    """三级冲突检测器。"""

    def __init__(self, store: V2Store, db_connection=None):
        self.store = store
        self.db = db_connection

    async def detect_all(
        self,
        new_memory_id: int,
        new_content: str,
        persona_id: str | None,
        new_metadata: dict[str, Any] | None = None,
        recent_memories: list[dict] | None = None,
    ) -> list[dict]:
        """对新写入的记忆执行三级冲突检测，返回检测到的冲突列表（并已落库）。

        Args:
            recent_memories: 可选的候选旧记忆列表 [{id, content}]，省去查库。
        """
        new_metadata = new_metadata or {}
        conflicts: list[dict] = []
        if not new_content or len(new_content.strip()) < 4:
            return conflicts

        old_memories = recent_memories or await self._fetch_recent(persona_id, new_memory_id, limit=30)

        # ── L1 内容冲突 ──
        for old in old_memories:
            old_content = str(old.get("content") or "")
            if not old_content:
                continue
            candidate = find_possible_conflict(new_content, old_content)
            if candidate:
                conflicts.append(
                    await self._record_conflict(
                        new_memory_id=new_memory_id,
                        old_memory_id=int(old["id"]),
                        level=1,
                        conflict_type="content",
                        reason=candidate["reason"],
                        confidence=candidate["confidence"],
                    )
                )

        # ── L2 因果冲突：新记忆与旧记忆存在矛盾，且旧记忆处于因果链中 ──
        # 简化：L1 命中的旧记忆若已挂因果记录（是某条记忆的 pre_cause 或 result），升级为 L2 关注
        for conflict in list(conflicts):
            old_id = conflict["old_memory_id"]
            if await self._is_in_causal_chain(old_id):
                await self.store.update_conflict_status(
                    conflict["id"], "candidate", {"upgraded_to_l2": True, "reason": "旧记忆处于因果链中"}
                )

        # ── L3 画像冲突 ──
        # 特征质量门（2026-08-18）：画像提取器偶发产出碎片键（如 trait_key="今天"/"实话"）
        # 和整段记忆复述值，这类"特征"参与词法检测只产出冤案（1298 条教训），直接跳过。
        if persona_id:
            profile = await self.store.get_profile(persona_id)
            for trait in profile:
                trait_key = str(trait.get("trait_key") or "")
                trait_value = str(trait.get("trait_value") or "")
                if len(trait_key) < 2 or trait_key in STOP_TERMS:
                    continue  # 碎片键不是合格特征
                if len(trait_value) > 200:
                    continue  # 超长复述值是记忆原文，不是特征
                if not trait_value:
                    continue
                candidate = find_possible_conflict(new_content, trait_value)
                if candidate:
                    conflicts.append(
                        await self._record_conflict(
                            new_memory_id=new_memory_id,
                            old_memory_id=0,  # 画像不是 documents 记忆
                            level=3,
                            conflict_type="profile",
                            reason=f"与画像特征「{trait.get('trait_key')}={trait_value}」矛盾: {candidate['reason']}",
                            confidence=max(candidate["confidence"], 0.7),
                        )
                    )

        # ── L1.5 传递闭包（P0-2：间接矛盾·反射弧②，2026-08-30）──
        # 沿 ontology 图 BFS(<=3跳) 推导事实，抓两两比对抓不住的间接矛盾（喜鹊悖论）。
        # 免疫降级铁律：任何异常只告警，绝不阻断入库主流程。
        try:
            _tc_db = ontology_db_path()
            if _tc_db:
                for _hit in check_transitive(new_content, _tc_db):
                    _rec = await self._record_conflict(
                        new_memory_id=new_memory_id,
                        old_memory_id=0,  # 证据来自图推导，非某条具体记忆
                        level=1,
                        conflict_type="transitive",
                        reason=f'[图推导] {_hit["reason"]}',
                        confidence=_hit["confidence"],
                    )
                    conflicts.append(_rec)
                    sakura_heartbeat("immune.transitive_conflict", {
                        "new_memory_id": new_memory_id, "rel": _hit.get("rel", ""),
                        "evidence": _hit.get("evidence_chain", [])[:3]})
        except Exception as _tc_err:
            logger.warning(f"[v2] L1.5 传递闭包降级: {_tc_err}")
        if conflicts:
            logger.info(f"[v2] 三级冲突检测完成: 发现 {len(conflicts)} 条冲突候选")
        return conflicts

    async def _record_conflict(
        self,
        new_memory_id: int,
        old_memory_id: int,
        level: int,
        conflict_type: str,
        reason: str,
        confidence: float,
    ) -> dict:
        cid = await self.store.add_conflict(
            new_memory_id=new_memory_id,
            old_memory_id=old_memory_id,
            level=level,
            conflict_type=conflict_type,
            reason=reason,
            confidence=confidence,
            status="candidate",
        )
        return {
            "id": cid,
            "new_memory_id": new_memory_id,
            "old_memory_id": old_memory_id,
            "level": level,
            "conflict_type": conflict_type,
            "reason": reason,
            "confidence": confidence,
            "status": "candidate",
            "created_at": time.time(),
        }

    async def _is_in_causal_chain(self, memory_id: int) -> bool:
        """该记忆是否已挂因果记录（是前因或结果）。"""
        entry = await self.store.get_causality(memory_id)
        if entry:
            return True
        causes = await self.store.get_causes(memory_id)
        return len(causes) > 0

    async def _fetch_recent(
        self, persona_id: str | None, exclude_id: int, limit: int = 30
    ) -> list[dict]:
        """从 documents 表取最近记忆（供冲突对比）。"""
        if self.db is None:
            return []
        try:
            if persona_id:
                cursor = await self.db.execute(
                    """
                    SELECT id, text, metadata FROM documents
                    WHERE json_extract(metadata, '$.persona_id') = ? AND id != ?
                    ORDER BY COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), id) DESC
                    LIMIT ?
                    """,
                    (persona_id, exclude_id, limit),
                )
            else:
                cursor = await self.db.execute(
                    """
                    SELECT id, text, metadata FROM documents
                    WHERE id != ?
                    ORDER BY COALESCE(CAST(json_extract(metadata, '$.create_time') AS REAL), id) DESC
                    LIMIT ?
                    """,
                    (exclude_id, limit),
                )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                result.append(
                    {
                        "id": row["id"] if isinstance(row, dict) else row[0],
                        "content": row["text"] if isinstance(row, dict) else row[1],
                    }
                )
            return result
        except BaseException:
            return []

    async def resolve(
        self,
        conflict_id: int,
        resolution_type: str = "preference_evolution",
        reason: str = "",
    ) -> None:
        """标记冲突已解决（人工/LLM 判定后的落库）。"""
        await self.store.update_conflict_status(
            conflict_id,
            "resolved",
            {"type": resolution_type, "reason": reason, "resolved_at": time.time()},
        )
        # v2.1 家庭反馈 A2/A8：冲突确认 → CONFLICT_CONFIRMED（预言重估 + 知识沉淀）
        try:
            if _HAS_BUS and MemoryEventType is not None:
                bus = get_event_bus()
                if bus is not None:
                    await bus.publish(
                        MemoryEvent(
                            type=MemoryEventType.CONFLICT_CONFIRMED,
                            memory_id=conflict_id,
                            memory_type="conflict",
                            metadata={
                                "resolution_type": resolution_type,
                                "reason": reason,
                            },
                        )
                    )
        except BaseException:
            logger.warning("[v2] CONFLICT_CONFIRMED 事件发布失败", exc_info=True)
        try:
            await self.store.record_feedback(
                from_module="conflict",
                to_module="prophecy",
                event_type="conflict_confirmed",
                memory_id=conflict_id,
                payload={"resolution_type": resolution_type, "reason": reason[:80]},
            )
        except BaseException:
            logger.warning("[v2] feedback_log 记录失败", exc_info=True)
