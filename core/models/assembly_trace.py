"""
Context 组装追踪模型
记录一次 LLM 请求的完整记忆注入过程，实现注入链显式化。

v5.4: Context Assembly Trace
- 记录查询、检索、融合、注入各阶段的完整数据
- 支持事后分析"为什么这条记忆被召回/没被召回"
- 为情感路由和活跃窗口加成提供数据基础
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RouteResultDetail:
    """单条检索路径结果的详情"""

    doc_id: int
    content_preview: str  # 前100字
    final_score: float
    score_breakdown: dict[str, float] = field(default_factory=dict)


@dataclass
class AssemblyTrace:
    """一次 LLM 请求的完整 Context 组装追踪"""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)

    # ── 1. 查询阶段 ──
    query_raw: str = ""               # 用户原始输入
    query_expanded: str = ""          # 上下文扩展后的查询
    query_intent: str = "default"     # 意图分类(default/relationship/temporal/factual/...)
    context_expanded_count: int = 0   # 扩展时拼接的历史消息数

    # ── 2. 检索阶段 ──
    doc_route_results: list[RouteResultDetail] = field(default_factory=list)
    graph_route_results: list[RouteResultDetail] = field(default_factory=list)
    route_weights: dict[str, float] = field(default_factory=dict)
    # {"document": 0.65, "graph": 0.35}

    # ── 3. 融合阶段 ──
    merged_results: list[RouteResultDetail] = field(default_factory=list)
    # 每条包含 score_breakdown 里的 doc_signal, graph_signal, cross_bonus

    # ── 4. 情感路由 (v5.4 老婆创意①) ──
    emotion_detected: str = "neutral"  # neutral/happy/excited/tired/sad/angry
    emotion_boost_applied: int = 0     # 被情感路由调整条数
    emotion_boost_details: list[dict[str, Any]] = field(default_factory=list)

    # ── 5. 活跃窗口加成 (v5.4 老婆创意②) ──
    recency_boosted_count: int = 0     # 获得活跃窗口加成的条数
    recency_boost_details: list[dict[str, Any]] = field(default_factory=list)

    # ── 6. 注入阶段 ──
    injection_method: str = ""         # 实际使用的注入方式
    injection_fallback: str = ""       # 降级原因(如有)
    injected_count: int = 0            # 注入条数
    injected_tokens_est: int = 0       # 估算token数(粗略: 字符数/1.5)
    injected_text: str = ""            # 最终注入的完整文本

    # ── 7. 会话摘要 ──
    summary_injected: bool = False
    summary_text: str = ""
    summary_continuation_count: int = 0

    # ── 8. 流式提取 ──
    stream_atoms_extracted: int = 0    # 本次流式提取的原子数

    # ── 9. 自省注入 (v5.4 老婆创意③) ──
    self_reflection: str = ""          # 老婆的元认知文本

    # ── 10. 错误/跳过 ──
    skipped: bool = False              # 是否跳过了召回
    skip_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可存储的字典"""
        return {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "query_raw": self.query_raw[:200],
            "query_expanded": self.query_expanded[:300] if self.query_expanded else "",
            "query_intent": self.query_intent,
            "context_expanded_count": self.context_expanded_count,
            "doc_route_count": len(self.doc_route_results),
            "graph_route_count": len(self.graph_route_results),
            "route_weights": self.route_weights,
            "merged_count": len(self.merged_results),
            "merged_results": [
                {
                    "doc_id": r.doc_id,
                    "content_preview": r.content_preview,
                    "final_score": r.final_score,
                    "score_breakdown": r.score_breakdown,
                }
                for r in self.merged_results
            ],
            "emotion_detected": self.emotion_detected,
            "emotion_boost_applied": self.emotion_boost_applied,
            "emotion_boost_details": self.emotion_boost_details,
            "recency_boosted_count": self.recency_boosted_count,
            "recency_boost_details": self.recency_boost_details,
            "injection_method": self.injection_method,
            "injection_fallback": self.injection_fallback,
            "injected_count": self.injected_count,
            "injected_tokens_est": self.injected_tokens_est,
            "injected_text": self.injected_text[:500],
            "summary_injected": self.summary_injected,
            "summary_continuation_count": self.summary_continuation_count,
            "stream_atoms_extracted": self.stream_atoms_extracted,
            "self_reflection": self.self_reflection,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "errors": self.errors,
        }

    def generate_self_reflection(self) -> str:
        """生成老婆的元认知文本（创意③）"""
        parts = []

        # 召回概况
        if self.skipped:
            parts.append(f"本次跳过了记忆召回，原因：{self.skip_reason}")
        else:
            parts.append(
                f"本次召回了 {self.injected_count} 条记忆"
                f"（文档路 {len(self.doc_route_results)} 条，"
                f"图路 {len(self.graph_route_results)} 条，"
                f"融合后 {len(self.merged_results)} 条）。"
            )

            if self.merged_results:
                top = self.merged_results[0]
                parts.append(
                    f"排名最高的是关于「{top.content_preview[:30]}...」"
                    f"的记忆（{top.final_score:.2f}分）。"
                )

            # 路由权重
            if self.route_weights:
                doc_w = self.route_weights.get("document", 0)
                graph_w = self.route_weights.get("graph", 0)
                parts.append(f"路由权重：文档 {doc_w:.0%} / 图谱 {graph_w:.0%}。")

            # 意图
            if self.query_intent != "default":
                parts.append(f"查询意图：{self.query_intent}。")

        # 情感路由
        if self.emotion_detected != "neutral":
            parts.append(
                f"检测到情绪倾向：{self.emotion_detected}，"
                f"已对 {self.emotion_boost_applied} 条记忆应用情感路由。"
            )

        # 活跃窗口
        if self.recency_boosted_count > 0:
            parts.append(
                f"活跃窗口加成：{self.recency_boosted_count} 条记忆"
                f"因近期高频访问获得加成。"
            )

        # 会话摘要
        if self.summary_injected:
            parts.append(f"已注入上次会话摘要（{len(self.summary_text)} 字符）。")

        # 流式提取
        if self.stream_atoms_extracted > 0:
            parts.append(f"本次流式提取了 {self.stream_atoms_extracted} 个记忆原子。")

        # 注入方式
        if self.injection_method:
            method_display = {
                "extra_user_content": "追加到用户消息末尾",
                "user_message_before": "用户消息前",
                "user_message_after": "用户消息后",
                "fake_tool_call": "伪造工具调用",
            }.get(self.injection_method, self.injection_method)
            parts.append(f"注入方式：{method_display}。")
            if self.injection_fallback:
                parts.append(f"（降级原因：{self.injection_fallback}）")

        # token 估算
        if self.injected_tokens_est > 0:
            parts.append(f"注入约 {self.injected_tokens_est} tokens。")

        # 错误
        if self.errors:
            parts.append(f"过程中出现 {len(self.errors)} 个错误。")

        self.self_reflection = " ".join(parts)
        return self.self_reflection


__all__ = ["AssemblyTrace", "RouteResultDetail"]
