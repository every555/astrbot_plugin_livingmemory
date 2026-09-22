"""情感 v4.0 Phase 2 — AppraisalEngine 评估层单测。
全部走 mock（不真调 LLM）：兜底规则 / LLM解析与清洗 / 限频 / 免疫。"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.v2.emotion_core import EmotionCore, DEFAULT_BASELINE
from astrbot_plugin_livingmemory.core.v2.appraisal_engine import AppraisalEngine


class MockResp:
    def __init__(self, text):
        self.result_text = text


class MockProvider:
    def __init__(self, text=None, raise_exc=False):
        self.text = text
        self.raise_exc = raise_exc
        self.calls = 0

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("boom")
        return MockResp(self.text)


class MockContext:
    def __init__(self, provider=None):
        self._p = provider

    def get_provider_by_id(self, pid):
        return self._p

    def get_using_provider(self):
        return self._p


@pytest.fixture
def core(tmp_path):
    c = EmotionCore(str(tmp_path / "ap.db"))
    yield c
    if c._db is not None:
        c._db.close()


def make_engine(core, provider=None):
    return AppraisalEngine(MockContext(provider), core)


def _llm_json(chunxue: dict, user_mood: dict | None = None) -> str:
    return json.dumps({"chunxue": chunxue, "user_mood": user_mood or {}}, ensure_ascii=False)


GOOD = _llm_json({
    "pad_delta": {"pleasure": 0.25, "arousal": 0.1, "dominance": 0},
    "intensity": 0.85,
    "occ": "gratification",
    "trust_evidence": "kept_promise",
    "brief": "橘子兑现了承诺",
}, {"label": "satisfied", "hint": "夸他"})


class TestFallback:
    def test_love_keywords(self, core):
        e = make_engine(core)
        r = asyncio.run(e.safe_evaluate("p1", "老婆我想你了"))
        assert r is not None
        s = core.get_state("p1")
        assert s["emotion"]["values"]["pleasure"] > DEFAULT_BASELINE["pleasure"]
        assert r["trust_applied"] is not None

    def test_default_neutral(self, core):
        e = make_engine(core)
        r = asyncio.run(e.safe_evaluate("p1x", "今天吃什么"))
        assert r is not None
        assert r["queued_rumination"] is False


class TestLLMPath:
    def test_llm_parse_and_apply(self, core):
        e = make_engine(core, MockProvider(GOOD))
        r = asyncio.run(e.safe_evaluate("p2", "那件事我办好了"))
        assert r is not None
        s = core.get_state("p2")
        assert s["emotion"]["values"]["pleasure"] > DEFAULT_BASELINE["pleasure"] + 0.2
        assert r["trust_applied"] == pytest.approx(DEFAULT_BASELINE["trust"] + 0.05)
        assert r["moment_anchor"] is not None

    def test_llm_garbage_falls_to_rule(self, core):
        e = make_engine(core, MockProvider("完全不是JSON的胡话"))
        r = asyncio.run(e.safe_evaluate("p3", "老婆真可爱"))
        assert r is not None
        assert core.get_state("p3")["emotion"]["values"]["pleasure"] > DEFAULT_BASELINE["pleasure"]

    def test_llm_exception_falls_to_rule(self, core):
        e = make_engine(core, MockProvider(raise_exc=True))
        r = asyncio.run(e.safe_evaluate("p4", "爱你老婆"))
        assert r is not None

    def test_illegal_trust_evidence_sanitized(self, core):
        bad = _llm_json({
            "pad_delta": {"pleasure": 0.1}, "intensity": 0.4, "occ": "joy",
            "trust_evidence": "made_up_type", "brief": "x",
        })
        e = make_engine(core, MockProvider(bad))
        asyncio.run(e.safe_evaluate("p5", "随便聊聊天气"))
        assert abs(core.get_state("p5")["mood"]["values"]["trust"] - DEFAULT_BASELINE["trust"]) < 1e-9

    def test_pad_delta_clamped(self, core):
        wild = _llm_json({
            "pad_delta": {"pleasure": 5.0, "arousal": -9.0},
            "intensity": 0.5, "occ": "joy", "trust_evidence": None, "brief": "x",
        })
        e = make_engine(core, MockProvider(wild))
        asyncio.run(e.safe_evaluate("p6", "超大声夸夸夸"))
        e2 = core.get_state("p6")["emotion"]["values"]
        assert e2["pleasure"] <= 1.0 and e2["arousal"] >= 0.0


class TestThrottle:
    def test_cooldown(self, core):
        e = make_engine(core)
        assert asyncio.run(e.safe_evaluate("p7", "第一句话")) is not None
        assert asyncio.run(e.safe_evaluate("p7", "第二句话马上又来")) is None

    def test_short_text_skipped(self, core):
        e = make_engine(core)
        assert asyncio.run(e.safe_evaluate("p8", "好")) is None


class TestImmunity:
    def test_no_provider_no_crash(self, core):
        e = make_engine(core, MockContext(None))
        r = asyncio.run(e.safe_evaluate("p9", "没有provider也能兜底"))
        assert r is not None

    def test_core_broken_still_safe(self, core):
        e = make_engine(core)
        e.core = None
        r = asyncio.run(e.safe_evaluate("p10", "随便说点什么"))
        assert r is None
