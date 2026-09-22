"""
Context 组装追踪存储层
使用 SQLite 存储 AssemblyTrace 记录，支持最近 N 条常驻 + 自动清理。

v5.4: Context Assembly Trace Store
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiosqlite

from ..models.assembly_trace import AssemblyTrace

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS context_assembly_traces (
    trace_id       TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    timestamp      REAL NOT NULL,
    trace_data     TEXT NOT NULL,
    injected_count INTEGER DEFAULT 0,
    skipped        INTEGER DEFAULT 0,
    query_intent   TEXT DEFAULT 'default',
    emotion        TEXT DEFAULT 'neutral'
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_traces_session_ts
ON context_assembly_traces (session_id, timestamp DESC)
"""

_CREATE_INDEX_RECENT_SQL = """
CREATE INDEX IF NOT EXISTS idx_traces_ts
ON context_assembly_traces (timestamp DESC)
"""


class ContextTraceStore:
    """存储和查询 AssemblyTrace 记录"""

    def __init__(
        self,
        db_path: str,
        max_traces: int = 500,
        cleanup_interval: float = 3600.0,
    ):
        """
        Args:
            db_path: SQLite 数据库路径
            max_traces: 最大保留条数，超出后自动清理最旧的
            cleanup_interval: 自动清理间隔（秒）
        """
        self.db_path = db_path
        self.max_traces = max_traces
        self.cleanup_interval = cleanup_interval
        self._db: aiosqlite.Connection | None = None
        self._last_cleanup: float = 0.0

    async def initialize(self) -> None:
        """初始化数据库表和索引"""
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute(_CREATE_TABLE_SQL)
        await self._db.execute(_CREATE_INDEX_SQL)
        await self._db.execute(_CREATE_INDEX_RECENT_SQL)
        await self._db.commit()
        logger.info(
            f"[ContextTraceStore] 初始化完成: {self.db_path} "
            f"(max_traces={self.max_traces})"
        )

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None

    async def save_trace(self, trace: AssemblyTrace) -> None:
        """保存一条追踪记录"""
        if not self._db:
            return

        try:
            trace_dict = trace.to_dict()
            await self._db.execute(
                """
                INSERT OR REPLACE INTO context_assembly_traces
                (trace_id, session_id, timestamp, trace_data,
                 injected_count, skipped, query_intent, emotion)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace.trace_id,
                    trace.session_id,
                    trace.timestamp,
                    json.dumps(trace_dict, ensure_ascii=False),
                    trace.injected_count,
                    int(trace.skipped),
                    trace.query_intent,
                    trace.emotion_detected,
                ),
            )
            await self._db.commit()

            # 定期清理
            now = time.time()
            if now - self._last_cleanup > self.cleanup_interval:
                await self._cleanup_old_traces()
                self._last_cleanup = now
        except Exception as e:
            logger.error(f"[ContextTraceStore] 保存追踪记录失败: {e}")

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 获取一条记录"""
        if not self._db:
            return None
        try:
            cursor = await self._db.execute(
                "SELECT trace_data FROM context_assembly_traces WHERE trace_id = ?",
                (trace_id,),
            )
            row = await cursor.fetchone()
            if row and row[0]:
                return json.loads(row[0])
        except Exception as e:
            logger.error(f"[ContextTraceStore] 获取追踪记录失败: {e}")
        return None

    async def get_recent_traces(
        self, session_id: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        """获取最近的追踪记录

        Args:
            session_id: 若指定则只返回该会话的记录
            limit: 返回条数
        """
        if not self._db:
            return []
        try:
            if session_id:
                cursor = await self._db.execute(
                    """
                    SELECT trace_data FROM context_assembly_traces
                    WHERE session_id = ?
                    ORDER BY timestamp DESC LIMIT ?
                    """,
                    (session_id, limit),
                )
            else:
                cursor = await self._db.execute(
                    """
                    SELECT trace_data FROM context_assembly_traces
                    ORDER BY timestamp DESC LIMIT ?
                    """,
                    (limit,),
                )
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                if row and row[0]:
                    results.append(json.loads(row[0]))
            return results
        except Exception as e:
            logger.error(f"[ContextTraceStore] 获取最近记录失败: {e}")
            return []

    async def get_last_trace(self, session_id: str | None = None) -> dict[str, Any] | None:
        """获取最近一条追踪记录"""
        traces = await self.get_recent_traces(session_id=session_id, limit=1)
        return traces[0] if traces else None

    async def get_statistics(self) -> dict[str, Any]:
        """获取追踪统计信息"""
        if not self._db:
            return {}
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM context_assembly_traces"
            )
            row = await cursor.fetchone()
            total = row[0] if row else 0

            cursor = await self._db.execute(
                """
                SELECT
                    COUNT(DISTINCT session_id) as sessions,
                    AVG(injected_count) as avg_injected,
                    MAX(injected_count) as max_injected,
                    SUM(skipped) as skipped_count
                FROM context_assembly_traces
                """
            )
            row = await cursor.fetchone()
            return {
                "total_traces": total,
                "unique_sessions": row[0] if row else 0,
                "avg_injected": round(row[1], 2) if row and row[1] is not None else 0,
                "max_injected": row[2] if row else 0,
                "skipped_count": row[3] if row else 0,
            }
        except Exception as e:
            logger.error(f"[ContextTraceStore] 获取统计信息失败: {e}")
            return {}

    async def _cleanup_old_traces(self) -> int:
        """清理超出上限的旧记录"""
        if not self._db:
            return 0
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM context_assembly_traces"
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            if count <= self.max_traces:
                return 0

            excess = count - self.max_traces
            await self._db.execute(
                """
                DELETE FROM context_assembly_traces
                WHERE trace_id IN (
                    SELECT trace_id FROM context_assembly_traces
                    ORDER BY timestamp ASC LIMIT ?
                )
                """,
                (excess,),
            )
            await self._db.commit()
            logger.info(f"[ContextTraceStore] 清理了 {excess} 条旧追踪记录")
            return excess
        except Exception as e:
            logger.error(f"[ContextTraceStore] 清理旧记录失败: {e}")
            return 0


__all__ = ["ContextTraceStore"]
