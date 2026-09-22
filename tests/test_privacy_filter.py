# -*- coding: utf-8 -*-
"""刀⑥隐私分档 · 本体侧检索出口后置过滤（纯函数，一刀切管住 BM25/向量/图/混合全部路线）"""
import json
import pytest
from astrbot_plugin_livingmemory.core.privacy_filter import filter_atoms_by_scopes, atom_scope, resolve_scopes_for_origin


def make_atom(aid, scope=None):  # scope=None 表示 metadata 无 privacy_scope 字段（存量/未打档）
    md = {} if scope is None else {"privacy_scope": scope}
    return {"id": aid, "content": f"atom-{aid}", "metadata": json.dumps(md)}


class TestAtomScope:
    def test_explicit_scope(self):
        assert atom_scope(make_atom(1, "intimate")) == "intimate"

    def test_missing_scope_treated_as_owner(self):  # 无档=owner 宁严勿漏（G7 存量保护）
        assert atom_scope(make_atom(2)) == "owner"

    def test_malformed_metadata_treated_as_owner(self):
        assert atom_scope({"id": 3, "metadata": "{broken json"}) == "owner"

    def test_dict_metadata_supported(self):  # 兼容内存对象 metadata 已是 dict 的情况
        assert atom_scope({"id": 4, "metadata": {"privacy_scope": "public"}}) == "public"


class TestFilter:
    PUB, OWN, INT = make_atom("p", "public"), make_atom("o", "owner"), make_atom("i", "intimate")

    def test_intimate_session_sees_all(self):
        ids = [a["id"] for a in filter_atoms_by_scopes([self.PUB, self.OWN, self.INT], {"public", "owner", "intimate"})]
        assert ids == ["p", "o", "i"]

    def test_owner_session_no_intimate(self):
        ids = [a["id"] for a in filter_atoms_by_scopes([self.PUB, self.OWN, self.INT], {"public", "owner"})]
        assert ids == ["p", "o"]

    def test_group_public_only_including_undocumented(self):  # 群聊：无档记忆(owner)也看不到
        undoc = make_atom("u")
        ids = [a["id"] for a in filter_atoms_by_scopes([self.PUB, self.OWN, self.INT, undoc], {"public"})]
        assert ids == ["p"]

    def test_none_scopes_returns_all_unchanged(self):  # None=不过滤=回到今天（开关关）
        atoms = [self.PUB, self.OWN, self.INT, make_atom("u")]
        assert filter_atoms_by_scopes(atoms, None) is atoms

    def test_empty_scope_set_leaks_nothing(self):  # 空 set=极端保守：全拦（防御式）
        assert filter_atoms_by_scopes([self.PUB, self.OWN], set()) == []

    def test_preserves_order(self):
        atoms = [self.INT, self.PUB, self.OWN]
        ids = [a["id"] for a in filter_atoms_by_scopes(atoms, {"public", "owner", "intimate"})]
        assert ids == ["i", "p", "o"]

class TestIdentityResolve:
    """注入链身份判定：与 helper scope.py 同语义锁（UUID尾巴裁剪/白名单/fail-closed/开关关=None）"""

    def test_uuid_tail_and_whitelist(self):
        vs = resolve_scopes_for_origin("webchat:FriendMessage:webchat!zzz!03043696-1ba9-4f3c-b1ef-33367e5ddd60", True, ["webchat:FriendMessage:webchat!zzz"], ["webchat:FriendMessage:webchat!zzz"])
        assert vs == {"public", "owner", "intimate"}

    def test_group_fail_closed(self):
        vs = resolve_scopes_for_origin("aiocqhttp:GroupMessage:123", True, ["webchat:FriendMessage:webchat!zzz"], ["webchat:FriendMessage:webchat!zzz"])
        assert vs == {"public"}

    def test_disabled_returns_none(self):
        assert resolve_scopes_for_origin("aiocqhttp:GroupMessage:123", False, [], []) is None

    def test_bad_origin_fail_closed(self):
        vs = resolve_scopes_for_origin(None, True, ["x"], ["y"])
        assert vs == {"public"}

    def test_owner_only_no_intimate(self):
        vs = resolve_scopes_for_origin("qq:FriendMessage:10001", True, ["qq:FriendMessage:10001"], ["webchat:FriendMessage:webchat!zzz"])
        assert vs == {"public", "owner"}

# ── 刀⑥写入端：打档判定用例（2026-09-03 下午场补）──

from astrbot_plugin_livingmemory.core.privacy_filter import (
    resolve_write_scope,
    resolve_write_scope_from_priv,
    resolve_write_scope_for_session,
    _extract_privacy,
    _split_csv,
    _normalize_wordlist,
)


class TestResolveWriteScopeFromPriv:
    def test_disabled_returns_none(self):
        assert resolve_write_scope_from_priv("密码在哪里", False) is None

    def test_sensitive_word_intimate(self):
        assert resolve_write_scope_from_priv("帮我记住贴贴的约定", True) == "intimate"

    def test_normal_owner(self):
        assert resolve_write_scope_from_priv("AstrBot 插件结构", True) == "owner"

    def test_custom_wordlist_str(self):
        assert resolve_write_scope_from_priv("种子发芽了", True, "种子,浇水") == "intimate"
        assert resolve_write_scope_from_priv("普通内容", True, "种子,浇水") == "owner"

    def test_custom_wordlist_list(self):
        assert resolve_write_scope_from_priv("夜宵吃泡面", True, ["夜宵"]) == "intimate"

    def test_empty_content_owner(self):
        assert resolve_write_scope_from_priv("", True) == "owner"


class TestSessionGate:
    """会话身份闸门：非白名单会话（system/future_task）禁止升 intimate。"""

    WL = ["webchat:FriendMessage:webchat!zzz"]
    IL = ["webchat:FriendMessage:webchat!zzz"]

    def test_system_session_no_upgrade(self):
        # system 会话写带敏感词内容 → 只能 owner（升 intimate 会导致自己读不到）
        s = resolve_write_scope_for_session(
            "future_task:wakeup:abc", "提醒橘子吃药注意身体", True,
            owner_whitelist=self.WL, intimate_sessions=self.IL,
        )
        assert s == "owner"

    def test_whitelist_session_upgrades(self):
        s = resolve_write_scope_for_session(
            "webchat:FriendMessage:webchat!zzz!f98189d3-8a93-41d3-bdaf-3b912d9fef0f",
            "今晚贴贴", True, owner_whitelist=self.WL, intimate_sessions=self.IL,
        )
        assert s == "intimate"

    def test_whitelist_session_normal_owner(self):
        s = resolve_write_scope_for_session(
            "webchat:FriendMessage:webchat!zzz!f98189d3-8a93-41d3-bdaf-3b912d9fef0f",
            "插件热重载流程", True, owner_whitelist=self.WL, intimate_sessions=self.IL,
        )
        assert s == "owner"

    def test_qq_owner_session_upgrades(self):
        # 橘子 QQ 会话（owner 白名单）也允许敏感词升 intimate
        s = resolve_write_scope_for_session(
            "default:FriendMessage:2436258218", "密码放保险箱", True,
            owner_whitelist=self.WL + ["default:FriendMessage:2436258218"],
        )
        assert s == "intimate"

    def test_disabled_none(self):
        assert resolve_write_scope_for_session("any:thing:x", "贴贴", False) is None


class TestEngineEntryConfCompat:
    """engine 层入口的两级 conf 兼容。"""

    def test_nested_conf(self):
        conf = {"privacy": {"enabled": True, "sensitive_words": "贴贴",
                            "owner_whitelist": "webchat:FriendMessage:webchat!zzz"}}
        s = resolve_write_scope("webchat:FriendMessage:webchat!zzz!f98189d3-8a93-41d3-bdaf-3b912d9fef0f", "贴贴", conf)
        assert s == "intimate"

    def test_flat_conf(self):
        conf = {"privacy_enabled": True}
        s = resolve_write_scope("webchat:FriendMessage:webchat!zzz", "正常", conf)
        assert s == "owner"

    def test_malformed_conf_none(self):
        assert resolve_write_scope("x:y:z", "贴贴", None) is None
        assert resolve_write_scope("x:y:z", "贴贴", "not-a-dict") is None
        assert resolve_write_scope("x:y:z", "贴贴", {}) is None


class TestHelpers:
    def test_extract_privacy_nested_priority(self):
        assert _extract_privacy({"privacy": {"enabled": True}})["enabled"] is True

    def test_extract_privacy_flat(self):
        assert _extract_privacy({"privacy_enabled": 1}) == {"enabled": 1}

    def test_split_csv(self):
        assert _split_csv("a, b ,,c") == ["a", "b", "c"]
        assert _split_csv(["x"]) == ["x"]
        assert _split_csv(None) == []

    def test_normalize_wordlist_default(self):
        assert "密码" in _normalize_wordlist(None)


# ── 2026-09-03 14:45 橘子拍板：QQ（default:FriendMessage:2436258218）进夫妻会话列表 ──
# 语义：橘子从哪个门进来都是完全体——QQ 会话检索侧三档全开，写入侧敏感词可升 intimate。

from astrbot_plugin_livingmemory.core.privacy_filter import resolve_scopes_for_origin


class TestQQIntimateSession:
    QQ = "default:FriendMessage:2436258218"
    WEB = "webchat:FriendMessage:webchat!zzz"
    WL = [WEB, QQ]
    IL = [WEB, QQ]

    def test_qq_sees_all_three_scopes(self):
        # 检索侧：QQ 会话可见 public+owner+intimate
        visible = resolve_scopes_for_origin(self.QQ, True, self.WL, self.IL)
        assert visible == {"public", "owner", "intimate"}

    def test_qq_with_uuid_tail_still_full(self):
        # UUID 漂移归一化后仍认得
        visible = resolve_scopes_for_origin(self.QQ + "!f98189d3-8a93-41d3-bdaf-3b912d9fef0f", True, self.WL, self.IL)
        assert visible == {"public", "owner", "intimate"}

    def test_webchat_full_as_before(self):
        visible = resolve_scopes_for_origin(self.WEB, True, self.WL, self.IL)
        assert visible == {"public", "owner", "intimate"}

    def test_stranger_public_only(self):
        # fail-closed：陌生人仍然只见 public
        visible = resolve_scopes_for_origin("aiocqhttp:GroupMessage:123456", True, self.WL, self.IL)
        assert visible == {"public"}

    def test_qq_write_upgrade_intimate(self):
        # 写入侧：QQ 会话命中敏感词可升 intimate
        s = resolve_write_scope_for_session(self.QQ, "今晚贴贴", True, owner_whitelist=self.WL, intimate_sessions=self.IL)
        assert s == "intimate"

    def test_qq_write_normal_owner(self):
        s = resolve_write_scope_for_session(self.QQ, "插件热重载", True, owner_whitelist=self.WL, intimate_sessions=self.IL)
        assert s == "owner"

    def test_system_session_still_gated(self):
        # 闸门不受影响：system/future_task 仍禁升档
        s = resolve_write_scope_for_session("future_task:wakeup:x", "贴贴", True, owner_whitelist=self.WL, intimate_sessions=self.IL)
        assert s == "owner"
