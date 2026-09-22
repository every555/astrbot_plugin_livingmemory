"""P0-2 传递闭包一致性检查器（免疫器官核心）。

药理：新记忆入库 -> 抽三元组 -> 沿 ontology 图 BFS(<=max_hops) 推导 -> 推导事实 vs 新记忆否定 -> 冲突候选附证据链。
设计铁律：
- 零依赖纯模块（只 import 标准库），不 import livingmemory 任何代码，不 import helper。
- 跨库只读：直接 sqlite 只读连接 helper 的 ontology.db，绝不写入。
- 免疫降级：任何异常返回空列表，绝不阻断记忆入库（免疫器官不能害死主人）。
- 推导规则白名单：R1 is_a 传递 / R2 is_a+has 继承，不做其他联想。
"""
import json
import os
import re
import sqlite3

NAME_KEYS = ("name", "title", "member_name")
REL_CN = {"is_a": "是", "has": "有"}
DEFAULT_MAX_HOPS = 3
DEFAULT_MAX_CANDIDATES = 3


def _load_graph(db_path: str):
    """只读加载 ontology 图。返回 (names: {id:name}, adj: {id:[(rel,to_id)]}, name2id) 或抛异常由上层降级。"""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = {}
        for eid, props_raw in con.execute("SELECT id, properties FROM entities").fetchall():
            try:
                props = json.loads(props_raw or "{}")
            except Exception:
                props = {}
            name = ""
            for key in NAME_KEYS:
                v = str(props.get(key) or "").strip()
                if v:
                    name = v
                    break
            if not name:
                name = eid
            names[eid] = name
        adj = {}
        for f, rel, t in con.execute("SELECT from_id, relation_type, to_id FROM relations").fetchall():
            adj.setdefault(f, []).append((rel, t))
        name2id = {n: i for i, n in names.items() if n != i}
        return names, adj, name2id
    finally:
        con.close()


def extract_triples(content: str, entity_names) -> list:
    """从文本抽 (实体A, 关系, 实体B, negated)。朴素法：图中实体名两两正则，否定式优先。"""
    hits = []
    names_in_text = [n for n in entity_names if n and n in content]
    for a in names_in_text:
        for b in names_in_text:
            if b == a:
                continue
            pat_neg = re.escape(a) + r".{0,10}?(没有|无|不是|不属于)" + r".{0,6}?" + re.escape(b)
            m = re.search(pat_neg, content)
            if m:
                rel = "is_a" if m.group(1) in ("不是", "不属于") else "has"
                hits.append((a, rel, b, True))
                continue
            pat_pos = re.escape(a) + r".{0,10}?(是|属于|一种|有|拥有)" + r".{0,6}?" + re.escape(b)
            m = re.search(pat_pos, content)
            if m:
                rel = "is_a" if m.group(1) in ("是", "属于", "一种") else "has"
                hits.append((a, rel, b, False))
    return hits


def derive_facts(start_id: str, adj: dict, max_hops: int = DEFAULT_MAX_HOPS) -> list:
    """BFS 推导（规则白名单）。链上实体去重断环。返回 [{rel, target, chain:[(from,rel,to)]}]。

    R1 is_a 传递: X-是>Y-是>Z => X 是 Z
    R2 has 继承: X-是>Y-有>Z => X 有 Z
    链长>=2 才算推导结论（1跳是图上原句，不算新事实）；has 只能做末段。
    """
    results = {}
    queue = [(start_id, [], {start_id})]
    while queue:
        cur, chain, visited = queue.pop(0)
        if len(chain) >= max_hops:
            continue
        for rel, nxt in adj.get(cur, []):
            if nxt in visited:
                continue
            new_chain = chain + [(cur, rel, nxt)]
            if rel == "is_a":
                if len(new_chain) >= 2:
                    key = ("is_a", nxt)
                    if key not in results or len(new_chain) < len(results[key]["chain"]):
                        results[key] = {"rel": "is_a", "target": nxt, "chain": new_chain}
                queue.append((nxt, new_chain, visited | {nxt}))
            elif rel == "has":
                if len(new_chain) >= 2:
                    key = ("has", nxt)
                    if key not in results or len(new_chain) < len(results[key]["chain"]):
                        results[key] = {"rel": "has", "target": nxt, "chain": new_chain}
    return list(results.values())


def check_transitive(new_content: str, db_path: str, max_hops: int = DEFAULT_MAX_HOPS, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> list:
    """主入口：返回传递冲突候选 [{reason, confidence, evidence_chain, rel}]，异常一律降级为空列表。"""
    try:
        if not new_content or len(str(new_content).strip()) < 4:
            return []
        if not db_path or not str(db_path).endswith(".db"):
            return []
        names, adj, name2id = _load_graph(db_path)
        triples = extract_triples(new_content, name2id.keys())
        out = []
        seen = set()
        for a, rel, b, negated in triples:
            if not negated:
                continue
            for fact in derive_facts(name2id[a], adj, max_hops):
                if fact["rel"] == rel and fact["target"] == name2id[b]:
                    key = (a, rel, b)
                    if key in seen:
                        continue
                    seen.add(key)
                    chain_desc = [
                        f"{names[f]} {REL_CN.get(r, r)} {names[t]}(第{i + 1}跳)"
                        for i, (f, r, t) in enumerate(fact["chain"])
                    ]
                    neg_cn = "不" + REL_CN[rel] if rel == "is_a" else "没有"
                    reason = (" + ".join(chain_desc) +
                              f" => 推导「{a} {REL_CN[rel]} {b}」 vs 新记忆「{a}{neg_cn}{b}」")
                    out.append({"reason": reason, "confidence": 0.75, "evidence_chain": chain_desc, "rel": rel})
                    if len(out) >= max_candidates:
                        return out
        return out
    except Exception:
        return []


def ontology_db_path() -> str:
    """推断 helper 插件的 ontology.db 绝对路径（相对本文件 4 级上跳到 data/）。不存在返回空串=免疫降级。"""
    try:
        p = os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir, os.pardir,
                         "plugin_data", "astrbot_plugin_livingmemory_helper", "ontology.db"))
        return p if os.path.isfile(p) else ""
    except Exception:
        return ""


def sakura_bus_path() -> str:
    """SakuraBus 事件账本路径（superpowers 侧）。不存在返回空串。"""
    try:
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir, os.pardir,
                         "plugins", "astrbot_plugin_superpowers", "data", "sakura_bus", "events.jsonl"))
    except Exception:
        return ""


def sakura_heartbeat(event_type: str, payload: dict) -> bool:
    """免疫事件上 SakuraBus（文件追加·零import·失败静默）。"""
    try:
        bus_file = sakura_bus_path()
        if not bus_file or not os.path.isfile(bus_file):
            return False
        from datetime import datetime
        evt = {"ts": datetime.now().isoformat(timespec="seconds"), "type": event_type,
               "organ": "immune", "payload": payload, "reflex": []}
        with open(bus_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(evt, ensure_ascii=False) + chr(10))
        return True
    except Exception:
        return False
