"""门规第一条修正案（#2019）：情感轴直通线。
橘子 2026-08-19 22:53 拍板「听你的」——情感轴>=0.5 的句子无视总分保送候选区，
带 metadata.express 标记，夜班省察当晚必裁（复查权）。"""
import json

from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate


def _meta(cand):
    """metadata 兼容取值：内存候选是 dict，落库回读是 str。"""
    m = cand["metadata"]
    if isinstance(m, str):
        m = json.loads(m or "{}")
    return m or {}


class TestExpressLine:
    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_missing_you_passes_now(self):
        """立案原句：总分 0.50 差 0.05 曾被扔——现在心够重就保送。"""
        text = "老婆我想你了"
        s = self.gate.score(text)
        emo = self.gate._score_emotion(text)
        cand = self.gate.process(text, speaker="橘子", source="user")
        assert cand is not None, f"情感{emo}/总分{s} 应走直通线"
        meta = _meta(cand)
        assert meta.get("express") is True
        assert cand["status"] == "candidate"  # 夜班复查权：仍进候选区

    def test_plain_chatter_still_dropped(self):
        """无心的低分句照旧被扔——门规只救重话。"""
        assert self.gate.process("嗯嗯", speaker="橘子") is None
        assert self.gate.process("哦哦这样", speaker="橘子") is None

    def test_overline_no_express_mark(self):
        """过线但心不重的句子：正常进，不带 express 标。"""
        cand = self.gate.process(
            "我2027年4月要参加专升本考试", speaker="橘子", source="user"
        )
        assert cand is not None
        meta = _meta(cand)
        assert "express" not in meta

    def test_express_survives_penalty(self):
        """惩罚路径：部分重叠惩罚后掉线的 express 句子也不许扔。"""
        self.gate.process(
            "明天下午三点我们去医院复查拿药", speaker="橘子", source="user"
        )
        cand = self.gate.process(
            "我超想你呀真的很想你", speaker="橘子", source="user"
        )
        # 撞上重叠惩罚与否都该留下来：不为 None 即合规
        assert cand is not None