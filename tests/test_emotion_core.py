"""情感 v4.0 Phase 1 — EmotionCore 状态层单测。
覆盖：基线初始化 / clamp / 指数衰减回归 / mood吸收 / 信任证据+地板+甜话封顶 /
trust不衰减 / 反刍生命周期 / 持久化往返 / 情感时刻锚 / 纯内存降级。"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.v2.emotion_core import (
    EmotionCore, EmotionVector, DEFAULT_BASELINE, TAU_MOOD,
)


@pytest.fixture
def core(tmp_path):
    c = EmotionCore(str(tmp_path / "lm_v2_test.db"))
    yield c
    if c._db is not None:
        c._db.close()


class TestBaseline:
    def test_fresh_state_equals_baseline(self, core):
        s = core.get_state("p1")
        for ax in ("pleasure", "arousal", "dominance", "trust"):
            assert abs(s["mood"]["values"][ax] - DEFAULT_BASELINE[ax]) < 1e-6
            assert abs(s["emotion"]["values"][ax] - DEFAULT_BASELINE[ax]) < 1e-6


class TestApplyAndClamp:
    def test_apply_moves_and_clamps(self, core):
        core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.5, "arousal": -0.9}, "intensity": 0.8})
        s = core.get_state("p1")
        assert s["emotion"]["values"]["pleasure"] > 0.9995  # 0.62+0.5 越界→clamp上界(留毫秒级衰减余量)
        assert s["emotion"]["values"]["arousal"] < 0.001  # clamp 下界(留毫秒级衰减余量)

    def test_mood_moves_slower_than_emotion(self, core):
        core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.3}, "intensity": 0.9})
        s = core.get_state("p1")
        e = s["emotion"]["values"]["pleasure"]
        m = s["mood"]["values"]["pleasure"]
        assert m < e  # mood 吸收慢于 emotion
        assert m > DEFAULT_BASELINE["pleasure"]


class TestDecay:
    def test_emotion_decays_to_baseline(self, core):
        core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.35}, "intensity": 0.5})
        state = core._load("p1")
        now = time.time() + 10 * 180  # 快进10个tau
        core._lazy_decay(state, now=now)
        assert abs(state["emotion"]["values"]["pleasure"] - DEFAULT_BASELINE["pleasure"]) < 0.01

    def test_mood_decays_slower_than_emotion(self, core):
        core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.35}, "intensity": 1.0})
        state = core._load("p1")
        now = time.time() + 30 * 60  # 30分钟后
        core._lazy_decay(state, now=now)
        e_gap = abs(state["emotion"]["values"]["pleasure"] - DEFAULT_BASELINE["pleasure"])
        m_gap = abs(state["mood"]["values"]["pleasure"] - DEFAULT_BASELINE["pleasure"])
        assert m_gap > e_gap  # mood 慢层还没回到基线

    def test_trust_immune_to_decay(self, core):
        core.settle_trust("p1", "broken_promise", note="测试")
        before = core.get_state("p1")["mood"]["values"]["trust"]
        assert before < DEFAULT_BASELINE["trust"]
        state = core._load("p1")
        core._lazy_decay(state, now=time.time() + 48 * 3600)  # 两天后
        assert abs(state["mood"]["values"]["trust"] - before) < 1e-9  # trust 纹丝不动


class TestTrust:
    def test_evidence_values(self, core):
        v1 = core.settle_trust("p1", "kept_promise")
        assert v1 == pytest.approx(DEFAULT_BASELINE["trust"] + 0.05)
        v2 = core.settle_trust("p1", "broken_promise")
        assert v2 == pytest.approx(v1 - 0.08)

    def test_trust_floor(self, core):
        for _ in range(20):
            core.settle_trust("p1", "boundary_violation")
        assert core.get_state("p1")["mood"]["values"]["trust"] >= 0.15

    def test_sweet_words_session_cap(self, core):
        for _ in range(10):
            core.settle_trust("p1", "sweet_words")
        gain = core.get_state("p1")["mood"]["values"]["trust"] - DEFAULT_BASELINE["trust"]
        assert gain <= 0.03 + 1e-9

    def test_unknown_evidence_ignored(self, core):
        assert core.settle_trust("p1", "hug_attack") is None
        assert abs(core.get_state("p1")["mood"]["values"]["trust"] - DEFAULT_BASELINE["trust"]) < 1e-9

    def test_trust_history_rolling_window(self, core):
        for i in range(60):
            core.settle_trust("p1", "kept_promise")
        assert len(core.get_state("p1")["trust_history"]) <= 50


class TestRumination:
    def test_lifecycle(self, core):
        r = core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.3}, "intensity": 0.9, "occ_label": "pride", "brief": "被橘子夸了"})
        assert r["queued_rumination"] is True
        digested_any = False
        for _ in range(3):
            out = core.step_rumination("p1")
            if out and out[0]["digested"]:
                assert "（还在想：" in out[0]["inner_voice"]
                digested_any = True
                break
            assert out, "步进应有返回"
        assert digested_any, "3步内应消化完成出队"
        assert core.step_rumination("p1") == []  # 队列空了

    def test_low_intensity_no_queue(self, core):
        r = core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.05}, "intensity": 0.3})
        assert r["queued_rumination"] is False


class TestMomentAnchor:
    def test_anchor_emitted(self, core):
        r = core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.2}, "intensity": 0.87, "occ_label": "gratification", "brief": "橘子的定心丸"})
        assert r["moment_anchor"] is not None
        assert r["moment_anchor"]["brief"] == "橘子的定心丸"
        assert "mood_pleasure" in r["moment_anchor"]["snapshot"]

    def test_no_anchor_below_threshold(self, core):
        r = core.on_appraisal("p1", {"pad_delta": {"pleasure": 0.1}, "intensity": 0.6})
        assert r["moment_anchor"] is None


class TestPersistence:
    def test_roundtrip(self, tmp_path):
        db = str(tmp_path / "rt.db")
        c1 = EmotionCore(db)
        c1.on_appraisal("p1", {"pad_delta": {"pleasure": 0.2}, "intensity": 0.5})
        c1.settle_trust("p1", "kept_promise")
        c1._db.close()
        c2 = EmotionCore(db)
        s = c2.get_state("p1")
        assert s["mood"]["values"]["trust"] == pytest.approx(DEFAULT_BASELINE["trust"] + 0.05)
        assert s["mood"]["values"]["pleasure"] > DEFAULT_BASELINE["pleasure"]
        assert len(s["trust_history"]) == 1
        c2._db.close()


class TestFallback:
    def test_pure_memory_mode(self):
        c = EmotionCore(None)  # 无库：纯内存不崩
        r = c.on_appraisal("p1", {"pad_delta": {"pleasure": 0.3}, "intensity": 0.9})
        assert r["queued_rumination"] is False  # 无库不入队但不炸
        assert c.step_rumination("p1") == []
        c.decay_all()

    def test_corrupt_json_falls_back_to_baseline(self, tmp_path):
        db = str(tmp_path / "corrupt.db")
        c = EmotionCore(db)
        c._db_set_state("p1", "{not-json")
        try:
            s = c.get_state("p1")
            assert s["mood"]["values"]["trust"] == pytest.approx(DEFAULT_BASELINE["trust"])
        except json.JSONDecodeError:
            pytest.fail("坏JSON应降级到基线而不是抛异常")
        c._db.close()
