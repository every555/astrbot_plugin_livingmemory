"""
P2-14 上下文预算工程（Context Budget Engineering）

两个组件：
1. 证据质量闸门 apply_evidence_gate —— 绝对分数门槛 + 相对 top1 门槛，拦低分噪声；
2. Token 预算装填器 pack_with_budget —— 按分数降序装填，超长截断保头，top1 保底。

背景：防 MMA 逻辑塌陷（无关低质记忆稀释注意力）；Vercel 案例中上下文工程
可将准确率从 80% 拉到 100%。本模块为纯函数库，不触碰检索排序主链。
"""
from __future__ import annotations

from dataclasses import dataclass, field


TRUNC_MARKER = "…[已截断]"
DEFAULT_META_OVERHEAD_TOKENS = 30  # 每条记忆元数据行(重要性/时间/主题)的估算开销


@dataclass
class BudgetReport:
    """预算装填报告，供 assembly_trace 记录可观测性。"""

    gate_dropped: int = 0
    dedup_dropped: int = 0
    truncated: int = 0
    injected: int = 0
    est_tokens: int = 0
    budget_tokens: int = 0


def estimate_tokens(text: str) -> int:
    """与 memory_recall.injected_tokens_est 同口径：len/1.5。"""
    return max(1, int(len(text) / 1.5))


def apply_evidence_gate(
    memories: list[dict], *, min_score: float = 0.30, relative_ratio: float = 0.0
) -> tuple[list[dict], int]:
    """证据质量闸门：score < max(min_score, top1*relative_ratio) 的记忆拦下。"""
    if not memories:
        return [], 0
    top1 = max(m.get("score", 0.0) for m in memories)
    threshold = max(min_score, top1 * relative_ratio)
    kept = [m for m in memories if m.get("score", 0.0) >= threshold]
    return kept, len(memories) - len(kept)


def _cjk_bigrams(text: str) -> set[str]:
    """CJK bigram 集合（与 dream_engine 同款思路）：中文按相邻双字，其余按单字符。"""
    grams: set[str] = set()
    prev = ""
    for ch in text:
        if ord(ch) > 0x2E7F:  # CJK 区
            if prev:
                grams.add(prev + ch)
            prev = ch
        else:
            grams.add(ch)
            prev = ""
    return grams


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _topics_of(m: dict) -> set:
    raw = (m.get("metadata") or {}).get("topics", [])
    if isinstance(raw, str):
        raw = [raw]
    return {str(t) for t in raw if t}


def _same_family(m: dict, kept_m: dict, *, min_jaccard: float, near_time_sec: float = 900.0) -> bool:
    """三信号判定同族（任一命中即同族）：
    ① topics 标签交集 >= 2（结构化最强信号，如三版棋局记忆都带「五子棋」）
    ② 前缀 CJK-bigram Jaccard >= min_jaccard（字面高度相似）
    ③ 创建时间戳相近（<near_time_sec）且 Jaccard >= min_jaccard*0.625（同晚连存的弱字面同族）
    """
    ta, tb = _topics_of(m), _topics_of(kept_m)
    if len(ta & tb) >= 2:
        return True
    ga = _cjk_bigrams(str(m.get("content", ""))[:40])
    gb = _cjk_bigrams(str(kept_m.get("content", ""))[:40])
    j = _jaccard(ga, gb)
    if j >= min_jaccard:
        return True
    ta_ts = (m.get("metadata") or {}).get("create_time", 0) or m.get("timestamp", 0)
    tb_ts = (kept_m.get("metadata") or {}).get("create_time", 0) or kept_m.get("timestamp", 0)
    try:
        if ta_ts and tb_ts and abs(float(ta_ts) - float(tb_ts)) < near_time_sec and j >= min_jaccard * 0.625:
            return True
    except (TypeError, ValueError):
        pass
    return False


def dedup_by_prefix(
    memories: list[dict], *, prefix_len: int = 40, min_jaccard: float = 0.40
) -> tuple[list[dict], int]:
    """冗余去重：同族记忆（同一事件的多个版本）只留分数最高的一条。

    同族判定用三信号（topics 交集 / 字面 bigram / 时间戳+弱字面）。
    返回顺序按分数降序。
    """
    if not memories:
        return [], 0
    ordered = sorted(memories, key=lambda m: m.get("score", 0.0), reverse=True)
    kept: list[dict] = []
    dropped = 0
    for m in ordered:
        if any(_same_family(m, km, min_jaccard=min_jaccard) for km in kept):
            dropped += 1
        else:
            kept.append(m)
    return kept, dropped


def _entry_cost(content: str, meta_overhead: int) -> int:
    return estimate_tokens(content) + meta_overhead


def pack_with_budget(
    memories: list[dict],
    *,
    budget_tokens: int = 1200,
    meta_overhead: int = DEFAULT_META_OVERHEAD_TOKENS,
    min_entry_tokens: int = 40,
) -> tuple[list[dict], BudgetReport]:
    """Token 预算装填器：按分数降序装填，超长截断（保头加标记），top1 保底。

    - 预算内尽量多装；
    - 单条装不下时若剩余空间 >= min_entry_tokens 则截断装入；
    - top1 在任何预算下保证至少注入截断版；
    - 不修改原列表（浅拷贝）。返回 (packed, report)。
    """
    if not memories:
        return [], BudgetReport(budget_tokens=budget_tokens)
    ordered = sorted(memories, key=lambda m: m.get("score", 0.0), reverse=True)
    packed: list[dict] = []
    remaining = budget_tokens
    truncated = 0
    for i, m in enumerate(ordered):
        cost = _entry_cost(str(m.get("content", "")), meta_overhead)
        if cost <= remaining:
            packed.append(m)
            remaining -= cost
            continue
        avail_content_tokens = remaining - meta_overhead - 6  # 留截断标记
        if avail_content_tokens < min_entry_tokens - meta_overhead and i == 0:
            # top1 保底：即使预算极小也注入截断版
            avail_content_tokens = max(10, budget_tokens - meta_overhead - 6)
        if avail_content_tokens >= 10:
            avail_chars = int(avail_content_tokens * 1.5)
            truncated_m = {**m, "content": str(m.get("content", ""))[:avail_chars] + TRUNC_MARKER}
            packed.append(truncated_m)
            truncated += 1
            remaining = 0
        else:
            break
    est = sum(_entry_cost(str(m.get("content", "")), meta_overhead) for m in packed)
    report = BudgetReport(
        truncated=truncated,
        injected=len(packed),
        est_tokens=est,
        budget_tokens=budget_tokens,
    )
    return packed, report
