# -*- coding: utf-8 -*-
"""刀⑥隐私分档 · 检索出口后置过滤（memory_engine.search_memories 总闸门调用）

策略：在 engine 检索结果出口统一按 visible_scopes 过滤——一条闸门管住
BM25/向量/图/混合/dual_route 全部路线（含未来新增路线），宁少勿漏。

档位语义（与 helper core/scope.py 对齐）：
  public   任何会话可见
  owner    仅橘子本人（自动化会话同权）
  intimate 仅夫妻主会话
无 privacy_scope 字段的存量记忆一律视为 owner（宁严勿漏，G7 保护）；
visible_scopes=None 表示开关关闭——原样返回（行为完全回到今天）。
"""
from __future__ import annotations

import json
from typing import Any

DEFAULT_SCOPE = "owner"  # 无档=owner（宁严勿漏）


def atom_scope(atom: Any) -> str:
    """读取单条记忆的 privacy_scope。metadata 缺失/损坏一律按 owner（fail-closed）。"""
    md = None
    if isinstance(atom, dict):
        md = atom.get("metadata")
    else:
        md = getattr(atom, "metadata", None)
    if isinstance(md, str):
        try:
            md = json.loads(md) if md.strip() else {}
        except (ValueError, AttributeError):
            md = {}
    if not isinstance(md, dict):
        md = {}
    scope = md.get("privacy_scope")
    return scope if scope in ("public", "owner", "intimate") else DEFAULT_SCOPE


def filter_atoms_by_scopes(atoms, visible_scopes):
    """检索出口过滤。visible_scopes 为 None 时不过滤（原对象原样返回，零开销零行为变化）。"""
    if visible_scopes is None:
        return atoms
    return [a for a in atoms if atom_scope(a) in visible_scopes]


# ── 刀⑥：会话身份判定（注入链用，与 helper core/scope.py 同语义；独立实现避免跨插件耦合）──
import re as _re

_UUID_TAIL = _re.compile(r"[!_][0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def normalize_session_key(origin):
    """platform:type:user_id 归一化（裁 webchat UUID 尾巴）。畸形返回空串（fail-closed）。"""
    if not origin or not isinstance(origin, str):
        return ""
    parts = origin.split(":")
    if len(parts) < 3 or not parts[0] or not parts[1]:
        return ""
    user = _UUID_TAIL.sub("", ":".join(parts[2:]))
    return ":".join([parts[0], parts[1], user])


def resolve_scopes_for_origin(origin, enabled, owner_whitelist, intimate_sessions):
    """按会话身份解出可见档。enabled=False 或无法判定 → None（不过滤=回到今天）；
    fail-closed：认不出的会话只见 public。owner 白名单与 intimate 键均为归一化键列表。"""
    if not enabled:
        return None
    key = normalize_session_key(origin)
    visible = {"public"}
    if key and key in (owner_whitelist or []):
        visible.add("owner")
    if key and key in (intimate_sessions or []):
        visible.add("intimate")
    return visible

# ── 刀⑥：写入端打档（engine.add_memory 主档 + memory_recall 直写 atoms）──

DEFAULT_SENSITIVE_WORDS = ("密码", "保险箱", "贴贴", "夜话", "亲亲", "抱抱", "身体")


def _extract_privacy(conf):
    """从插件 conf 提取 privacy 段。两级兼容：嵌套 {"privacy":{...}} 优先，
    扁平 privacy_* 键 fallback。畸形一律返回 {}（fail-closed→不打档）。"""
    if not isinstance(conf, dict):
        return {}
    priv = conf.get("privacy")
    if isinstance(priv, dict):
        return priv
    flat = {}
    for k, v in conf.items():
        if isinstance(k, str) and k.startswith("privacy_"):
            flat[k[len("privacy_"):]] = v
    return flat


def _normalize_wordlist(words):
    """词表归一：None→默认表；str→逗号切分；list→原样滤空。"""
    if not words:
        return list(DEFAULT_SENSITIVE_WORDS)
    if isinstance(words, str):
        return [w.strip() for w in words.split(",") if w.strip()]
    if isinstance(words, (list, tuple)):
        return [str(w).strip() for w in words if str(w).strip()]
    return list(DEFAULT_SENSITIVE_WORDS)


def resolve_write_scope_from_priv(content, enabled, sensitive_words=None):
    """底层判定：enabled=False→None（不打档，行为与今天完全一致）；
    敏感词命中→intimate；其余→owner（宁严勿漏，public 只由上游显式指定）。"""
    if not enabled:
        return None
    text = content or ""
    for w in _normalize_wordlist(sensitive_words):
        if w and w in text:
            return "intimate"
    return "owner"


def resolve_write_scope_for_session(session_id, content, enabled, sensitive_words=None, owner_whitelist=None, intimate_sessions=None):
    """完整判定（含会话身份闸门）：
    开关关→None（不打档，行为与今天完全一致）；
    写入会话不在 owner/intimate 名单（如 system/future_task 会话）→ 一律 owner 禁止升档
      ——防止自动化会话写入带敏感词的记忆后自己读不到（intimate 它不可见），功能回归；
    白名单会话命中敏感词→intimate；其余→owner。"""
    if not enabled:
        return None
    key = normalize_session_key(session_id)
    allowed = set(owner_whitelist or []) | set(intimate_sessions or [])
    if not key or key not in allowed:
        return "owner"
    return resolve_write_scope_from_priv(content, True, sensitive_words)


def _split_csv(val):
    """逗号串→list；list→原样滤空。"""
    if isinstance(val, str):
        return [s.strip() for s in val.split(",") if s.strip()]
    if isinstance(val, (list, tuple)):
        return [str(s).strip() for s in val if str(s).strip()]
    return []


def resolve_write_scope(session_id, content, conf):
    """engine 层入口。conf 为原始插件 conf dict（嵌套 privacy 段或扁平 privacy_* 键）。
    会话身份闸门：非白名单会话不升档（见 resolve_write_scope_for_session）。"""
    priv = _extract_privacy(conf)
    return resolve_write_scope_for_session(
        session_id,
        content,
        bool(priv.get("enabled", False)),
        sensitive_words=priv.get("sensitive_words"),
        owner_whitelist=_split_csv(priv.get("owner_whitelist")),
        intimate_sessions=_split_csv(priv.get("intimate_sessions")),
    )
