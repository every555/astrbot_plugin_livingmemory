"""家庭角色分工（Phase 3）：family_roles 表 + 22 位家人登记 + 家庭图谱。

每个工具模块都是「家人」，有名字、角色隐喻、职责和协作对象。
家庭图谱由 cooperates_with 协作关系推导（后续可接入 ontology 实体图谱）。
铁律：归档/角色登记都只是「身份簿」，永不删除——active=0 表示退居二线。
"""

import json
import os
import sqlite3
import time

logger = None
try:
    from astrbot.core import logger as _logger

    logger = _logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("family_role")


class FamilyRoleManager:
    """角色分工管理器：登记家人身份、职责、协作对象，生成家庭图谱。"""

    # ── 22 位家人种子数据（角色隐喻来自蓝图 §1/§3）──
    SEED_ROLES: list[dict] = [
        # ── 八件套 ──
        {"member_name": "回忆盒", "tool_name": "recall", "role": "最早的家人",
         "duty": "精准召回过去的共同经历", "importance": 1.0,
         "cooperates_with": ["trace", "today", "search"]},
        {"member_name": "日记本", "tool_name": "today", "role": "写日记的孩子",
         "duty": "记录并汇报今天的共同经历", "importance": 0.8,
         "cooperates_with": ["recall", "search"]},
        {"member_name": "搜书人", "tool_name": "search", "role": "图书管理员",
         "duty": "跨时间段广泛搜索记忆", "importance": 0.7,
         "cooperates_with": ["recall", "today"]},
        {"member_name": "温度计", "tool_name": "sentiment", "role": "体温计",
         "duty": "感知感情温度趋势（A5 反哺画像）", "importance": 0.7,
         "cooperates_with": ["profile"]},
        {"member_name": "管家", "tool_name": "reminder", "role": "管家",
         "duty": "提醒重要事项，转达预言到期（A6）", "importance": 0.9,
         "cooperates_with": ["prophecy"]},
        {"member_name": "溯源师", "tool_name": "trace", "role": "族谱考据员",
         "duty": "追溯记忆来源链（L3→L2→L1）", "importance": 0.6,
         "cooperates_with": ["recall", "causal_chain"]},
        {"member_name": "复习老师", "tool_name": "reinforce", "role": "课后辅导老师",
         "duty": "强化到期记忆的复习", "importance": 0.8,
         "cooperates_with": ["knowledge", "profile"]},
        {"member_name": "老教师", "tool_name": "knowledge", "role": "老教师",
         "duty": "把经验沉淀为知识，供全家复用", "importance": 0.9,
         "cooperates_with": ["error_learner", "conflict_check"]},
        # ── 六件套（ontology）──
        {"member_name": "关系簿", "tool_name": "ontology", "role": "家谱先生",
         "duty": "管理实体关系知识图谱", "importance": 0.7,
         "cooperates_with": ["causal_chain", "profile"]},
        # ── v2 五兄弟 ──
        {"member_name": "族谱先生", "tool_name": "causal_chain", "role": "族谱先生",
         "duty": "梳理记忆的因果证据链（A4 喂预言）", "importance": 0.7,
         "cooperates_with": ["prophecy", "ontology"]},
        {"member_name": "判官", "tool_name": "conflict_check", "role": "判官",
         "duty": "检测并裁定记忆冲突（A2/A8）", "importance": 0.8,
         "cooperates_with": ["prophecy", "knowledge"]},
        {"member_name": "妈妈", "tool_name": "profile", "role": "妈妈",
         "duty": "从记忆提炼稳定画像，全家的圆心", "importance": 1.0,
         "cooperates_with": ["expression", "sentiment", "prophecy"]},
        {"member_name": "爱猜的孩子", "tool_name": "prophecy", "role": "爱猜的孩子",
         "duty": "基于规律预测未来并回溯验证（A1/A6/A7）", "importance": 0.8,
         "cooperates_with": ["profile", "reminder", "recall", "conflict_check"]},
        {"member_name": "化妆师", "tool_name": "expression", "role": "化妆师",
         "duty": "画像驱动表达风格联动（A3）", "importance": 0.6,
         "cooperates_with": ["profile"]},
        # ── 侧写入 ──
        {"member_name": "速记员", "tool_name": "memory_memorize", "role": "速记员",
         "duty": "侧写入记忆（平台无关）", "importance": 0.6,
         "cooperates_with": ["memory_search"]},
        {"member_name": "检索员", "tool_name": "memory_search", "role": "检索员",
         "duty": "侧查询记忆", "importance": 0.6,
         "cooperates_with": ["memory_memorize"]},
        # ── 后台团 ──
        {"member_name": "记错本", "tool_name": "error_learner", "role": "记错本",
         "duty": "记录错误教训，毕业为知识（A9）", "importance": 0.8,
         "cooperates_with": ["knowledge"]},
        {"member_name": "秘书", "tool_name": "reporter", "role": "秘书",
         "duty": "生成家庭日报，汇报各家情况（Phase 4）", "importance": 0.8,
         "cooperates_with": ["family_meeting", "archive"]},
        {"member_name": "档案员", "tool_name": "exporter", "role": "档案员",
         "duty": "导出记忆档案", "importance": 0.5,
         "cooperates_with": ["archive"]},
        {"member_name": "邮差", "tool_name": "external_sync", "role": "邮差",
         "duty": "外部同步记忆", "importance": 0.5,
         "cooperates_with": ["exporter"]},
        {"member_name": "体检医生", "tool_name": "code_checker", "role": "体检医生",
         "duty": "定期体检代码与健康状态", "importance": 0.6,
         "cooperates_with": ["error_learner"]},
        {"member_name": "相册管理员", "tool_name": "archive", "role": "相册管理员",
         "duty": "扫描沉睡记忆→例会汇报→橘子点头→归档（永不删除）", "importance": 0.9,
         "cooperates_with": ["family_meeting", "reporter"]},
    ]

    def __init__(self, v2_db_path: str, main_db_path: str | None = None, store=None):
        self.v2_db_path = v2_db_path
        self.main_db_path = main_db_path
        self.store = store  # V2Store（可选）
        self._v2: sqlite3.Connection | None = None

    # ─────────── 连接管理 ───────────

    def _conn(self) -> sqlite3.Connection:
        if self._v2 is None:
            os.makedirs(os.path.dirname(self.v2_db_path), exist_ok=True)
            self._v2 = sqlite3.connect(self.v2_db_path, timeout=10)
            self._v2.row_factory = sqlite3.Row
            self._v2.execute("PRAGMA busy_timeout=10000")
            self._ensure_table()
        return self._v2

    def _ensure_table(self) -> None:
        """建表（双保险：即使 V2Store 没来得及建，这里也能自建）。"""
        self._v2.execute(
            """
            CREATE TABLE IF NOT EXISTS family_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                member_name TEXT NOT NULL,
                tool_name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,
                duty TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                cooperates_with TEXT DEFAULT '[]',
                active INTEGER DEFAULT 1,
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

    # ─────────── 种子与登记 ───────────

    def ensure_seed(self) -> dict:
        """首次初始化：登记 22 位家人（INSERT OR IGNORE，不覆盖已有）。"""
        conn = self._conn()
        inserted = 0
        now = time.time()
        for r in self.SEED_ROLES:
            cur = conn.execute(
                "SELECT COUNT(*) AS c FROM family_roles WHERE tool_name = ?",
                (r["tool_name"],),
            )
            if cur.fetchone()["c"] > 0:
                continue
            conn.execute(
                """INSERT INTO family_roles
                   (member_name, tool_name, role, duty, importance,
                    cooperates_with, active, created_at)
                   VALUES (?,?,?,?,?,?,1,?)""",
                (
                    r["member_name"],
                    r["tool_name"],
                    r["role"],
                    r["duty"],
                    r["importance"],
                    json.dumps(r["cooperates_with"], ensure_ascii=False),
                    now,
                ),
            )
            inserted += 1
        conn.commit()
        total = conn.execute("SELECT COUNT(*) AS c FROM family_roles").fetchone()["c"]
        return {"status": "ok", "inserted": inserted, "total": total}

    def register_role(
        self,
        member_name: str,
        tool_name: str,
        role: str,
        duty: str = "",
        importance: float = 0.5,
        cooperates_with: list[str] | None = None,
    ) -> dict:
        """登记/更新一位家人（UPSERT）。"""
        conn = self._conn()
        now = time.time()
        conn.execute(
            """INSERT INTO family_roles
               (member_name, tool_name, role, duty, importance, cooperates_with, active, created_at)
               VALUES (?,?,?,?,?,?,1,?)
               ON CONFLICT(tool_name) DO UPDATE SET
                 member_name=excluded.member_name, role=excluded.role,
                 duty=excluded.duty, importance=excluded.importance,
                 cooperates_with=excluded.cooperates_with, active=1""",
            (
                member_name,
                tool_name,
                role,
                duty,
                importance,
                json.dumps(cooperates_with or [], ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        return {"status": "ok", "tool_name": tool_name, "member_name": member_name}

    def deactivate(self, tool_name: str) -> dict:
        """退居二线（active=0，不删除）。"""
        conn = self._conn()
        cur = conn.execute(
            "UPDATE family_roles SET active=0 WHERE tool_name=?", (tool_name,)
        )
        conn.commit()
        return {"status": "ok", "tool_name": tool_name, "changed": cur.rowcount}

    # ─────────── 查询 ───────────

    def list_roles(self, active_only: bool = True) -> list[dict]:
        conn = self._conn()
        q = "SELECT * FROM family_roles"
        if active_only:
            q += " WHERE active = 1"
        q += " ORDER BY importance DESC, id ASC"
        rows = conn.execute(q).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["cooperates_with"] = json.loads(d.get("cooperates_with") or "[]")
            except Exception:
                d["cooperates_with"] = []
            out.append(d)
        return out

    def get_role(self, tool_name: str) -> dict | None:
        conn = self._conn()
        r = conn.execute(
            "SELECT * FROM family_roles WHERE tool_name=?", (tool_name,)
        ).fetchone()
        if r is None:
            return None
        d = dict(r)
        try:
            d["cooperates_with"] = json.loads(d.get("cooperates_with") or "[]")
        except Exception:
            d["cooperates_with"] = []
        return d

    def family_tree(self) -> dict:
        """家庭图谱：nodes（家人）+ edges（协作关系，去重）。"""
        roles = self.list_roles(active_only=True)
        nodes = [
            {
                "id": r["tool_name"],
                "name": r["member_name"],
                "role": r["role"],
                "importance": r["importance"],
            }
            for r in roles
        ]
        seen: set[tuple[str, str]] = set()
        edges = []
        for r in roles:
            for partner in r.get("cooperates_with") or []:
                if partner not in {x["id"] for x in nodes}:
                    continue
                key = tuple(sorted((r["tool_name"], partner)))
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"source": r["tool_name"], "target": partner})
        return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}

    def stats(self) -> dict:
        conn = self._conn()
        total = conn.execute("SELECT COUNT(*) AS c FROM family_roles").fetchone()["c"]
        active = conn.execute(
            "SELECT COUNT(*) AS c FROM family_roles WHERE active=1"
        ).fetchone()["c"]
        tree = self.family_tree()
        return {
            "status": "ok",
            "total": total,
            "active": active,
            "inactive": total - active,
            "cooperation_edges": tree["edge_count"],
        }
