"""
Fact Superseder 事实替代引擎 (2026-08-31)

背景: 8-20密码更新后, 旧密码明文残留在20条记忆中, 检索时18:1淹没新密码,
      导致旧密码被反复脱口(三犯)。时间衰减治不了"版本替代"型过期——
      高importance护盾反而保护了错误旧版。本引擎补上这条神经:
      新记忆入库若带"版本替代"信号, 自动将同域旧档案打标 superseded。

安全阀:
  1. 只打标不删除, status改回active即可回滚
  2. 教训/叙事类记忆豁免(它们含域词但不是事实声明)
  3. fire-and-forget, 任何异常不影响入库主链路
"""

from astrbot.api import logger

# 版本替代信号: 新记忆内容命中任一才进入域分析
SIGNALS = [
    "新密码", "改为", "改成", "已改", "作废", "换成", "更换",
    "覆盖旧档", "更新为", "不再用", "已失效", "旧密码",
    "已停用", "换成了", "新的密码", "已更换",
]

# 易变事实域: 内容中出现的域词, 用于圈定同域旧记忆
DOMAINS = [
    "密码", "password", "IP", "地址", "账号", "端口",
    "手机号", "手机", "排期", "档期", "版本号",
]

# 叙事豁免词: 旧记忆文本含任一则跳过(教训/事件/工程记录不是事实档案)
EXEMPT_TEXT = [
    "教训", "错题", "抓包", "二犯", "三犯", "省察", "会话",
    "陪伴", "工程", "竣工", "交付", "聊天", "存档", "交接",
]
EXEMPT_TOPIC = ["教训", "错题", "省察", "会话", "陪伴", "记录"]


async def check_and_supersede(engine, new_doc_id: int, content: str, metadata: dict) -> int:
    """新记忆入库后的版本替代检测. 返回被打标的旧记忆数."""
    try:
        if not any(s in content for s in SIGNALS):
            return 0
        domains = [d for d in DOMAINS if d.lower() in content.lower()]
        if not domains:
            return 0
        db = engine.db_connection
        if db is None:
            return 0
        import json as _json
        import time as _time
        like_conds = " OR ".join(["text LIKE ?"] * len(domains))
        params = [f"%{d}%" for d in domains]
        sql = (
            f"SELECT id, text, metadata FROM documents "
            f"WHERE ({like_conds}) AND id != ? "
            f"AND json_extract(metadata, '$.status') IS NULL "
            f"AND created_at < (SELECT created_at FROM documents WHERE id = ?)"
        )
        cursor = await db.execute(sql, (*params, new_doc_id, new_doc_id))
        rows = await cursor.fetchall()
        count = 0
        now = _time.strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            try:
                old_text = row[1] or ""
                if any(x in old_text for x in EXEMPT_TEXT):
                    continue
                md = _json.loads(row[2]) if row[2] else {}
                if not isinstance(md, dict):
                    continue
                topics = str(md.get("topics", ""))
                if any(x in topics for x in EXEMPT_TOPIC):
                    continue
                md["status"] = "superseded"
                md["superseded_by"] = new_doc_id
                md["superseded_at"] = now
                md["importance"] = min(float(md.get("importance", 0.5)), 0.2)
                await db.execute(
                    "UPDATE documents SET metadata = ? WHERE id = ?",
                    (_json.dumps(md, ensure_ascii=False), row[0]),
                )
                count += 1
                logger.info(
                    f"[FactSuperseder] doc#{row[0]} 被 doc#{new_doc_id} 替代 (域: {domains})"
                )
            except Exception as row_err:
                logger.warning(f"[FactSuperseder] 行处理异常 doc#{row[0]}: {row_err}")
                continue
        if count > 0:
            await db.commit()
            logger.info(f"[FactSuperseder] 版本替代完成: {count} 条旧记忆已打标")
        return count
    except Exception as e:
        logger.warning(f"[FactSuperseder] 判定异常(不影响入库): {e}")
        return 0
