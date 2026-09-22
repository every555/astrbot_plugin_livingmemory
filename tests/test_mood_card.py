"""P0 心情卡片注入单测——纯函数零依赖，不真调 LLM。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from astrbot_plugin_livingmemory.core.v2.mood_card import (
    build_mood_card,
    make_card,
    OCC_ZH,
)


def _snap(p=0.71, a=0.59, d=0.55, t=0.98):
    return {
        "mood_pleasure": p, "mood_arousal": a,
        "mood_dominance": d, "mood_trust": t,
        "emotion_pleasure": p, "emotion_arousal": a,
        "emotion_dominance": d, "emotion_trust": t,
    }


def test_pleasure_words():
    """mood pleasure 分档: 雀跃/舒心/平静/蔫蔫的"""
    for p, word in [(0.75, "雀跃"), (0.60, "舒心"), (0.50, "平静"), (0.40, "蔫蔫的")]:
        card = build_mood_card(_snap(p=p), None)
        assert card is not None, f"p={p} 卡片为 None"
        assert word in card, f"p={p} 应含 {word!r}: {card}"


def test_occ_zh_13_labels():
    """appraisal prompt 的 13 个 OCC 标签中文映射齐全"""
    expect = {
        "joy", "pride", "gratification", "love", "hope", "fear",
        "anger", "sadness", "pity", "jealousy", "relief", "guilt", "shame",
    }
    missing = expect - set(OCC_ZH.keys())
    assert not missing, f"缺标签映射: {missing}"
    for k, v in OCC_ZH.items():
        assert v, f"{k} 的中文为空"


def test_card_with_appraisal():
    ap = {"occ_label": "joy", "brief": "橘子夸了我一句", "intensity": 0.6}
    card = build_mood_card(_snap(), ap)
    assert "开心" in card, card
    assert "橘子夸了我一句" in card, card
    assert "P0.71" in card, card
    assert "A0.59" in card, card
    assert "0.98" in card, card  # trust


def test_card_without_appraisal():
    card = build_mood_card(_snap(), None)
    assert card and "春雪" in card, card
    assert "P0.71" in card, card
    assert "刚发生" not in card  # 无 appraisal 不出该段


def test_none_on_bad_input():
    assert build_mood_card({}, None) is None
    assert build_mood_card(None, None) is None
    assert make_card(None, None) is None


class _FakeEngine:
    def get_snapshot(self, persona_id="default"):
        return _snap()


class _FakeAppliance:
    def get_last_appraisal(self, persona_id, max_age=None):
        return {"occ_label": "love", "brief": "贴贴", "intensity": 0.7}


def test_make_card_wiring():
    card = make_card(_FakeEngine(), _FakeAppliance())
    assert card and "心动" in card and "贴贴" in card, card
    card2 = make_card(_FakeEngine(), None)
    assert card2 and "P0.71" in card2, card2
