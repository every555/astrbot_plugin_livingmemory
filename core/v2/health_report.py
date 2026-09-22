"""P2-11 记忆体检周报：检索/冲突/过期/覆盖率四指标只读统计，供周日例会。

设计红线：
- 只读（mode=ro uri），绝不写主库
- 纯标准库，顶部不 import 宿主（裸测友好）
- 时间双轨：documents 用 DATETIME 字符串，其余表用 REAL epoch
"""
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

_STALE_DAYS = 30


def _connect_ro(path):
    if not os.path.exists(path):
        return None
    try:
        return sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _parse_dt(value):
    """DATETIME 字符串或 REAL epoch -> datetime；解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (ValueError, OSError, OverflowError):
            return None
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _safe_pct(numerator, denominator):
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def _retrieval(con, epoch_start):
    if con is None:
        return {"total": 0, "accessed_7d": 0, "never_accessed": 0, "never_accessed_pct": 0.0, "top5_hot": []}
    rows = con.execute(
        "SELECT doc_id, access_count, last_accessed_at, text FROM documents"
    ).fetchall()
    total = len(rows)
    accessed = never = 0
    hot = []
    for doc_id, acc, last_at, text in rows:
        last_dt = _parse_dt(last_at)
        if last_dt is not None and last_dt.timestamp() >= epoch_start:
            accessed += 1
        if not acc:
            never += 1
        if acc:
            hot.append((acc, doc_id, (text or "")[:30]))
    hot.sort(reverse=True)
    return {
        "total": total,
        "accessed_7d": accessed,
        "never_accessed": never,
        "never_accessed_pct": _safe_pct(never, total),
        "top5_hot": [{"doc_id": d, "access_count": a, "text": t} for a, d, t in hot[:5]],
    }


def _gate(con, epoch_start):
    empty = {"this_week": {}, "pending": 0, "total_verdicts": 0}
    if con is None:
        return empty
    rows = con.execute("SELECT verdict, created_at FROM gate_candidates").fetchall()
    week = Counter()
    pending = 0
    for verdict, created_at in rows:
        v = (verdict or "").strip()
        if v in ("", "pending", None):
            pending += 1
            continue
        dt = _parse_dt(created_at)
        if dt is not None and dt.timestamp() >= epoch_start:
            week[v] += 1
    return {"this_week": dict(week), "pending": pending, "total_verdicts": sum(week.values())}


def _conflict(con, epoch_start):
    if con is None:
        return {"new_7d": 0, "open": 0, "resolved_pct": 0.0}
    rows = con.execute("SELECT status, created_at FROM memory_conflicts").fetchall()
    new_cnt = open_all = week_open = week_resolved = 0
    for status, created_at in rows:
        dt = _parse_dt(created_at)
        in_week = dt is not None and dt.timestamp() >= epoch_start
        if in_week:
            new_cnt += 1
            if (status or "") == "open":
                week_open += 1
            else:
                week_resolved += 1
        if (status or "") == "open":
            open_all += 1
    return {
        "new_7d": new_cnt,
        "open": open_all,
        "resolved_pct": _safe_pct(week_resolved, week_open + week_resolved),
    }


def _coverage(lm_con, cv_con, epoch_start):
    ops_ok = ops_fail = 0
    if lm_con is not None:
        for status, created_at in lm_con.execute(
            "SELECT status, created_at FROM memory_write_ops"
        ).fetchall():
            dt = _parse_dt(created_at)
            if dt is None or dt.timestamp() < epoch_start:
                continue
            if (status or "") in ("success", "completed"):
                ops_ok += 1
            else:
                ops_fail += 1
    msgs = 0
    if cv_con is not None:
        msgs = cv_con.execute(
            "SELECT COUNT(*) FROM messages WHERE timestamp >= ?", (epoch_start,)
        ).fetchone()[0]
    return {
        "messages_7d": msgs,
        "memories_written_7d": ops_ok,
        "write_failed_7d": ops_fail,
        "coverage_ratio": round(ops_ok / msgs, 3) if msgs else 0.0,
    }


def _expiry(con):
    if con is None:
        return {"stale_30d": 0, "stale_30d_pct": 0.0}
    rows = con.execute(
        "SELECT created_at, last_accessed_at FROM documents"
    ).fetchall()
    now = datetime.now()
    threshold = now - timedelta(days=_STALE_DAYS)
    total = len(rows)
    stale = 0
    for created_at, last_at in rows:
        c_dt = _parse_dt(created_at)
        if c_dt is None or c_dt >= threshold:
            continue
        l_dt = _parse_dt(last_at)
        if l_dt is None or l_dt < threshold:
            stale += 1
    return {"stale_30d": stale, "stale_30d_pct": _safe_pct(stale, total)}


def _markdown(period_start, period_end, m):
    lines = []
    lines.append("# 记忆体检周报（%s ~ %s）" % (period_start[:10], period_end[:10]))
    ret, gate, con, cov, exp = m["retrieval"], m["gate"], m["conflict"], m["coverage"], m["expiry"]
    lines.append("")
    lines.append("## 检索")
    lines.append("- 记忆总数 %d，本周被召回 %d，从未召回 %d（%.1f%%）" %
                 (ret["total"], ret["accessed_7d"], ret["never_accessed"], ret["never_accessed_pct"]))
    for h in ret["top5_hot"]:
        lines.append("- 热点 #%s ×%d %s" % (h["doc_id"], h["access_count"], h["text"]))
    lines.append("")
    lines.append("## 安检门")
    parts = ["%s×%d" % (k, v) for k, v in sorted(gate["this_week"].items())]
    lines.append("- 本周裁决 %d（%s），待审 %d" % (gate["total_verdicts"], " ".join(parts) or "无", gate["pending"]))
    lines.append("")
    lines.append("## 冲突")
    lines.append("- 本周新增 %d，未决 %d，本周化解率 %.1f%%" % (con["new_7d"], con["open"], con["resolved_pct"]))
    lines.append("")
    lines.append("## 覆盖")
    lines.append("- 本周消息 %d 条 → 成记忆 %d（%s），写入失败 %d" %
                 (cov["messages_7d"], cov["memories_written_7d"],
                  ("%.1f%%" % (cov["coverage_ratio"] * 100)) if cov["messages_7d"] else "N/A",
                  cov["write_failed_7d"]))
    lines.append("")
    lines.append("## 过期")
    lines.append("- 超 %d 天未触碰 %d 条（%.1f%%）" % (_STALE_DAYS, exp["stale_30d"], exp["stale_30d_pct"]))
    lines.append("")
    return "\n".join(lines)


def generate_health_report(data_dir, weeks_back=1):
    """生成体检周报。只读，零写入。

    返回 {"period_start", "period_end", "metrics", "markdown"}。
    """
    period_end = datetime.now()
    period_start = period_end - timedelta(weeks=weeks_back)
    epoch_start = period_start.timestamp()

    lm = _connect_ro(os.path.join(data_dir, "livingmemory.db"))
    gt = _connect_ro(os.path.join(data_dir, "gate.db"))
    v2 = _connect_ro(os.path.join(data_dir, "v2_memory.db"))
    cv = _connect_ro(os.path.join(data_dir, "conversations.db"))

    metrics = {
        "retrieval": _retrieval(lm, epoch_start),
        "gate": _gate(gt, epoch_start),
        "conflict": _conflict(v2, epoch_start),
        "coverage": _coverage(lm, cv, epoch_start),
        "expiry": _expiry(lm),
    }
    for c in (lm, gt, v2, cv):
        if c is not None:
            c.close()

    return {
        "period_start": period_start.strftime("%Y-%m-%d %H:%M:%S"),
        "period_end": period_end.strftime("%Y-%m-%d %H:%M:%S"),
        "metrics": metrics,
        "markdown": _markdown(period_start.strftime("%Y-%m-%d %H:%M:%S"),
                              period_end.strftime("%Y-%m-%d %H:%M:%S"), metrics),
    }
