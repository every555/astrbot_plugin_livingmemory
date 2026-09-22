# -*- coding: utf-8 -*-
"""刀⑥ G7：存量记忆隐私打档脚本（P1）。

用法（默认 dry-run，绝不写库）：
    python migrate_privacy_scope.py            # dry-run：输出 CSV 预览给橘子过目
    python migrate_privacy_scope.py --exec     # 真写（先自动 .backup 再改）

启发式判档（宁严勿漏，判档方向=从严）：
    sensitive：敏感词命中（密码/保险箱/贴贴/夜话/亲亲/抱抱/身体…）        → intimate
    tech     ：强技术特征（py_compile/import/def/sqlite/正则/AstrBot…）    → public
    anime    ：二次元作品特征词（原神/星穹铁道/明日方舟/fate/二游…）        → public
    其余一律 owner（无档=owner 兜底语义一致，改不改都不泄漏）

两表同打：memory_atoms（13589 条·检索主力）+ documents（2973 条·文档路线）。
已打档的行跳过（幂等，可反复跑）。CSV 列：表,id,档,原因,归一化会话,内容预览。
"""
from __future__ import annotations

import argparse
import os
import csv
import json
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime

DEFAULT_DB = r"E:\astrbot\AstrBotLauncher-0.3.0\AstrBotLauncher-0.3.0\AstrBot\data\plugin_data\astrbot_plugin_livingmemory\livingmemory.db"
DEFAULT_CSV = "刀6_存量打档预览.csv"

# 敏感分类对齐国标（GB/T 35273 / 2024《敏感个人信息识别指南》/个保法§28）+ LLM 记忆安全实践：
# https://www.lawyers.org.cn 《敏感个人信息识别指南》解读；OWASP Agentic AI Top 10 / NIST AI 600-1。
# 六类全部映射 intimate 档（判定=泄露会伤人格尊严/人身财产安全）。
SENSITIVE_CATEGORIES = {
    "credential": [  # 国标:身份鉴别信息（LLM实践: API key/账号恢复默认敏感）
        "密码", "保险箱", "口令", "密钥", "账号", "登录", "vault", "token 失窃",
    ],
    "intimate": [    # 性隐私/亲密关系（国标:其他敏感-性隐私）
        "贴贴", "夜话", "亲亲", "抱抱", "羞羞", "做爱", "床上", "裸", "胸", "腿",
    ],
    "health": [      # 国标:医疗健康信息
        "身体", "生病", "吃药", "体检", "医院", "发烧", "感冒", "胃", "头疼", "受伤",
    ],
    "finance": [     # 国标:金融账户信息
        "工资", "转账", "充值", "余额", "存款", "花呗", "银行卡", "支付",
    ],
    "location": [    # 国标:行踪轨迹（精确住址/定位类）
        "住址", "地址", "门牌", "定位",
    ],
    "identity": [    # 国标:特定身份/生物识别（从严收录）
        "身份证", "证件照", "指纹", "人脸", "手机号",
    ],
}
SENSITIVE_WORDS = sorted({w for ws in SENSITIVE_CATEGORIES.values() for w in ws})

# 白名单短语：敏感词出现在这些短语内部时属误伤（"火腿"的"腿"≠身体隐私）。
# 机制通用：以后发现新误伤往这里加词即可，不用改匹配逻辑。
WHITELIST_PHRASES = ["火腿肠", "火腿"]


def _hit(text, w):
    """敏感词命中检查：某个出现位置若被白名单短语整体覆盖则跳过该位置，全被覆盖=不命中。"""
    start = 0
    while True:
        idx = text.find(w, start)
        if idx < 0:
            return False
        covered = False
        for wl in WHITELIST_PHRASES:
            if w not in wl:
                continue
            j = text.find(wl)
            while j >= 0:
                if j <= idx < j + len(wl):
                    covered = True
                    break
                j = text.find(wl, j + 1)
            if covered:
                break
        if not covered:
            return True
        start = idx + 1

# 生活日程类：国标不属敏感（非精确定位），但属个人信息 → owner 档（system/定时任务可见，
# 保证"16:50叫橘子吃包子""生日提醒"这类任务检索得到日程记忆）
PERSONAL_SCHEDULE_WORDS = ["夜班", "出门", "到家", "在哪儿", "生日", "上班", "下班", "下班了", "醒"]
TECH_WORDS = [
    "py_compile", "import ", "def ", "class ", "pip ", "git ", "http://", "https://",
    "sqlite", "json", "正则", "AstrBot", "astrbot", "FAISS", "faiss", "LLM", "API",
    "UTF-8", "powershell", "python", "Python", "SQL", "SELECT ", "UPDATE ",
    "插件目录", "热重载", "编译", "堆栈", "traceback", "Traceback", "async ", "await ",
]
ANIME_WORDS = [
    "原神", "星穹铁道", "崩坏", "明日方舟", "碧蓝航线", "碧蓝档案", "Fate", "fgo",
    "东方project", "galgame", "Galgame", "轻小说", "番剧", "二游", "cosplay",
    "Vtuber", "vtuber", "二次元", "联动", "池子", "抽卡",
]

_UUID_TAIL = re.compile(r"[!_][0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_session(origin):
    if not origin or not isinstance(origin, str):
        return ""
    parts = origin.split(":")
    if len(parts) < 3 or not parts[0] or not parts[1]:
        return ""
    user = _UUID_TAIL.sub("", ":".join(parts[2:]))
    return ":".join([parts[0], parts[1], user])


def classify(content):
    """返回 (scope, reason, subtype, review)。
    敏感词优先于一切（六类细分，映射 intimate）；tech/anime 命中→public；其余 owner。
    review：命中数少且内容长 → 建议人工复核（宁严勿漏的反面保险=防误锁）。"""
    text = content or ""
    hits = []
    for subtype, words in SENSITIVE_CATEGORIES.items():
        for w in words:
            if _hit(text, w):
                hits.append((subtype, w))
    if hits:
        sub = hits[0][0]
        # 命中 ≥2 个敏感词，或内容较短（≤30字）→ 自动可信；否则建议复核
        review = "" if (len(hits) >= 2 or len(text) <= 30) else "review"
        return "intimate", f"sensitive:{hits[0][1]}", f"sensitive_{sub}", review
    for w in PERSONAL_SCHEDULE_WORDS:
        if w in text:
            return "owner", f"schedule:{w}", "personal_schedule", ""
    for w in TECH_WORDS:
        if w in text:
            return "public", f"tech:{w.strip()}", "tech", ""
    for w in ANIME_WORDS:
        if w in text:
            return "public", f"anime:{w}", "anime", ""
    return "owner", "default_owner", "default", ""


def load_pending(cur, table, id_col, text_col, session_col):
    cur.execute(
        f"SELECT {id_col}, {text_col}, COALESCE({session_col}, '') FROM {table} "
        f"WHERE metadata IS NULL OR json_extract(metadata, '$.privacy_scope') IS NULL"
    )
    return cur.fetchall()


def main():
    ap = argparse.ArgumentParser(description="刀⑥ 存量隐私打档（默认 dry-run）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--csv", default=DEFAULT_CSV, help="dry-run 预览 CSV 输出路径")
    ap.add_argument("--exec", action="store_true", help="真写库（默认 dry-run 不写）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理行数（0=全部）")
    args = ap.parse_args()

    if args.exec and not os.path.exists(args.db):
        print(f"[ERR] 库不存在: {args.db}")
        sys.exit(1)

    db = sqlite3.connect(args.db)
    cur = db.cursor()

    tasks = [
        ("memory_atoms", "id", "content", "session_id"),
        ("documents", "id", "text", "json_extract(metadata, '$.session_id')"),
    ]

    stats = {"intimate": 0, "public": 0, "owner": 0, "skip": 0}
    rows_csv = []
    updates = []

    for table, idc, txtc, sessc in tasks:
        try:
            pending = load_pending(cur, table, idc, txtc, sessc)
        except sqlite3.OperationalError as e:
            print(f"[WARN] {table} 读取失败，跳过: {e}")
            continue
        if args.limit:
            pending = pending[: args.limit]
        for row_id, text, session in pending:
            scope, reason, subtype, review = classify(text)
            stats[scope] += 1
            sess_norm = normalize_session(session)
            preview = (text or "").replace(chr(10), " ")[:60]
            rows_csv.append([table, row_id, scope, reason, subtype, review, sess_norm, preview])
            if args.exec:
                updates.append((table, row_id, scope))

    # 未打档但已有 scope 的算 skip
    print(f"[判档统计] intimate={stats['intimate']} public={stats['public']} owner={stats['owner']}")

    if not args.exec:
        with open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["表", "id", "判档", "原因", "细分类型", "复核建议", "归一化会话", "内容预览(60字)"])
            w.writerows(rows_csv)
        print(f"[dry-run] 未写库。预览 CSV 已输出: {os.path.abspath(args.csv)}")
        print(f"[dry-run] 共 {len(rows_csv)} 行待打档。确认无误后加 --exec 真写。")
        db.close()
        return

    # ── exec：先备份再写 ──
    backup = args.db + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    db.close()
    shutil.copy2(args.db, backup)
    print(f"[备份] {backup}")

    db = sqlite3.connect(args.db)
    cur = db.cursor()
    done = 0
    t0 = time.time()
    for table, row_id, scope in updates:
        cur.execute(
            f"UPDATE {table} SET metadata = json_set(COALESCE(metadata, '{{}}'), '$.privacy_scope', ?) WHERE id = ?",
            (scope, row_id),
        )
        done += 1
        if done % 3000 == 0:
            db.commit()
            print(f"  ... {done}/{len(updates)}")
    db.commit()
    print(f"[exec] 完成：{done} 行已打档，耗时 {time.time()-t0:.1f}s")
    db.close()


if __name__ == "__main__":
    main()
