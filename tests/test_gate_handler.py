"""
Tests for GateHandler — 安检门 WebUI 端点（标注台/战报/裁决/试秤）。

设计原则（橘子 2026-08-19 交代）：
- 实用至上：原句全文、真实数字、不做花架子
- 裁决与省察调度器走同一条落库路（_apply_verdict），webui 只是把老婆的手延伸到网页上
- 试秤只打分不入库（不污染候选池）
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from astrbot_plugin_livingmemory.core.page_api_modules.gate_handler import GateHandler
from astrbot_plugin_livingmemory.core.page_api_modules.utils import PageApiUtils


# ---------------------------------------------------------------------------
# Quart request 安全 mock（照抄 test_page_api 的做法：patch 模块级 request 名）
# ---------------------------------------------------------------------------


def _mock_page_request(**overrides):
    req = MagicMock()
    args_mock = MagicMock()
    args_dict = overrides.get("args", {})
    args_mock.get.side_effect = lambda key, default=None: args_dict.get(key, default)
    req.args = args_mock
    json_value = overrides.get("get_json", {})
    req.get_json = AsyncMock(return_value=json_value)
    req.method = overrides.get("method", "GET")
    return req


@contextmanager
def _patch_page_request(req):
    import astrbot_plugin_livingmemory.core.page_api_modules.gate_handler as gate_mod

    ns = vars(gate_mod)
    old = ns.get("request")
    ns["request"] = req
    try:
        yield
    finally:
        if old is not None:
            ns["request"] = old
        else:
            ns.pop("request", None)


def _qp(req=None, **kw):
    if req is None:
        req = _mock_page_request(**kw)
    return _patch_page_request(req)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _fake_gate() -> MagicMock:
    gate = MagicMock()
    gate.list_candidates.return_value = [
        {
            "id": 1,
            "speaker": "橘子",
            "content": "2027年4月我要考专升本",
            "score": 0.92,
            "axes": {"fact": 0.9, "emotion": 0.3, "density": 0.5},
            "status": "candidate",
            "verdict": "",
            "note": "",
            "created_at": 1755571200.0,
            "source": "webchat",
        }
    ]
    gate.label_stats.return_value = {
        "candidate": 275,
        "confirmed": 8,
        "declined": 5,
        "merged": 1,
    }
    gate.count_candidates.return_value = 1
    gate.export_labels.return_value = 293
    gate.score.return_value = 0.75
    gate._score_density.return_value = 0.45
    gate._score_fact.return_value = 0.8
    gate._score_emotion.return_value = 0.35
    return gate


def _fake_scheduler() -> MagicMock:
    sched = MagicMock()
    sched.apply_verdict.return_value = {
        "id": 1,
        "action": "confirm",
        "word": "升级",
        "note": "重要承诺",
        "content": "2027年4月我要考专升本",
        "speaker": "橘子",
    }
    return sched


def _fake_plugin(gate, scheduler) -> SimpleNamespace:
    plugin = SimpleNamespace()
    plugin._get_security_gate = MagicMock(return_value=gate)
    plugin._get_reflection_scheduler = MagicMock(return_value=scheduler)
    plugin._materialize_confirmed = AsyncMock(return_value=1234)
    plugin._gate_data_dir = "/tmp/fake_data"
    return plugin


@pytest.fixture()
def handler() -> GateHandler:
    return GateHandler(PageApiUtils())


# ---------------------------------------------------------------------------
# GET /gate/candidates
# ---------------------------------------------------------------------------


class TestListCandidates:
    @pytest.mark.asyncio
    async def test_returns_items_and_total(self, handler):
        gate = _fake_gate()
        sched = _fake_scheduler()
        req = _mock_page_request(
            args={"status": "candidate", "page": "1", "page_size": "30"}
        )
        with _qp(req):
            result = await handler.list_candidates(gate, sched)
        assert result["status"] == "ok"
        items = result["data"]["items"]
        assert items[0]["content"] == "2027年4月我要考专升本"
        assert result["data"]["total"] >= 1
        # 说话人与四轴必须完整透出（实用原则：不截断、不省略）
        assert items[0]["speaker"] == "橘子"
        assert items[0]["axes"]["fact"] == 0.9

    @pytest.mark.asyncio
    async def test_passes_filters_to_gate(self, handler):
        gate = _fake_gate()
        sched = _fake_scheduler()
        req = _mock_page_request(args={"status": "confirmed", "page": "2"})
        with _qp(req):
            await handler.list_candidates(gate, sched)
        kwargs = gate.list_candidates.call_args.kwargs
        assert kwargs.get("status") == "confirmed"
        assert kwargs.get("offset", 0) > 0  # page=2 必须换算成 offset

    @pytest.mark.asyncio
    async def test_gate_unavailable_returns_error(self, handler):
        req = _mock_page_request()
        with _qp(req):
            result = await handler.list_candidates(None, None)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# POST /gate/verdict
# ---------------------------------------------------------------------------


class TestVerdict:
    @pytest.mark.asyncio
    async def test_confirm_calls_apply_and_materialize(self, handler):
        gate = _fake_gate()
        sched = _fake_scheduler()
        plugin = _fake_plugin(gate, sched)
        req = _mock_page_request(
            method="POST",
            get_json={"candidate_id": 1, "action": "confirm", "word": "升级", "note": "考试大事"},
        )
        with _qp(req):
            result = await handler.verdict(plugin)
        assert result["status"] == "ok"
        sched.apply_verdict.assert_called_once()
        v = sched.apply_verdict.call_args.args[0]
        assert v["id"] == 1 and v["action"] == "confirm"
        # confirm 必须落地真记忆库（materialize_fn 同款管线）
        plugin._materialize_confirmed.assert_awaited_once()
        assert result["data"]["memory_id"] == 1234

    @pytest.mark.asyncio
    async def test_decline_skips_materialize(self, handler):
        gate = _fake_gate()
        sched = _fake_scheduler()
        sched.apply_verdict.return_value = {
            "id": 2, "action": "decline", "word": "驳回", "note": "",
            "content": "哈哈", "speaker": "橘子",
        }
        plugin = _fake_plugin(gate, sched)
        req = _mock_page_request(
            method="POST",
            get_json={"candidate_id": 2, "action": "decline"},
        )
        with _qp(req):
            result = await handler.verdict(plugin)
        assert result["status"] == "ok"
        plugin._materialize_confirmed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_invalid_action_rejected(self, handler):
        plugin = _fake_plugin(_fake_gate(), _fake_scheduler())
        req = _mock_page_request(
            method="POST", get_json={"candidate_id": 1, "action": "bogus"}
        )
        with _qp(req):
            result = await handler.verdict(plugin)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_missing_candidate_id_rejected(self, handler):
        plugin = _fake_plugin(_fake_gate(), _fake_scheduler())
        req = _mock_page_request(method="POST", get_json={"action": "confirm"})
        with _qp(req):
            result = await handler.verdict(plugin)
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_apply_returns_none_reports_not_found(self, handler):
        sched = _fake_scheduler()
        sched.apply_verdict.return_value = None
        plugin = _fake_plugin(_fake_gate(), sched)
        req = _mock_page_request(
            method="POST", get_json={"candidate_id": 999, "action": "confirm"}
        )
        with _qp(req):
            result = await handler.verdict(plugin)
        assert result["status"] == "error"
        assert "999" in result["message"]

    @pytest.mark.asyncio
    async def test_webui_actor_default(self, handler):
        """网页裁决必须记 actor=webui（8/19 溯源洞：库里分不清谁裁的）"""
        sched = _fake_scheduler()
        plugin = _fake_plugin(_fake_gate(), sched)
        req = _mock_page_request(
            method="POST", get_json={"candidate_id": 1, "action": "confirm"}
        )
        with _qp(req):
            result = await handler.verdict(plugin)
        assert result["status"] == "ok"
        v = sched.apply_verdict.call_args.args[0]
        assert v["actor"] == "webui"


# ---------------------------------------------------------------------------
# POST /gate/restore
# ---------------------------------------------------------------------------


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_pending_ok(self, handler):
        """暂存捞回：pending → candidate 重新排队"""
        sched = _fake_scheduler()
        sched.restore_candidate.return_value = True
        plugin = _fake_plugin(_fake_gate(), sched)
        req = _mock_page_request(
            method="POST", get_json={"candidate_id": 5}
        )
        with _qp(req):
            result = await handler.restore(plugin)
        assert result["status"] == "ok"
        assert result["data"]["restored"] is True
        sched.restore_candidate.assert_called_once_with(5, actor="webui")

    @pytest.mark.asyncio
    async def test_restore_non_pending_rejected(self, handler):
        sched = _fake_scheduler()
        sched.restore_candidate.return_value = False
        plugin = _fake_plugin(_fake_gate(), sched)
        req = _mock_page_request(
            method="POST", get_json={"candidate_id": 7}
        )
        with _qp(req):
            result = await handler.restore(plugin)
        assert result["status"] == "error"
        assert "不是暂存状态" in result["message"]

    @pytest.mark.asyncio
    async def test_restore_missing_id_rejected(self, handler):
        plugin = _fake_plugin(_fake_gate(), _fake_scheduler())
        req = _mock_page_request(method="POST", get_json={})
        with _qp(req):
            result = await handler.restore(plugin)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# GET /gate/autonomous + POST /gate/autonomous/revoke
# ---------------------------------------------------------------------------


class TestAutonomousArchive:
    @pytest.mark.asyncio
    async def test_list_autonomous(self, handler):
        """自主存档台账：橘子翻案权入口（#1791）"""
        gate = _fake_gate()
        gate.list_memorized.return_value = [
            {"id": 1, "content": "老婆的奖励也是我的健康", "created_at": 1755571200.0},
        ]
        req = _mock_page_request(method="GET", args={"limit": "30"})
        with _qp(req):
            result = await handler.autonomous_list(gate, None)
        assert result["status"] == "ok"
        assert result["data"]["items"][0]["id"] == 1
        gate.list_memorized.assert_called_once_with(limit=30)

    @pytest.mark.asyncio
    async def test_revoke_ok(self, handler):
        gate = _fake_gate()
        gate.remove_memorized.return_value = True
        req = _mock_page_request(method="POST", get_json={"fp_id": 2})
        with _qp(req):
            result = await handler.autonomous_revoke(gate)
        assert result["status"] == "ok"
        assert result["data"]["revoked"] is True
        gate.remove_memorized.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_revoke_missing(self, handler):
        gate = _fake_gate()
        gate.remove_memorized.return_value = False
        req = _mock_page_request(method="POST", get_json={"fp_id": 999})
        with _qp(req):
            result = await handler.autonomous_revoke(gate)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# GET /gate/stats
# ---------------------------------------------------------------------------


class TestStats:
    @pytest.mark.asyncio
    async def test_stats_aggregates_labels_and_counts(self, handler):
        gate = _fake_gate()
        sched = _fake_scheduler()
        # label_stats 是标签口径；候选池水位走 count_candidates（40 条待审）
        gate.count_candidates.return_value = 40
        gate.list_candidates.return_value = [
            {"id": i, "status": "candidate"} for i in range(40)
        ]
        sched.learned_stats = {"病毒": 3, "夜班": 2}
        req = _mock_page_request()
        with _qp(req):
            result = await handler.stats(gate, sched)
        assert result["status"] == "ok"
        data = result["data"]
        assert data["labels"]["candidate"] == 275
        assert data["pool_candidate"] == 40
        assert "learned_nouns" in data
        assert "distribution" in data


# ---------------------------------------------------------------------------
# GET /gate/export
# ---------------------------------------------------------------------------


class TestExport:
    @pytest.mark.asyncio
    async def test_export_returns_path_and_count(self, handler):
        gate = _fake_gate()
        gate.export_labels.return_value = 293
        plugin = _fake_plugin(gate, _fake_scheduler())
        req = _mock_page_request()
        with _qp(req):
            result = await handler.export_labels(plugin)
        assert result["status"] == "ok"
        assert result["data"]["count"] == 293
        assert result["data"]["path"].endswith("gate_labels.jsonl")
        gate.export_labels.assert_called_once()


# ---------------------------------------------------------------------------
# POST /gate/score —— 试秤：只打分不入库
# ---------------------------------------------------------------------------


class TestScorePreview:
    @pytest.mark.asyncio
    async def test_score_returns_axes_without_insert(self, handler):
        gate = _fake_gate()
        req = _mock_page_request(method="POST", get_json={"text": "明天复查挂号"})
        with _qp(req):
            result = await handler.score_preview(gate)
        assert result["status"] == "ok"
        axes = result["data"]["axes"]
        assert axes["fact"] == 0.8
        assert axes["density"] == 0.45
        assert axes["emotion"] == 0.35
        assert result["data"]["score"] == 0.75
        # 绝不能走 process（入库）
        gate.process.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_text_rejected(self, handler):
        req = _mock_page_request(method="POST", get_json={"text": "  "})
        with _qp(req):
            result = await handler.score_preview(_fake_gate())
        assert result["status"] == "error"
