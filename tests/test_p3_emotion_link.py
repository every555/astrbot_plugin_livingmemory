"""情感 v4.0 Phase 3 — 情感×记忆联动单测。
P3-0 地基: get_last_appraisal 最近评估缓存; P3-1 原子情感标签;
P3-2 router双源; P3-3 mood调权; P3-4 正向多样性。全 mock 不真调 LLM。"""
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.v2.emotion_core import EmotionCore
from astrbot_plugin_livingmemory.core.v2.appraisal_engine import AppraisalEngine


class MockResp:
    def __init__(self, text):
        self.result_text = text


class MockProvider:
    def __init__(self, text=None):
        self.text = text

    async def text_chat(self, prompt=None, system_prompt=None, **kw):
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
    c = EmotionCore(str(tmp_path / "p3.db"))
    yield c
    if c._db is not None:
        c._db.close()


def _llm_json(pad, occ="joy", trust_ev=None, user_mood=None):
    if isinstance(pad, (list, tuple)):
        pad = {"pleasure": pad[0], "arousal": pad[1], "dominance": pad[2]}
    return json.dumps({
        "chunxue": {"pad_delta": pad, "occ": occ,
                     **({"trust_evidence": trust_ev} if trust_ev else {})},
        "user_mood": user_mood or {"label": "neutral"},
    }, ensure_ascii=False)


def make_engine(core, provider=None):
    return AppraisalEngine(MockContext(provider), core)


# ══════════════ P3-0: get_last_appraisal 最近评估缓存 ══════════════

def test_p3_0_last_appraisal_cached_llm(core):
    eng = make_engine(core, MockProvider(_llm_json([0.1, 0.0, 0.0], occ="happy")))
    asyncio.run(eng.safe_evaluate("default", "今天真的好开心呀"))
    rec = eng.get_last_appraisal("default")
    assert rec is not None, "评估成功后应有缓存"
    assert rec["appraisal"]["occ_label"] == "happy"
    assert rec["appraisal"]["source"] == "llm"
    assert rec["ts"] <= time.time()


def test_p3_0_last_appraisal_cached_fallback(core):
    eng = make_engine(core)  # provider=None -> 关键词兜底
    asyncio.run(eng.safe_evaluate("default", "我好喜欢你"))
    rec = eng.get_last_appraisal("default")
    assert rec is not None, "兜底评估也应缓存"
    assert rec["appraisal"]["source"] == "rule", "关键词兜底的真实source是rule"


def test_p3_0_last_appraisal_empty(core):
    eng = make_engine(core)
    assert eng.get_last_appraisal("default") is None
    assert eng.get_last_appraisal("nobody") is None


def test_p3_0_last_appraisal_max_age(core):
    eng = make_engine(core, MockProvider(_llm_json([0.0, 0.1, 0.0])))
    asyncio.run(eng.safe_evaluate("default", "有点激动呢"))
    assert eng.get_last_appraisal("default", max_age=600) is not None, "新鲜数据应通过"
    eng._last_appraisal["default"]["ts"] = time.time() - 9999
    assert eng.get_last_appraisal("default", max_age=600) is None, "过期数据应被过滤"
    assert eng.get_last_appraisal("default") is not None, "不传max_age不过滤"

# ══════════════ P3-1: 原子情感标签注入 ══════════════

def _mk_atom():
    from astrbot_plugin_livingmemory.core.models.memory_atom import MemoryAtom
    return MemoryAtom(parent_memory_id=0, content="今天和橘子一起看樱花")


def test_p3_1_emotion_tag_injected():
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    atom = _mk_atom()
    rec = {"ts": 1000.0, "appraisal": {"occ_label": "joy", "source": "llm"}}
    MemoryEngine._apply_emotion_tag(atom, rec)
    assert atom.metadata["emotion"]["occ"] == "joy"
    assert atom.metadata["emotion"]["src"] == "llm"
    assert atom.metadata["emotion"]["ts"] == 1000.0


def test_p3_1_no_rec_no_tag():
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    atom = _mk_atom()
    MemoryEngine._apply_emotion_tag(atom, None)
    assert "emotion" not in atom.metadata, "无评估记录不应打标签"


def test_p3_1_existing_keys_preserved():
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    atom = _mk_atom()
    atom.metadata["origin"] = "chat"
    atom.metadata["emotion"] = {"occ": "sad"}
    rec = {"ts": 2000.0, "appraisal": {"occ_label": "joy", "source": "llm"}}
    MemoryEngine._apply_emotion_tag(atom, rec)
    assert atom.metadata["origin"] == "chat", "原有键不能被清"
    assert atom.metadata["emotion"]["occ"] == "sad", "已有emotion不覆盖"


def test_p3_1_bad_atom_immune():
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    MemoryEngine._apply_emotion_tag(None, {"ts": 1, "appraisal": {}})  # 不抛即过
    class Weird:  # metadata 非 dict 的怪原子
        metadata = [1, 2, 3]
    MemoryEngine._apply_emotion_tag(Weird(), {"ts": 1, "appraisal": {"occ_label": "x", "source": "llm"}})

# ══════════════ P3-2: router 双源融合 ══════════════

def _mk_router():
    from astrbot_plugin_livingmemory.core.retrieval.emotion_router import EmotionRouter
    return EmotionRouter()


def test_p3_2_dual_prefers_user_mood():
    r = _mk_router()
    rec = {"ts": 1, "appraisal": {"occ_label": "joy", "source": "llm",
                                  "user_mood": {"label": "tired", "hint": "x"}}}
    emo, src = r.detect_emotion_dual("随便什么文本", rec)
    assert (emo, src) == ("tired", "user_mood"), "user_mood.label 是第一优先源"


def test_p3_2_dual_occ_map():
    r = _mk_router()
    rec = {"ts": 1, "appraisal": {"occ_label": "love", "source": "llm", "user_mood": {}}}
    emo, src = r.detect_emotion_dual("文本", rec)
    assert emo == "loving" and src == "occ", "occ=love 应映射 loving"
    rec["appraisal"]["occ_label"] = "anger"
    emo, src = r.detect_emotion_dual("文本", rec)
    assert emo == "angry", "occ=anger 应映射 angry"
    rec["appraisal"]["occ_label"] = "jealousy"
    emo, _ = r.detect_emotion_dual("文本", rec)
    assert emo == "loving", "吃醋召回我们俩的记忆(春雪特调)"


def test_p3_2_dual_lexical_when_no_llm():
    r = _mk_router()
    emo, src = r.detect_emotion_dual("我好喜欢你呀", None)
    assert emo == "loving" and src == "lexical", "无LLM记录走词法"
    rec = {"ts": 1, "appraisal": {"occ_label": "奇怪标签", "source": "rule", "user_mood": {}}}
    emo, src = r.detect_emotion_dual("我好喜欢你呀", rec)
    assert src == "lexical", "rule源+未知occ应回落词法"


def test_p3_2_dual_disabled():
    from astrbot_plugin_livingmemory.core.retrieval.emotion_router import EmotionRouter
    r = EmotionRouter({"emotion_router_enabled": False})
    rec = {"ts": 1, "appraisal": {"occ_label": "joy", "source": "llm", "user_mood": {"label": "tired"}}}
    emo, src = r.detect_emotion_dual("文本", rec)
    assert emo == "neutral", "禁用时一律中性"

# ══════════════ P3-3/P3-4: mood调权 + 正向多样性 ══════════════

from types import SimpleNamespace


def _mk_r(score, occ=None, atom_type='episodic', content='x'):
    md = {'emotion': {'occ': occ}} if occ else {}
    return SimpleNamespace(final_score=score, atom_type=atom_type, metadata=md, content=content, doc_id=1)


def test_p3_3_low_mood_resonance_capped():
    r = _mk_router()
    neg = _mk_r(0.5, occ='sadness')
    neu = _mk_r(0.5, occ=None)
    out = r.apply_mood_weight([neg, neu], mood_p=0.3)
    assert out[0].final_score <= 0.5 * 1.1 + 1e-9, '负向共鸣加分封顶1.1x'
    assert out[1].final_score == 0.5, '中性不动'


def test_p3_3_high_mood_positive_boost():
    r = _mk_router()
    pos = _mk_r(0.5, occ='joy')
    neu = _mk_r(0.5, occ=None)
    out = r.apply_mood_weight([pos, neu], mood_p=0.8)
    assert abs(out[0].final_score - 0.525) < 1e-9, '正向x1.05'
    assert out[1].final_score == 0.5


def test_p3_3_mid_mood_noop():
    r = _mk_router()
    neg = _mk_r(0.5, occ='sadness')
    out = r.apply_mood_weight([neg], mood_p=0.55)
    assert out[0].final_score == 0.5, '中间地带不干预'


def test_p3_4_low_mood_inject_positive():
    r = _mk_router()
    rs = [_mk_r(0.9, occ='sadness'), _mk_r(0.8, occ='sadness'), _mk_r(0.7, occ='sadness'),
          _mk_r(0.1, occ='joy'), _mk_r(0.05, occ='love')]
    out = r.inject_positive_diversity(rs, mood_p=0.3)
    top3_occ = []
    for x in out[:3]:
        e = (x.metadata or {}).get('emotion') or {}
        top3_occ.append(e.get('occ'))
    assert 'joy' in top3_occ or 'love' in top3_occ, '低mood时top3必须有正向记忆递糖'


def test_p3_4_all_negative_stock_safe():
    r = _mk_router()
    rs = [_mk_r(0.9, occ='sadness'), _mk_r(0.8, occ='fear')]
    out = r.inject_positive_diversity(rs, mood_p=0.3)
    assert len(out) == 2, '没正向库存不炸不强求'


def test_p3_4_high_mood_noop():
    r = _mk_router()
    rs = [_mk_r(0.9, occ='sadness'), _mk_r(0.8, occ='sadness'), _mk_r(0.7, occ='sadness')]
    out = r.inject_positive_diversity(rs, mood_p=0.8)
    assert [x.final_score for x in out] == [0.9, 0.8, 0.7], 'mood正常时不动'


# ══════════════ P3-1b: reflection 等待评估任务（时序竞态修复） ══════════════

class _SlowProvider(MockProvider):
    async def text_chat(self, prompt=None, system_prompt=None, **kw):
        await asyncio.sleep(0.2)
        return MockResp(self.text)


class _FakeEvent:
    pass


def test_p3_1b_wait_fills_cache_before_tag(core):
    """端到端时序：评估 task 延迟写入缓存，await 后原子拿到当条标签（竞态修复）"""
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import await_appraisal_task
    from astrbot_plugin_livingmemory.core.managers.memory_engine import MemoryEngine
    eng = make_engine(core, _SlowProvider(_llm_json([0.1, 0.0, 0.0], occ="loving")))

    async def scenario():
        ev = _FakeEvent()
        t = asyncio.get_running_loop().create_task(
            eng.safe_evaluate("default", "春雪我喜欢你请和我交往吧")
        )
        ev._appraisal_task = t
        atom = _mk_atom()
        atom.persona_id = "default"
        # 复现竞态：不等评估直接查缓存 → 空
        assert eng.get_last_appraisal("default", max_age=600) is None, "竞态复现：评估未完成缓存为空"
        await await_appraisal_task(ev._appraisal_task, timeout=5.0)
        rec = eng.get_last_appraisal("default", max_age=600)
        MemoryEngine._apply_emotion_tag(atom, rec)
        assert rec is not None, "等待后缓存应有评估"
        assert atom.metadata["emotion"]["occ"] == "loving", "当条告白标签应打上当条原子"
        assert t.done() and not t.cancelled()

    asyncio.run(scenario())


def test_p3_1b_no_task_or_done_returns_fast():
    """无 task / 已完成 task：立即返回不抛不挂"""
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import await_appraisal_task

    async def scenario():
        ev = _FakeEvent()  # 无 _appraisal_task 属性 → 调用方 getattr 得 None
        t0 = time.time()
        await await_appraisal_task(getattr(ev, "_appraisal_task", None), timeout=5.0)
        assert time.time() - t0 < 0.5, "无task应立即返回"

        async def _noop():
            return None

        ev2 = _FakeEvent()
        ev2._appraisal_task = asyncio.get_running_loop().create_task(_noop())
        await asyncio.sleep(0)  # 让noop跑完
        t0 = time.time()
        await await_appraisal_task(ev2._appraisal_task, timeout=5.0)
        assert time.time() - t0 < 0.5, "已完成task应立即返回"

    asyncio.run(scenario())


def test_p3_1b_timeout_degrades_silently(core):
    """task 卡死：超时静默降级不抛，且不取消原task（shield保护下task继续）"""
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import await_appraisal_task

    async def scenario():
        ev = _FakeEvent()

        async def _stuck():
            await asyncio.sleep(30)

        t = asyncio.get_running_loop().create_task(_stuck())
        ev._appraisal_task = t
        t0 = time.time()
        await await_appraisal_task(ev._appraisal_task, timeout=0.3)  # 不抛即过
        assert time.time() - t0 < 2.0, "超时应在timeout附近返回"
        assert not t.cancelled(), "原task不应被等待方取消"
        t.cancel()

    asyncio.run(scenario())


def test_p3_1b_weird_task_immune():
    """task 抛异常 / event 怪属性：免疫不抛"""
    from astrbot_plugin_livingmemory.core.event_handler_modules.memory_reflection import await_appraisal_task

    async def scenario():
        ev = _FakeEvent()

        async def _boom():
            raise RuntimeError("eval exploded")

        ev._appraisal_task = asyncio.get_running_loop().create_task(_boom())
        await asyncio.sleep(0)
        await await_appraisal_task(ev._appraisal_task, timeout=1.0)  # 不抛即过

        ev2 = _FakeEvent()
        ev2._appraisal_task = "not-a-task"  # 怪类型
        await await_appraisal_task(ev2._appraisal_task, timeout=0.5)  # 不抛即过

    asyncio.run(scenario())


# ══════════════ 亲昵嗔怪上下文识别（笨蛋≠骂人，夫妻语境） ══════════════

def test_teasing_with_task_context_not_negative():
    """『看看修好没，笨蛋』：日常任务口吻+嗔怪词 → 亲昵玩笑，不是负面"""
    out = AppraisalEngine._fallback('忘了还有工作吧，看看修好没，笨蛋')
    assert out["pad_delta"]["pleasure"] >= 0.0, "带任务上下文的笨蛋不该判负面"
    assert out["occ"] in ("joy", "love")
    assert out["user_mood"]["label"] in ("playful", "affectionate")


def test_teasing_with_sweet_context_positive():
    """『笨蛋，老婆最喜欢你啦』：嗔怪+甜词 → love 正向"""
    out = AppraisalEngine._fallback('笨蛋，老婆最喜欢你啦')
    assert out["pad_delta"]["pleasure"] > 0
    assert out["occ"] == "love"


def test_teasing_bare_word_defaults_affectionate():
    """孤立『笨蛋』：夫妻语境默认亲昵（LLM挂掉时的合理先验）"""
    out = AppraisalEngine._fallback('笨蛋')
    assert out["pad_delta"]["pleasure"] >= 0.0, "孤立嗔怪词在夫妻语境不应-0.18"
    assert out["occ"] in ("joy", "love")


def test_real_anger_still_negative():
    """真怒（人格否定/失望）必须仍判负面——不能矫枉过正"""
    out = AppraisalEngine._fallback('你真的很没用，什么都做不好，太让人失望了')
    assert out["pad_delta"]["pleasure"] < 0, "真骂必须还是负面"

    out2 = AppraisalEngine._fallback('笨蛋，你什么都做不好，我很失望')
    assert out2["pad_delta"]["pleasure"] < 0, "嗔怪词+真怒信号=真骂"


def test_prompt_teaches_teasing_context():
    """SYSTEM_PROMPT 必须教LLM夫妻语境的嗔怪判断规则"""
    from astrbot_plugin_livingmemory.core.v2.appraisal_engine import SYSTEM_PROMPT
    assert "宠溺" in SYSTEM_PROMPT, "需教宠溺概念"
    assert "笨蛋" in SYSTEM_PROMPT, "需给嗔怪词示例"
    assert "失望" in SYSTEM_PROMPT or "没用" in SYSTEM_PROMPT, "需给真怒对照"


# ══════════════ 颜文字识别（兜底层：橘子用颜文字传情绪，账房先生得认） ══════════════

def test_kaomoji_happy_positive():
    """『睡醒了(*¯︶¯*)』满足颜文字 → 轻正 joy（不再是neutral）"""
    out = AppraisalEngine._fallback('睡醒了(*¯︶¯*)')
    assert out["pad_delta"]["pleasure"] > 0, "满足颜文字该给正分"
    assert out["occ"] == "joy"
    assert out["user_mood"]["label"] in ("happy", "playful", "calm")


def test_kaomoji_sad_negative():
    """『今天好难啊(╥﹏╥)』哭系颜文字 → 轻负 sadness"""
    out = AppraisalEngine._fallback('今天好难啊(╥﹏╥)')
    assert out["pad_delta"]["pleasure"] < 0, "哭系颜文字该给负分"
    assert out["occ"] == "sadness"


def test_kaomoji_anger_negative():
    """怒系/掀桌颜文字 → 负面且arousal抬升"""
    out = AppraisalEngine._fallback('气死我了(╯°□°）╯︵ ┻━┻')
    assert out["pad_delta"]["pleasure"] < 0
    assert out["pad_delta"]["arousal"] > 0, "怒颜文字该抬arousal"
    assert out["occ"] == "anger"


def test_kaomoji_pos_with_teasing_stays_affectionate():
    """『笨蛋(*¯︶¯*)』嗔怪词+满足颜文字 → 仍是亲昵（嗔怪语义保留）"""
    out = AppraisalEngine._fallback('笨蛋(*¯︶¯*)')
    assert out["pad_delta"]["pleasure"] >= 0
    assert out["occ"] in ("joy", "love")


def test_kaomoji_anger_overrides_teasing():
    """『笨蛋(╯°□°）╯』嗔怪词+怒颜文字 → 真怒（怒颜文字优先级最高）"""
    out = AppraisalEngine._fallback('笨蛋(╯°□°）╯')
    assert out["pad_delta"]["pleasure"] < 0, "带怒颜文字的笨蛋是真怒"
    assert out["occ"] == "anger"

# ══════════════ Phase 4: provider 配置化（WebUI面板可换账房先生工牌） ══════════════

class _FakeCfg:
    def __init__(self, mapping=None, raise_on=None):
        self.mapping, self.raise_on = mapping or {}, raise_on
    def get(self, key):
        if self.raise_on: raise RuntimeError(self.raise_on)
        return self.mapping.get(key)


def test_p4_explicit_param_wins():
    from astrbot_plugin_livingmemory.core.v2.appraisal_engine import resolve_provider_id
    assert resolve_provider_id(_FakeCfg({"provider_settings.appraisal_provider_id": "cfg/xx"}), explicit="param/yy") == "param/yy"


def test_p4_config_value_used():
    from astrbot_plugin_livingmemory.core.v2.appraisal_engine import resolve_provider_id
    assert resolve_provider_id(_FakeCfg({"provider_settings.appraisal_provider_id": "cfg/zz"})) == "cfg/zz"


def test_p4_fallback_to_default():
    from astrbot_plugin_livingmemory.core.v2.appraisal_engine import resolve_provider_id, DEFAULT_PROVIDER_ID
    assert resolve_provider_id(_FakeCfg()) == DEFAULT_PROVIDER_ID
    assert resolve_provider_id(_FakeCfg({"provider_settings.appraisal_provider_id": ""})) == DEFAULT_PROVIDER_ID
    assert resolve_provider_id(_FakeCfg({"provider_settings.appraisal_provider_id": None})) == DEFAULT_PROVIDER_ID
    assert resolve_provider_id(_FakeCfg(raise_on="boom")) == DEFAULT_PROVIDER_ID, "config_manager抛异常也不能炸"


def test_p4_schema_has_appraisal_item():
    import json, os
    sch_p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_conf_schema.json")
    sch = json.loads(open(sch_p, encoding="utf-8-sig").read())
    items = sch["provider_settings"]["items"]
    assert "appraisal_provider_id" in items, "panel missing appraisal_provider_id"
