"""家庭例会（Phase 4）：每日汇总全家动态 → 生成日报 → family_reports。

例会流程：
  1. 秘书（reporter）每天定时收集各家今日「流水」：画像更新、预言验证、
     冲突裁定、因果梳理、表达进化、归档动作、反馈互动。
  2. 生成日报写入 family_reports（report_date 唯一，同日覆盖）。
  3. 第一议题永远优先：归档候选审批（archive_pending / archive_candidates）。
  4. 日报可以主动推送给橘子（调度器调用后经 main 层推送）。

家训不变：日报只是台账，不删除任何记忆。
"""

import json
import os
import sqlite3
import time
import datetime

logger = None
try:
    from astrbot.core import logger as _logger

    logger = _logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("family_meeting")


class FamilyMeetingManager:
    """家庭例会管理器：收集全家动态 → 生成/查询日报。"""

    def __init__(self, v2_db_path: str, main_db_path: str | None = None, store=None):
        self.v2_db_path = v2_db_path
        self.main_db_path = main_db_path
        self.store = store  # V2Store（可选）
        self._v2: sqlite3.Connection | None = None

    # ─────────── 连接与建表 ───────────

    def _conn(self) -> sqlite3.Connection:
        if self._v2 is None:
            os.makedirs(os.path.dirname(self.v2_db_path), exist_ok=True)
            self._v2 = sqlite3.connect(self.v2_db_path, timeout=10)
            self._v2.row_factory = sqlite3.Row
            self._v2.execute("PRAGMA busy_timeout=10000")
            self._ensure_table()
        return self._v2

    def _ensure_table(self) -> None:
        self._v2.execute(
            """
            CREATE TABLE IF NOT EXISTS family_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL UNIQUE,
                content TEXT DEFAULT '{}',
                summary TEXT DEFAULT '',
                status TEXT DEFAULT 'issued',
                created_at REAL NOT NULL
            )
            """
        )
        self._v2.commit()

    def close(self) -> None:
        if self._v2 is not None:
            try:
                self._v2.close()
            except Exception:
                pass
            self._v2 = None

    # ─────────── 时间窗口 ───────────

    @staticmethod
    def _day_window(date_str: str | None = None) -> tuple[float, float, str]:
        """返回指定日期（本地时区）的 [start, end) epoch 窗口与日期串。"""
        if not date_str:
            date_str = datetime.date.today().isoformat()
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        start = time.mktime(dt.timetuple())
        return start, start + 86400, date_str

    # ─────────── 数据收集 ───────────

    def collect_daily_stats(self, date_str: str | None = None) -> dict:
        """收集全家今日「流水」（各表按 created_at/updated_at 时间戳统计）。"""
        start, end, ds = self._day_window(date_str)
        conn = self._conn()
        c = conn.execute

        def cnt(sql: str, *args) -> int:
            try:
                # v6.9 修复：execute(sql, params) 只收一个参数序列，
                # 原来的 c(sql, *args) 平铺传参抛 TypeError 被 except 吞成 0——
                # 例会"今日N次/新增N条"自上线起全线静默失明的根因（2026-08-20 橘子问出）
                return int(c(sql, args).fetchone()[0])
            except Exception:
                return 0

        stats: dict = {}
        # 妈妈：画像更新
        stats["profile_updated"] = cnt(
            "SELECT COUNT(*) FROM memory_profile WHERE updated_at>=? AND updated_at<?",
            start, end,
        )
        # 爱猜的孩子：预言创建/验证/失败
        stats["prophecy_created"] = cnt(
            "SELECT COUNT(*) FROM memory_prophecies WHERE created_at>=? AND created_at<?",
            start, end,
        )
        stats["prophecy_verified"] = cnt(
            "SELECT COUNT(*) FROM memory_prophecies WHERE verified_at>=? AND verified_at<? AND status='verified'",
            start, end,
        )
        stats["prophecy_failed"] = cnt(
            "SELECT COUNT(*) FROM memory_prophecies WHERE verified_at>=? AND verified_at<? AND status='failed'",
            start, end,
        )
        stats["prophecy_active"] = cnt(
            "SELECT COUNT(*) FROM memory_prophecies WHERE status='active'"
        )
        # 判官：冲突新增/已解决/待处理
        stats["conflicts_created"] = cnt(
            "SELECT COUNT(*) FROM memory_conflicts WHERE created_at>=? AND created_at<?",
            start, end,
        )
        stats["conflicts_resolved"] = cnt(
            "SELECT COUNT(*) FROM memory_conflicts WHERE resolved_at>=? AND resolved_at<?",
            start, end,
        )
        stats["conflicts_pending"] = cnt(
            "SELECT COUNT(*) FROM memory_conflicts WHERE status='candidate'"
        )
        # 族谱先生：因果链新增
        stats["causality_linked"] = cnt(
            "SELECT COUNT(*) FROM memory_causality WHERE created_at>=? AND created_at<?",
            start, end,
        )
        # 化妆师：表达进化
        stats["expression_logged"] = cnt(
            "SELECT COUNT(*) FROM memory_expression_log WHERE updated_at>=? AND updated_at<?",
            start, end,
        )
        # 相册管理员：今日候选 / 今日归档 / 待审批
        stats["archive_candidates_new"] = cnt(
            "SELECT COUNT(*) FROM archive_candidates WHERE created_at>=? AND created_at<?",
            start, end,
        )
        stats["archive_archived"] = cnt(
            "SELECT COUNT(*) FROM archive_candidates WHERE archived_at>=? AND archived_at<?",
            start, end,
        )
        stats["archive_pending"] = cnt(
            "SELECT COUNT(*) FROM archive_candidates WHERE status IN ('candidate','proposed','confirmed')"
        )
        # 反馈回路：今日互动 / 累计互动
        stats["feedback_events"] = cnt(
            "SELECT COUNT(*) FROM feedback_log WHERE created_at>=? AND created_at<?",
            start, end,
        )
        stats["feedback_total"] = cnt("SELECT COUNT(*) FROM feedback_log")
        # 家庭：活跃家人 / 协作关系
        stats["family_active"] = cnt(
            "SELECT COUNT(*) FROM family_roles WHERE active=1"
        )
        stats["cooperation_edges"] = cnt(
            "SELECT COUNT(*) FROM family_roles WHERE active=1 AND cooperates_with != '[]'"
        )
        stats["report_date"] = ds
        return stats

    # ─────────── 日报生成 ───────────

    def generate_report(self, date_str: str | None = None) -> dict:
        """生成/更新当日例会日报（同日覆盖，幂等）。"""
        stats = self.collect_daily_stats(date_str)
        # P0-3: 挂载梦境报告（Auto-Dream 例会整合，缺失/过期则 None）
        stats["dream_report"] = self._load_dream_report()
        ds = stats["report_date"]
        summary = self._build_summary(stats)
        conn = self._conn()
        now = time.time()
        conn.execute(
            """INSERT INTO family_reports (report_date, content, summary, status, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(report_date) DO UPDATE SET
                 content=excluded.content, summary=excluded.summary,
                 status='issued', created_at=excluded.created_at""",
            (ds, json.dumps(stats, ensure_ascii=False), summary, "issued", now),
        )
        conn.commit()
        report = self.get_report(ds, refresh=False)
        return {"status": "ok", "report_date": ds, "report": report}

    def _load_dream_report(self, max_age_hours: float = 26.0):
        """P0-3: 读 helper 侧 DreamEngine 落盘的梦境报告（跨插件读文件，任何异常降级 None）。"""
        import time as _time
        try:
            path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))),
                "plugin_data", "astrbot_plugin_livingmemory_helper", "dream_report.json",
            )
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as fh:
                rep = json.load(fh)
            if _time.time() - float(rep.get("saved_at", 0)) > max_age_hours * 3600:
                return None
            return rep
        except Exception:
            return None

    def _build_summary(self, s: dict) -> str:
        """把今日流水转成一句句人话（秘书口吻）。"""
        lines = [
            f"今日例会（{s['report_date']}）：全家 {s['family_active']} 位家人在线，"
            f"累计 {s['feedback_total']} 次互动，今日 {s['feedback_events']} 次。"
        ]
        if s["profile_updated"]:
            lines.append(f"· 妈妈（画像）更新 {s['profile_updated']} 条特征")
        if s["prophecy_created"] or s["prophecy_verified"] or s["prophecy_failed"]:
            lines.append(
                f"· 爱猜的孩子（预言）新增 {s['prophecy_created']} 条，"
                f"验证 {s['prophecy_verified']} 条，失败 {s['prophecy_failed']} 条，"
                f"生效中 {s['prophecy_active']} 条"
            )
        if s["conflicts_created"] or s["conflicts_pending"]:
            lines.append(
                f"· 判官（冲突）今日新增 {s['conflicts_created']} 条，"
                f"待处理 {s['conflicts_pending']} 条"
            )
        if s["causality_linked"]:
            lines.append(f"· 族谱先生（因果链）今日梳理 {s['causality_linked']} 条")
        if s["expression_logged"]:
            lines.append(f"· 化妆师（表达）今日进化 {s['expression_logged']} 次")
        if s["archive_archived"] or s["archive_pending"]:
            lines.append(
                f"· 相册管理员（归档）今日归档 {s['archive_archived']} 条，"
                f"待审批 {s['archive_pending']} 条"
            )
        if s["cooperation_edges"]:
            lines.append(f"· 全家协作关系 {s['cooperation_edges']} 条")
        # P0-3: 梦境议题（Auto-Dream 例会整合）
        drep = s.get("dream_report")
        if drep:
            mode = drep.get("mode", "real")
            if mode == "real":
                lines.append(
                    f"· 做梦的孩子（梦境）上次清洗归并 {drep.get('consolidated', 0)} 条、"
                    f"修剪 {drep.get('pruned', 0)} 条，冲突巡检发现 {drep.get('conflicts_found', 0)} 组"
                )
            else:
                t = drep.get("totals", {})
                lines.append(
                    f"· 做梦的孩子（梦境dry_run预览）待归并 {t.get('merge_candidates', 0)} 对、"
                    f"待修剪 {t.get('prune_candidates', 0)} 条、疑似冲突 {t.get('conflict_candidates', 0)} 组"
                )
            if drep.get("conflicts_found") or drep.get("totals", {}).get("conflict_candidates"):
                lines.append("⚠️ 第二议题：梦境巡检发现疑似记忆冲突，请橘子过目（conflict_check 查看）")

        # 第一议题：归档审批
        if s["archive_pending"]:
            lines.append(f"⚠️ 第一议题：有 {s['archive_pending']} 条归档候选待橘子审批！")
        else:
            lines.append("✔ 第一议题：归档候选已清空，无待审批。")
        return "\n".join(lines)

    # ─────────── 查询 ───────────

    def get_report(self, date_str: str | None = None, refresh: bool = True) -> dict | None:
        # v6.9 活页日报：今日日报读时自动刷新，白天互动即时反映（修复"今日0次"快照陈旧）
        if refresh and not date_str:
            date_str = datetime.date.today().isoformat()
        if refresh and date_str == datetime.date.today().isoformat():
            try:
                return self.generate_report(date_str)["report"]
            except Exception:
                pass
        conn = self._conn()
        if not date_str:
            date_str = datetime.date.today().isoformat()
        r = conn.execute(
            "SELECT * FROM family_reports WHERE report_date=?", (date_str,)
        ).fetchone()
        if r is None:
            return None
        d = dict(r)
        try:
            d["content"] = json.loads(d.get("content") or "{}")
        except Exception:
            d["content"] = {}
        return d

    def list_reports(self, limit: int = 7) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT report_date, summary, status, created_at FROM family_reports "
            "ORDER BY report_date DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) FROM family_reports").fetchone()[0]
        latest = conn.execute(
            "SELECT report_date, created_at FROM family_reports ORDER BY report_date DESC LIMIT 1"
        ).fetchone()
        return {
            "status": "ok",
            "reports_total": total,
            "latest": dict(latest) if latest else None,
        }
