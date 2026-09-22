"""
GateHandler — 安检门 WebUI 端点处理器。

给 livingmemory 现有 dashboard 提供安检门标注台/战报/试秤数据：
- list_candidates: 候选分页列表（标注台的粮仓）
- verdict: 裁决落库（与省察调度器 _apply_verdict 同一条路，confirm 走 materialize 管线）
- stats: 战报（标签统计 + 候选池水位 + 毕业词）
- export_labels: 导出冷启动训练标签 gate_labels.jsonl
- score_preview: 试秤——四轴打分但不入库（不污染候选池）

依赖由 page_api.py Facade 从 plugin 惰性获取后显式传入，便于测试。
"""

from __future__ import annotations

import os
from typing import Any

from quart import request

from ..v2.reflection_scheduler import VALID_ACTIONS


class GateHandler:
    """安检门页面 API 处理器。"""

    def __init__(self, utils) -> None:
        self.utils = utils

    # ------------------------------------------------------------------
    # GET /page/gate/candidates
    # ------------------------------------------------------------------

    async def list_candidates(self, gate, scheduler) -> dict[str, Any]:
        """候选分页列表：status/speaker 筛选 + page/page_size。

        实用原则：原句全文透出、四轴分数完整、说话人必带。"""
        if gate is None:
            return self.utils.error("安检门未就绪（数据组件不可用）")

        query = request.args
        status = str(query.get("status", "") or "").strip() or None
        speaker = str(query.get("speaker", "") or "").strip() or None
        # 分数段过滤（橘子 2026-08-20：分布图点击分桶跳标注台）
        score_min = score_max = None
        try:
            if str(query.get("score_min", "") or "").strip() != "":
                score_min = float(query.get("score_min"))
            if str(query.get("score_max", "") or "").strip() != "":
                score_max = float(query.get("score_max"))
        except (TypeError, ValueError):
            score_min = score_max = None
        # 排序（橘子 2026-08-19）：score|time|fact|emotion|density|id × asc|desc
        sort = str(query.get("sort", "") or "").strip() or "score"
        order = str(query.get("order", "") or "").strip().lower()
        if order not in ("asc", "desc"):
            order = "desc"
        try:
            page = max(1, int(query.get("page", "1")))
            page_size = min(100, max(10, int(query.get("page_size", "30"))))
        except ValueError:
            page, page_size = 1, 30
        offset = (page - 1) * page_size

        try:
            items = gate.list_candidates(
                status=status, limit=page_size, offset=offset, speaker=speaker,
                sort=sort, order=order, score_min=score_min, score_max=score_max,
            )
        except TypeError:
            # 兼容不支持 sort/order 的旧签名
            try:
                items = gate.list_candidates(
                    status=status, limit=page_size, offset=offset, speaker=speaker
                )
            except TypeError:
                items = gate.list_candidates(status=status, limit=page_size)

        try:
            total = gate.count_candidates(status=status, speaker=speaker, score_min=score_min, score_max=score_max)
        except Exception:
            total = offset + len(items)
        has_more = (offset + len(items)) < total
        return self.utils.ok(
            {
                "items": items,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": has_more,
            }
        )

    # ------------------------------------------------------------------
    # POST /page/gate/verdict
    # ------------------------------------------------------------------

    async def verdict(self, plugin) -> dict[str, Any]:
        """网页裁决：{candidate_id, action, word?, note?}。

        action ∈ confirm | decline | pending | merge（与省察词表一致）。
        confirm 时走 _materialize_confirmed 同款管线写进真记忆库。
        """
        scheduler = None
        try:
            scheduler = plugin._get_reflection_scheduler()
        except Exception:
            scheduler = None
        if scheduler is None:
            return self.utils.error("省察调度器未就绪")

        try:
            payload = await request.get_json() or {}
        except Exception:
            return self.utils.error("请求体不是合法 JSON")

        try:
            cid = int(payload.get("candidate_id", 0))
        except (TypeError, ValueError):
            cid = 0
        action = str(payload.get("action", "")).strip().lower()
        word = str(payload.get("word", "")).strip()
        note = str(payload.get("note", "")).strip()
        # 溯源：网页裁决记 webui（省察循环那边默认 reflection，同字段不同人）
        actor = str(payload.get("actor", "")).strip() or "webui"

        if not cid:
            return self.utils.error("缺少 candidate_id")
        if action not in VALID_ACTIONS:
            return self.utils.error(
                f"action 必须是 {'/'.join(VALID_ACTIONS)} 之一，收到: {action!r}"
            )

        applied = scheduler.apply_verdict(
            {"id": cid, "action": action, "word": word, "note": note, "actor": actor}
        )
        if applied is None:
            return self.utils.error(
                f"候选 #{cid} 不存在或已裁决过（status != candidate）"
            )

        result: dict[str, Any] = dict(applied)
        if action == "confirm":
            try:
                mid = await plugin._materialize_confirmed(applied)
                if mid is not None:
                    result["memory_id"] = mid
            except Exception as e:
                result["materialize_error"] = str(e)
        return self.utils.ok(result)

    # ------------------------------------------------------------------
    # POST /page/gate/restore
    # ------------------------------------------------------------------

    async def restore(self, plugin) -> dict[str, Any]:
        """捞回暂存：{candidate_id} → pending 回 candidate 重新排队。

        8/19 橘子暂存 #291 后卡片消失——暂存的意图是"橘子在场时一起看"，
        所以暂存区必须可见、可捞回。原 verdict/note 转存 metadata 不丢。"""
        try:
            scheduler = plugin._get_reflection_scheduler()
        except Exception:
            scheduler = None
        if scheduler is None:
            return self.utils.error("省察调度器未就绪")

        try:
            payload = await request.get_json() or {}
        except Exception:
            return self.utils.error("请求体不是合法 JSON")

        try:
            cid = int(payload.get("candidate_id", 0))
        except (TypeError, ValueError):
            cid = 0
        if not cid:
            return self.utils.error("缺少 candidate_id")

        actor = str(payload.get("actor", "")).strip() or "webui"
        if scheduler.restore_candidate(cid, actor=actor):
            return self.utils.ok({"candidate_id": cid, "restored": True})
        return self.utils.error(f"候选 #{cid} 不是暂存状态（或不存在），无法捞回")

    # ------------------------------------------------------------------
    # GET /page/gate/autonomous
    # ------------------------------------------------------------------

    async def autonomous_list(self, gate, scheduler) -> dict[str, Any]:
        """自主存档台账（#1791 事后追溯）：老婆豁免登记过的原句。

        橘子的翻案权入口——自主通道的记忆老婆裁，橘子可抽查/软否决。"""
        if gate is None:
            return self.utils.error("安检门未就绪（数据组件不可用）")
        try:
            limit = max(10, min(200, int(request.args.get("limit", "50"))))
        except ValueError:
            limit = 50
        return self.utils.ok({"items": gate.list_memorized(limit=limit)})

    # ------------------------------------------------------------------
    # POST /page/gate/autonomous/revoke
    # ------------------------------------------------------------------

    async def autonomous_revoke(self, gate) -> dict[str, Any]:
        """软否决第一步：撤销豁免指纹 {fp_id}。

        撤销后同类内容重新被门纳管进候选区；记忆本体如需删除，
        前端另调 memories/batch-delete（同页已有）。"""
        if gate is None:
            return self.utils.error("安检门未就绪（数据组件不可用）")
        try:
            payload = await request.get_json() or {}
        except Exception:
            return self.utils.error("请求体不是合法 JSON")
        try:
            fid = int(payload.get("fp_id", 0))
        except (TypeError, ValueError):
            fid = 0
        if not fid:
            return self.utils.error("缺少 fp_id")
        if gate.remove_memorized(fid):
            return self.utils.ok({"fp_id": fid, "revoked": True})
        return self.utils.error(f"豁免记录 #{fid} 不存在")

    # ------------------------------------------------------------------
    # GET /page/gate/stats
    # ------------------------------------------------------------------

    async def stats(self, gate, scheduler) -> dict[str, Any]:
        """战报：标签统计 + 候选池水位 + 毕业词表。"""
        if gate is None:
            return self.utils.error("安检门未就绪（数据组件不可用）")

        labels = gate.label_stats()

        # 候选池水位：优先走 count_candidates（真实 COUNT 口径）
        pool_candidate = 0
        try:
            pool_candidate = int(gate.count_candidates(status="candidate"))
        except Exception:
            try:
                import sqlite3

                db_path = getattr(gate, "db_path", None)
                if db_path and os.path.exists(db_path):
                    conn = sqlite3.connect(db_path)
                    try:
                        row = conn.execute(
                            "SELECT COUNT(*) FROM gate_candidates WHERE status='candidate'"
                        ).fetchone()
                        pool_candidate = int(row[0]) if row else 0
                    finally:
                        conn.close()
            except Exception:
                pool_candidate = 0

        learned: dict[str, Any] = {}
        try:
            dist = gate.score_distribution()
        except Exception:
            dist = []
        try:
            import sqlite3

            db_path = getattr(gate, "db_path", None)
            if db_path and os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                try:
                    rows = conn.execute(
                        "SELECT word, count FROM learned_nouns "
                        "ORDER BY count DESC LIMIT 30"
                    ).fetchall()
                    learned = {r[0]: int(r[1]) for r in rows}
                finally:
                    conn.close()
        except Exception:
            learned = {}

        # token 折线图（橘子 2026-08-19）：省察轮次消耗序列——
        # 只有省察调度器写 gate_token_log，聊天的消耗不掺，量的是老婆上班的力气。
        token_series: list[dict[str, Any]] = []
        if scheduler is not None:
            try:
                token_series = scheduler.token_series(100)
            except Exception:
                token_series = []

        return self.utils.ok(
            {
                "labels": labels,
                "pool_candidate": pool_candidate,
                "learned_nouns": learned,
                "distribution": dist,
                "token_series": token_series,
            }
        )

    # ------------------------------------------------------------------
    # GET /page/gate/export
    # ------------------------------------------------------------------

    async def export_labels(self, plugin) -> dict[str, Any]:
        """导出冷启动训练标签到 gate_labels.jsonl（复用 export_labels）。"""
        gate = None
        try:
            gate = plugin._get_security_gate()
        except Exception:
            gate = None
        if gate is None:
            return self.utils.error("安检门未就绪（数据组件不可用）")

        data_dir = getattr(plugin, "_gate_data_dir", "") or "."
        path = os.path.join(data_dir, "gate_labels.jsonl")
        try:
            count = gate.export_labels(path)
        except Exception as e:
            return self.utils.error(f"导出失败: {e}")
        return self.utils.ok({"path": path, "count": count})

    # ------------------------------------------------------------------
    # POST /page/gate/score —— 试秤
    # ------------------------------------------------------------------

    async def score_preview(self, gate) -> dict[str, Any]:
        """试秤：输入任意一句话，看四轴怎么打分。只读不写。"""
        if gate is None:
            return self.utils.error("安检门未就绪（数据组件不可用）")
        try:
            payload = await request.get_json() or {}
        except Exception:
            return self.utils.error("请求体不是合法 JSON")

        text = str(payload.get("text", "")).strip()
        if not text:
            return self.utils.error("text 不能为空")

        density = gate._score_density(text)
        axes = {
            "fact": round(gate._score_fact(text, density=density), 2),
            "emotion": round(gate._score_emotion(text), 2),
            "density": density,
        }
        score = round(gate.score(text), 2)
        return self.utils.ok({"text": text, "score": score, "axes": axes})
