"""
Context Assembly Trace 处理模块
为 WebUI 提供 Trace 列表、详情和统计接口。
"""

from typing import TYPE_CHECKING, Any

from quart import request

from astrbot.api import logger

if TYPE_CHECKING:
    from .utils import PageApiUtils


class TraceHandler:
    """Context 组装追踪处理器"""

    def __init__(self, utils: "PageApiUtils"):
        self.utils = utils

    def _get_trace_store(self, plugin) -> Any:
        """从插件实例获取 ContextTraceStore"""
        event_handler = getattr(plugin, "event_handler", None)
        if event_handler is None:
            return None
        return getattr(event_handler, "context_trace_store", None)

    async def get_trace_list(self, plugin) -> dict[str, Any]:
        """获取最近的 Trace 列表

        Query params:
            limit: 返回条数（默认 20，最大 100）
            session_id: 可选，按会话过滤
        """
        try:
            trace_store = self._get_trace_store(plugin)
            if trace_store is None:
                return self.utils.ok({
                    "traces": [],
                    "stats": {},
                    "message": "Trace store not initialized",
                })

            query = request.args
            try:
                limit = min(100, max(1, int(query.get("limit", 20))))
            except (TypeError, ValueError):
                limit = 20
            session_id = self.utils.optional_text(query.get("session_id"))

            traces = await trace_store.get_recent_traces(
                session_id=session_id, limit=limit
            )

            # 精简列表数据（不包含 merged_results 等大字段）
            summary_list = []
            for t in traces:
                summary_list.append({
                    "trace_id": t.get("trace_id", ""),
                    "session_id": t.get("session_id", ""),
                    "timestamp": t.get("timestamp", 0),
                    "query_raw": t.get("query_raw", ""),
                    "query_intent": t.get("query_intent", "default"),
                    "emotion_detected": t.get("emotion_detected", "neutral"),
                    "injected_count": t.get("injected_count", 0),
                    "injected_tokens_est": t.get("injected_tokens_est", 0),
                    "doc_route_count": t.get("doc_route_count", 0),
                    "graph_route_count": t.get("graph_route_count", 0),
                    "merged_count": t.get("merged_count", 0),
                    "skipped": t.get("skipped", False),
                    "skip_reason": t.get("skip_reason", ""),
                    "summary_injected": t.get("summary_injected", False),
                    "stream_atoms_extracted": t.get("stream_atoms_extracted", 0),
                    "injection_method": t.get("injection_method", ""),
                })

            stats = await trace_store.get_statistics()

            return self.utils.ok({
                "traces": summary_list,
                "stats": stats,
            })
        except Exception as exc:
            logger.error(f"[PageAPI] 获取 Trace 列表失败: {exc}", exc_info=True)
            return self.utils.error(str(exc))

    async def get_trace_detail(self, plugin) -> dict[str, Any]:
        """获取单条 Trace 的完整详情

        Query params:
            trace_id: Trace ID（必需）
        """
        try:
            trace_store = self._get_trace_store(plugin)
            if trace_store is None:
                return self.utils.error("Trace store not initialized")

            query = request.args
            trace_id = str(query.get("trace_id", "")).strip()
            if not trace_id:
                return self.utils.error("trace_id is required")

            trace_data = await trace_store.get_trace(trace_id)
            if trace_data is None:
                return self.utils.error("Trace not found: " + trace_id)

            return self.utils.ok(trace_data)
        except Exception as exc:
            logger.error(f"[PageAPI] 获取 Trace 详情失败: {exc}", exc_info=True)
            return self.utils.error(str(exc))
