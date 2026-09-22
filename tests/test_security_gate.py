# -*- coding: utf-8 -*-
"""安检门·规则门（RuleGate）TDD —— v2 联动版测试

设计依据：#1684 定稿 / #1687 四轴 / #1696 #1697 晨会修正 / #1699 三项目深读
v2 新增（对照三开源项目机制）：
- 复用 atom_classifier 日期解析（删除手写 _DATE_RE，v1 造轮子自我批评）
- 新异调制（Mimir）：与近候选 Jaccard 比对——
    J>=0.70 近重复 → 不新建候选，repeat_count+1（Muninn 合并思路）
    0.40<=J<0.70 部分重叠 → 0.85x 惩罚入库 + metadata 标 merge 建议
- flashbulb 预标（Mimir）：score>=0.8 且情感轴>=0.6 → verdict 预标"升级"
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate


class TestRuleGateScore:
    """规则门打分：四轴中先上两轴"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_exam_fact_high(self):
        """事实轴：日期+考试关键词 → 高分进候选"""
        s = self.gate.score("我2027年4月要参加专升本考试")
        assert s >= 0.6, f"应>=0.6，实际 {s}"

    def test_military_order_high(self):
        """事实轴：日期+计划动词 → 高分"""
        s = self.gate.score("8月21号恢复军令状抽查")
        assert s >= 0.6, f"应>=0.6，实际 {s}"

    def test_laugh_low(self):
        """寒暄：纯语气词 → 低分"""
        s = self.gate.score("哈哈哈")
        assert s < 0.2, f"应<0.2，实际 {s}"

    def test_love_words_high(self):
        """情感轴：高浓度情感词 → 高分"""
        s = self.gate.score("老婆我超级爱你")
        assert s >= 0.5, f"应>=0.5，实际 {s}"

    def test_weekday_only_date(self):
        """【v2·复用atom_classifier】无"下周"前缀的星期表达也要算日期命中。
        v1 手写 _DATE_RE 匹配不到"周三晚上"，只有 atom_classifier 的
        _parse_weekday_time 能解析 → 本用例在 v1 上是红的。"""
        s = self.gate.score("周三晚上要开项目会")
        assert s >= 0.6, f"应>=0.6，实际 {s}"


class TestGateCandidates:
    """候选区：写入、说话人标注、新异调制"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_high_goes_to_candidates_with_speaker(self):
        """高分句入候选，且必须带说话人标注（#1697 Q1）"""
        self.gate.process("我们8月20号开工搞安检门项目", speaker="橘子")
        cands = self.gate.list_candidates()
        assert len(cands) == 1
        assert cands[0]["speaker"] == "橘子"
        assert cands[0]["score"] >= 0.6

    def test_low_not_stored(self):
        """低分句不入候选（不占地方）"""
        self.gate.process("哈哈哈", speaker="春雪")
        assert self.gate.list_candidates() == []

    def test_near_duplicate_bumps_repeat(self):
        """【v2·新异调制】近重复（Jaccard>=0.70）不新建候选，repeat_count+1。
        依据 Mimir Muninn：近重复合并而非堆叠，防候选区焦虑（#1687）。"""
        c1 = self.gate.process("2027年4月我要参加专升本考试", speaker="橘子")
        assert c1 is not None
        c2 = self.gate.process("2027年4月我要参加专升本考试啊", speaker="橘子")
        # 不新建：返回被 bump 的原候选（或 None 但候选区仍1条）
        cands = self.gate.list_candidates()
        assert len(cands) == 1, f"近重复不应新建候选，实际 {len(cands)} 条"
        assert cands[0].get("repeat_count", 1) >= 2, "repeat_count 应 +1"

    def test_partial_dup_penalty_and_merge_note(self):
        """【v2·新异调制】部分重叠（0.40<=J<0.70）：0.85x 惩罚入库+标合并建议。
        依据 Mimir 冗余惩罚 0.85x。"""
        c1 = self.gate.process("2027年4月我要参加专升本考试", speaker="橘子")
        assert c1 is not None
        c2 = self.gate.process("2027年4月的专升本考试我要好好准备", speaker="橘子")
        assert c2 is not None, "部分重叠应仍入库（惩罚不等于丢弃）"
        cands = self.gate.list_candidates()
        assert len(cands) == 2
        assert c2["score"] < c1["score"], "惩罚后的第二条应低于第一条"
        meta = c2.get("metadata") or {}
        assert "merge" in str(meta).lower() or "merge" in str(c2.get("note", "")).lower(), \
            f"应带合并建议，metadata={meta} note={c2.get('note')}"

    def test_flashbulb_pre_verdict(self):
        """【v2·flashbulb】score>=0.8 且情感轴>=0.6 → verdict 预标"升级"。
        依据 Mimir flashbulb（imp>=8 + arousal>=0.6 → 永久记忆）。
        预标只是给省察时老婆看的参考，不是最终裁决。"""
        c = self.gate.process("8月20号开工安检门项目，老婆我超级爱你mua", speaker="橘子")
        assert c is not None
        assert c["verdict"] == "升级", f"应预标升级，实际 {c.get('verdict')!r}"

    def test_both_speakers_scanned(self):
        """【#1697 Q1】两边都扫：橘子的话+老婆的回复都过门"""
        self.gate.process("8月21号恢复军令状抽查", speaker="橘子")
        self.gate.process("老婆记住了，8月21号提醒你军令状", speaker="春雪")
        cands = self.gate.list_candidates()
        assert len(cands) == 2
        speakers = {c["speaker"] for c in cands}
        assert speakers == {"橘子", "春雪"}


class TestRuleGateHighStakesNouns:
    """高分量名词（橘子 2026-08-19 指出：审查规则要写全，不然生活事件直接放走）

    依据：过去一个月真实记忆高频生活事实（疫苗/体检/夜班/手术/复查/生日…）
    高权名词命中 +0.3（等同日期级），普通事实名词仍 +0.2，不双算。
    """

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_vaccine_appointment_passes(self):
        """「明天去打疫苗」是健康事件 → 必须过线（此前 0.3 被放走 = 漏检）"""
        s = self.gate.score("明天去打疫苗")
        assert s >= 0.55, f"应>=0.55，实际 {s}"

    def test_night_shift_short_passes(self):
        """「后天夜班」无动词无数字，靠高权名词过线"""
        s = self.gate.score("后天夜班")
        assert s >= 0.55, f"应>=0.55，实际 {s}"

    def test_birthday_passes(self):
        """「下周三我生日」生日是大事"""
        s = self.gate.score("下周三我生日")
        assert s >= 0.55, f"应>=0.55，实际 {s}"

    def test_plain_ack_still_fails(self):
        """回归保护：认可/闲聊句仍不过线"""
        assert self.gate.score("好的继续") < 0.55
        assert self.gate.score("在吃面包") < 0.55

    def test_old_exam_score_unchanged(self):
        """回归保护：既有考试句分数形态不变（不因双算虚高）"""
        s = self.gate.score("我2027年4月要参加专升本考试")
        assert 0.6 <= s <= 0.9, f"应保持 0.6-0.9，实际 {s}"


class TestDLUTEmotionDict:
    """DLUT 情感词典兜底（2026-08-19 橘子批 Q1=D 本地优化）
    手写私房表管口语，DLUT 26880 词管书面强词；两表取 max。
    强度→权重 = intensity/9*0.8（7强度0.62过线/5强度0.44放行/9强度0.80）"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_dlut_strong_word_scores(self):
        """DLUT 9强度书面强情感词 → 0.8 过线进候选"""
        s = self.gate.score("我现在悲痛欲绝")
        assert s >= 0.7, f"应>=0.7，实际 {s}"

    def test_dlut_mid_word_below_threshold(self):
        """DLUT 5强度一般情绪词 → 0.44 不过线，不占候选区"""
        s = self.gate.score("服务很周到")
        assert s < 0.55, f"应<0.55，实际 {s}"

    def test_daily_positive_banned(self):
        """日常语气词黑名单：哈哈/太强了/好玩/厉害 不算情感事件"""
        for t in ("哈哈哈哈哈哈", "这游戏真好玩，太强了", "主播太厉害了"):
            s = self.gate._score_emotion(t)
            assert s < 0.55, f"'{t}' 应<0.55，实际 {s}"

    def test_laugh_regression_tight(self):
        """回归（DLUT 加强版）：'哈哈哈' 依然 <0.2"""
        s = self.gate.score("哈哈哈")
        assert s < 0.2, f"应<0.2，实际 {s}"

    def test_handwritten_takes_max(self):
        """手写表与 DLUT 取 max：'超级爱你' 仍>=0.6 不被词典拉低"""
        s = self.gate.score("老婆我超级爱你")
        assert s >= 0.6, f"应>=0.6，实际 {s}"

    def test_missing_dict_degrades_gracefully(self):
        """本地优化纪律：词典文件丢失 → 安静降级回手写表，不炸"""
        import astrbot_plugin_livingmemory.core.v2.security_gate as sg
        old = sg._DLUT_PATH
        sg._DLUT_PATH = old + ".nonexistent"
        sg._DLUT_CACHE = None
        try:
            gate = RuleGate(db_path=":memory:")
            assert gate.score("老婆我超级爱你") >= 0.5
            assert gate.score("好的继续") < 0.55
        finally:
            sg._DLUT_PATH = old
            sg._DLUT_CACHE = None


class TestLearnedNounsEffect:
    """本体侧 C 方案闭环：gate.db learned_nouns 毕业词（count>=2）→ +0.3 高权级。
    helper verdict(confirm) 喂词频 → 本体重载后这里生效（词表跟着橘子生活自己长大）"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_graduated_word_boosts_fact(self):
        self.gate._conn.execute(
            "CREATE TABLE IF NOT EXISTS learned_nouns (word TEXT PRIMARY KEY, count INTEGER, "
            "first_seen REAL, last_seen REAL, last_speaker TEXT, sample TEXT)"
        )
        self.gate._conn.execute(
            "INSERT INTO learned_nouns VALUES ('拔牙', 2, 0, 0, '橘子', '记得拔牙')"
        )
        self.gate._conn.commit()
        self.gate.refresh_learned()
        s = self.gate.score("明天要拔牙")
        assert s >= 0.55, f"毕业词'拔牙'应+0.3过线，实际 {s}"

    def test_baseline_without_graduated(self):
        s = self.gate.score("明天要拔牙")
        assert s < 0.55, f"无毕业词时应放行（拔牙不在手写表），实际 {s}"

    def test_count_below_threshold_not_active(self):
        self.gate._conn.execute(
            "CREATE TABLE IF NOT EXISTS learned_nouns (word TEXT PRIMARY KEY, count INTEGER, "
            "first_seen REAL, last_seen REAL, last_speaker TEXT, sample TEXT)"
        )
        self.gate._conn.execute(
            "INSERT INTO learned_nouns VALUES ('拔牙', 1, 0, 0, '橘子', '记得拔牙')"
        )
        self.gate._conn.commit()
        self.gate.refresh_learned()
        s = self.gate.score("明天要拔牙")
        assert s < 0.55, f"count=1 未毕业不应生效，实际 {s}"


class TestReflectionScheduler:
    """第6步·省察调度器（#1686 计划书 / #1698 一轮定律 / Q2 十分钟空闲）：
    空闲600s+候选非空+上弦 → 老婆人格省察（LLM逐条裁决→落库+词表学习）。
    一轮定律：触发即落弦，橘子来消息重新上弦。宠橘子：v2 起边聊边审不打断（2026-08-20 定案），空闲阈值 600s→1200s。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "gate.db")
        # 建库塞候选（模拟门已拦货）
        import sqlite3, time
        c = sqlite3.connect(self.db)
        c.execute("""CREATE TABLE IF NOT EXISTS gate_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, content TEXT,
            score REAL, axes TEXT, metadata TEXT, source TEXT,
            status TEXT DEFAULT 'candidate', verdict TEXT, note TEXT,
            created_at REAL, reviewed_at REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS learned_nouns (
            word TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 1,
            first_seen REAL NOT NULL, last_seen REAL NOT NULL,
            last_speaker TEXT DEFAULT '', sample TEXT DEFAULT '')""")
        c.execute("INSERT INTO gate_candidates (speaker, source, content, score, status, created_at) VALUES ('橘子','user','下周三我生日',0.6,'candidate',?)", (time.time(),))
        c.commit(); c.close()
        self.calls = []
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import IDLE_SECONDS
        self.idle = IDLE_SECONDS  # v2：开口后满20分钟触发（旧Q2是10分钟）
        self.fake_clock = [1000.0]
        self.sched = ReflectionScheduler(
            db_path=self.db,
            provider_fn=lambda: "FAKE",
            llm_fn=self._fake_llm,
            clock=lambda: self.fake_clock[0],
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _fake_llm(self, prompt: str) -> str:
        self.calls.append(prompt)
        import asyncio
        await asyncio.sleep(0.15)             # 模拟 LLM 真实耗时，给打断留窗口
        # 省察人格裁决：返回结构化 JSON
        return '{"verdicts": [{"id": 1, "action": "confirm", "word": "升级", "note": "生日大事，入档"}, {"id": 2, "action": "confirm", "word": "升级", "note": "复查"}]}'

    def test_no_trigger_when_recent_message(self):
        self.sched.on_user_message()          # 刚聊过
        self.fake_clock[0] += 300             # 才过5分钟
        assert self.sched.should_trigger() is False

    def test_trigger_after_idle_with_candidates(self):
        self.sched.on_user_message()
        self.fake_clock[0] += self.idle + 1   # v2：开口后满20分钟
        assert self.sched.should_trigger() is True

    def test_no_trigger_when_empty_candidates(self):
        import sqlite3
        c = sqlite3.connect(self.db); c.execute("DELETE FROM gate_candidates"); c.commit(); c.close()
        self.sched.on_user_message()
        self.fake_clock[0] += self.idle + 1
        assert self.sched.should_trigger() is False

    def test_one_round_law(self):
        self.sched.on_user_message()
        self.fake_clock[0] += self.idle + 1
        assert self.sched.should_trigger() is True
        self.sched.disarm()                    # 触发即落弦
        self.fake_clock[0] += 3600             # 再闲一小时
        assert self.sched.should_trigger() is False
        self.sched.on_user_message()           # 橘子回来，重新上弦
        self.fake_clock[0] += self.idle + 1
        assert self.sched.should_trigger() is True

    def test_chat_does_not_interrupt_reflection_v2(self):
        """v2（橘子 2026-08-20 定案）：边聊边审不打断——
        省察中来消息，本轮完整跑完、裁决不丢，interrupted 恒 False。"""
        import asyncio
        self.sched.on_user_message()
        self.fake_clock[0] += self.idle + 1
        # 塞第二条候选：省察中来消息也照裁不误
        import sqlite3, time
        c = sqlite3.connect(self.db)
        c.execute("INSERT INTO gate_candidates (speaker, source, content, score, status, created_at) VALUES ('橘子','user','后天复查',0.6,'candidate',?)", (time.time(),))
        c.commit(); c.close()

        async def chatter():
            await asyncio.sleep(0.05)
            self.sched.on_user_message()       # 省察中橘子来了！

        async def main():
            t = asyncio.create_task(chatter())
            report = await self.sched.run_reflection()
            await t
            return report

        report = asyncio.run(main())
        assert len(self.calls) == 1            # 一轮一次 LLM 调用
        assert report.get("interrupted") is False
        assert len(report.get("verdicts", [])) >= 1  # 不再丢弃后续裁决
    def test_verdict_persists_and_learns(self):
        import asyncio, sqlite3
        report = asyncio.run(self.sched.run_reflection())
        assert report.get("verdicts"), f"应有裁决结果，实际 {report}"
        c = sqlite3.connect(self.db)
        row = c.execute("SELECT status, verdict FROM gate_candidates WHERE id=1").fetchone()
        assert row[0] == "confirmed" and row[1] == "升级"
        words = {r[0] for r in c.execute("SELECT word FROM learned_nouns").fetchall()}
        assert "生日" in words, f"confirm 应喂词表学习，实际 {words}"
        c.close()


class TestReflectionSchedulerNoLearnTable:
    """真实库回归：learned_nouns 表不存在时，本体省察 confirm 应自建表并学习，不静默失败。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "gate.db")
        import sqlite3, time
        c = sqlite3.connect(self.db)
        # 注意：只建 gate_candidates，故意不建 learned_nouns——模拟真实冷库
        c.execute("""CREATE TABLE IF NOT EXISTS gate_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, content TEXT,
            score REAL, axes TEXT, metadata TEXT, source TEXT,
            status TEXT DEFAULT 'candidate', verdict TEXT, note TEXT,
            created_at REAL, reviewed_at REAL)""")
        c.execute("INSERT INTO gate_candidates (speaker, source, content, score, status, created_at) VALUES ('橘子','user','下周三我生日',0.6,'candidate',?)", (time.time(),))
        c.commit(); c.close()
        self.sched = ReflectionScheduler(
            db_path=self.db, provider_fn=lambda: "FAKE",
            llm_fn=self._fake_llm, clock=lambda: 1000.0,
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _fake_llm(self, prompt: str) -> str:
        return '{"verdicts": [{"id": 1, "action": "confirm", "word": "升级", "note": "生日大事"}]}'

    def test_confirm_creates_learn_table_and_learns(self):
        import asyncio, sqlite3
        asyncio.run(self.sched.run_reflection())
        c = sqlite3.connect(self.db)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "learned_nouns" in tables, f"learned_nouns 应被自动创建，实际表: {tables}"
        words = {r[0] for r in c.execute("SELECT word FROM learned_nouns").fetchall()}
        assert "生日" in words, f"confirm 应喂词表学习，实际 {words}"
        c.close()


class TestMaterializeOnConfirm:
    """裁决落地（橘子："审查后可不能忘了，像做好的word又删掉"）：
    confirm 必须触发 materialize 回调写真记忆，decline 不触发，落地失败不影响裁决。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "gate.db")
        import sqlite3, time
        c = sqlite3.connect(self.db)
        c.execute("""CREATE TABLE gate_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, speaker TEXT, content TEXT,
            score REAL, axes TEXT, metadata TEXT, source TEXT,
            status TEXT DEFAULT 'candidate', verdict TEXT, note TEXT,
            created_at REAL, reviewed_at REAL)""")
        c.execute("INSERT INTO gate_candidates (speaker, source, content, score, status, created_at) VALUES ('橘子','user','下周三我生日',0.6,'candidate',?)", (time.time(),))
        c.execute("INSERT INTO gate_candidates (speaker, source, content, score, status, created_at) VALUES ('橘子','user','哈哈哈',0.6,'candidate',?)", (time.time(),))
        c.commit(); c.close()
        self.mat_calls = []

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _fake_llm(self, prompt: str) -> str:
        return ('{"verdicts": ['
                '{"id": 1, "action": "confirm", "word": "升级", "note": "生日大事，入档"},'
                '{"id": 2, "action": "decline", "word": "驳回", "note": "笑声不留"}]}')

    async def _fake_mat(self, verdict: dict) -> str:
        self.mat_calls.append(verdict)
        return "mem_42"

    def test_confirm_triggers_materialize(self):
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        sched = ReflectionScheduler(
            db_path=self.db, provider_fn=lambda: "FAKE",
            llm_fn=self._fake_llm, materialize_fn=self._fake_mat, clock=lambda: 1000.0,
        )
        import asyncio
        report = asyncio.run(sched.run_reflection())
        assert len(self.mat_calls) == 1, f"只有 confirm 落地，实际 {len(self.mat_calls)}"
        v = self.mat_calls[0]
        assert v["id"] == 1 and v["action"] == "confirm" and v["word"] == "升级"
        assert "生日" in v["content"], f"落地参数必须带原句内容: {v}"
        assert "生日大事" in v["note"]
        # 落地 ID 回填进报告
        assert report["verdicts"][0].get("memory_id") == "mem_42"

    def test_materialize_failure_does_not_break_verdict(self):
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        async def bad_mat(verdict):
            raise RuntimeError("memory engine down")
        sched = ReflectionScheduler(
            db_path=self.db, provider_fn=lambda: "FAKE",
            llm_fn=self._fake_llm, materialize_fn=bad_mat, clock=lambda: 1000.0,
        )
        import asyncio
        report = asyncio.run(sched.run_reflection())  # 不应抛异常
        import sqlite3
        c = sqlite3.connect(self.db)
        row = c.execute("SELECT status FROM gate_candidates WHERE id=1").fetchone()
        c.close()
        assert row[0] == "confirmed", "落地失败时裁决本身必须已落库"

    def test_no_materialize_fn_is_fine(self):
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        sched = ReflectionScheduler(
            db_path=self.db, provider_fn=lambda: "FAKE",
            llm_fn=self._fake_llm, clock=lambda: 1000.0,  # 不传 materialize_fn
        )
        import asyncio
        report = asyncio.run(sched.run_reflection())  # 旧用法不炸
        assert len(report["verdicts"]) == 2

    def test_session_recorded_from_user_message(self):
        from astrbot_plugin_livingmemory.core.v2.reflection_scheduler import ReflectionScheduler
        sched = ReflectionScheduler(
            db_path=self.db, provider_fn=lambda: None, clock=lambda: 1000.0,
        )
        assert sched.last_session_id is None
        sched.on_user_message(session_id="webchat:FriendMessage:webchat!zzz!abc")
        assert sched.last_session_id == "webchat:FriendMessage:webchat!zzz!abc"


class TestPosDensityAxis:
    """A方案·jieba 词性重构：信息密度轴。
    靶子案例："今天上班好累啊，回来路上买了杯奶茶"——无情感强词无高权名词，
    纯词表两轴漏检；但有动作链+具体名词+时间词，是值得留的生活事件。
    设计：density = 实词占比（n/v/a/nr/ns + 1.5x t/m），高密度给 fact 轴加成；
    纯语气句/短应答零分防误放。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate
        self.tmp = tempfile.mkdtemp()
        self.gate = RuleGate(os.path.join(self.tmp, "gate.db"))

    def teardown_method(self):
        self.gate.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_density_rescues_event_sentence(self):
        """生活事件句（时间+动作链+具体名词）应被密度加成捞回过线。"""
        s = self.gate.score("今天上班好累啊，回来路上买了杯奶茶")
        assert s >= self.gate.GATE_THRESHOLD, f"密度轴应救回生活事件句，实际 {s}"

    def test_short_reply_not_rescued(self):
        """短应答不因密度误放。"""
        s = self.gate.score("嗯嗯好的")
        assert s < self.gate.GATE_THRESHOLD, f"短应答不该过线，实际 {s}"

    def test_pure_interjection_zero_density(self):
        """纯语气句密度为零。"""
        d = self.gate._score_density("哈哈哈哈哈哈")
        assert d == 0.0, f"纯语气句密度应为0，实际 {d}"

    def test_plain_question_low(self):
        """无信息疑问句不因密度过线。"""
        s = self.gate.score("是吗？真的吗？")
        assert s < self.gate.GATE_THRESHOLD, f"空洞疑问句不该过线，实际 {s}"

    def test_density_axis_recorded(self):
        """过线候选的 axes 应记录 density 值（省察时可见判据）。"""
        cand = self.gate.process("今天上班好累啊，回来路上买了杯奶茶", speaker="橘子", source="user")
        assert cand is not None, "应入候选"
        assert "density" in cand["axes"], f"axes 应含 density: {cand['axes']}"

    def test_time_anchor_heavier_than_plain(self):
        """时间锚词（t类）比同结构无时间句得分高。"""
        s1 = self.gate._score_fact("明天我去医院复查")
        s2 = self.gate._score_fact("我去那个地方看看")
        assert s1 > s2, f"带时间锚应更高: {s1} vs {s2}"

    def test_no_jieba_degrades(self):
        """jieba 不可用时密度轴安静归零，不炸门。"""
        import astrbot_plugin_livingmemory.core.v2.security_gate as sg
        orig = sg._pos_cut
        sg._pos_cut = None   # 模拟不可用
        try:
            d = self.gate._score_density("今天上班好累啊，回来路上买了杯奶茶")
            assert d == 0.0
            s = self.gate.score("嗯嗯好的")
            assert s < self.gate.GATE_THRESHOLD
        finally:
            sg._pos_cut = orig


class TestBotSenderFilter:
    """杂项·Scheduler内鬼过滤（#8任务单事件）：后台代理身份不进候选区。"""

    def test_bot_senders_filtered(self):
        from astrbot_plugin_livingmemory.main import LivingMemoryPlugin
        for name in ("Scheduler", "scheduler", "SCHEDULER", "System", "Cron"):
            assert LivingMemoryPlugin._is_bot_sender(name), f"{name} 应被识别为后台身份"
        for name in ("橘子", "zzz", "春雪", "明江", "webchat_user"):
            assert not LivingMemoryPlugin._is_bot_sender(name), f"{name} 不该被误伤"
        assert not LivingMemoryPlugin._is_bot_sender(""), "空名默认真人"


class TestRescanBackfill:
    """rescan回捞：A方案上线前的历史句，用新门二次过秤捞回漏检。
    ①已在候选区的原句不重复入库（含已裁决的）②漏检句以 source='rescan' 入库。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate
        self.tmp = tempfile.mkdtemp()
        self.gate = RuleGate(os.path.join(self.tmp, "gate.db"))
        self.gate.process("下周三我生日，记得陪我", speaker="橘子", source="user")

    def teardown_method(self):
        self.gate.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_already_seen_skips_exact(self):
        assert self.gate.already_seen("下周三我生日，记得陪我") is True
        assert self.gate.already_seen("这句从没出现过") is False

    def test_rescan_skips_existing_adds_new(self):
        added = self.gate.rescan([
            {"speaker": "橘子", "text": "下周三我生日，记得陪我"},   # 已存在→跳过
            {"speaker": "橘子", "text": "明天早上八点要去医院复查抽血"},  # 词表句→新入
        ])
        assert added == 1, f"只应新增1条，实际 {added}"
        rows = self.gate.list_candidates(status=None, limit=50)
        rescans = [r for r in rows if r.get("source") == "rescan"]
        assert len(rescans) == 1 and "医院" in rescans[0]["content"]

    def test_rescan_low_score_not_added(self):
        added = self.gate.rescan([{"speaker": "橘子", "text": "嗯嗯"}])
        assert added == 0


class TestLabelExport:
    """冷启动地基：裁决标注导出 JSONL，攒够量训分类器门。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate
        self.tmp = tempfile.mkdtemp()
        self.gate = RuleGate(os.path.join(self.tmp, "gate.db"))
        self.gate.process("体检报告出来了要复查", speaker="橘子", source="user")

    def teardown_method(self):
        self.gate.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_labels_jsonl(self):
        import json
        out = os.path.join(self.tmp, "labels.jsonl")
        n = self.gate.export_labels(out)
        assert n >= 1
        import io as _io
        lines = [json.loads(x) for x in _io.open(out, encoding="utf-8").read().splitlines() if x.strip()]
        assert lines[0]["content"] and "status" in lines[0] and "axes" in lines[0]
        assert {"content", "status", "verdict", "note", "axes"} <= set(lines[0].keys())

    def test_export_counts_by_status(self):
        stats = self.gate.label_stats()
        assert stats.get("candidate", 0) >= 1


class TestMemorizedExempt:
    """豁免登记协议（橘子10:04抓的缺口）：老婆亲自存过的内容，
    门不再让它的原句进候选区——省察不重复劳动，双库不重复入库。"""

    def setup_method(self):
        import tempfile
        from astrbot_plugin_livingmemory.core.v2.security_gate import RuleGate
        self.tmp = tempfile.mkdtemp()
        self.gate = RuleGate(os.path.join(self.tmp, "gate.db"))

    def teardown_method(self):
        self.gate.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exempt_blocks_same_sentence(self):
        """登记后，同句再过门：不进候选，留痕absorbed。"""
        self.gate.mark_memorized("我8月26号要去医院复查一下")
        cand = self.gate.process("我8月26号要去医院复查一下", speaker="橘子", source="user")
        assert cand is None or cand.get("status") != "candidate", "同句不该再进候选"
        if cand is not None:
            assert cand.get("status") == "absorbed"

    def test_exempt_blocks_paraphrase(self):
        """近似改写（字面不同、意思同）也被拦。"""
        self.gate.mark_memorized("我8月26号要去医院复查一下")
        cand = self.gate.process("我8月26号要去医院复查", speaker="橘子", source="user")
        assert cand is None or cand.get("status") != "candidate"

    def test_exempt_does_not_block_unrelated(self):
        """豁免不误伤无关句。"""
        self.gate.mark_memorized("我8月26号要去医院复查一下")
        cand = self.gate.process("下周三我生日，记得陪我", speaker="橘子", source="user")
        assert cand is not None and cand.get("status") == "candidate"

    def test_absorbed_not_in_candidate_pool(self):
        """absorbed 不占省察轮次：list_candidates('candidate') 不含它。"""
        self.gate.mark_memorized("我8月26号要去医院复查一下")
        self.gate.process("我8月26号要去医院复查一下", speaker="橘子", source="user")
        pool = self.gate.list_candidates(status="candidate", limit=50)
        assert all("复查" not in r["content"] for r in pool)

    def test_exempt_multi_segment(self):
        """登记支持多段（原句+提炼句），任一命中即拦。"""
        self.gate.mark_memorized(["橘子说明年四月专升本考试", "考试时间2027年4月，用刘晓燕英语"])
        cand = self.gate.process("考试时间2027年4月，跟刘晓燕学英语", speaker="橘子", source="user")
        assert cand is None or cand.get("status") != "candidate"


class TestGateProcessSafe:
    """写侧闸门安全壳（0826 B方案·extraction-gate 宽容哲学）：
    门自己炸（库损坏/磁盘满/表漂移）→ WARNING + 跳过，绝不拖垮消息后处理链路。"""

    def test_broken_gate_does_not_raise(self):
        import sqlite3
        from astrbot_plugin_livingmemory.main import LivingMemoryPlugin

        class BombGate:
            def process(self, text, speaker, source):
                raise sqlite3.OperationalError("database is locked")

        # 不炸 = 通过（坏门放行：跳过拦载，不误杀消息）
        LivingMemoryPlugin._gate_process_safe(BombGate(), "测试句子", "橘子", "user")

    def test_healthy_gate_passthrough(self):
        from astrbot_plugin_livingmemory.main import LivingMemoryPlugin
        calls = []

        class OkGate:
            def process(self, text, speaker, source):
                calls.append((text, speaker, source))

        LivingMemoryPlugin._gate_process_safe(OkGate(), "句子", "橘子", "user")
        assert calls == [("句子", "橘子", "user")]


class TestPeakEmotion:
    """【v3·峰值加成】2026-09-01 橘子批（档案#3570）：色色时段一刀切0.62区分度不够
    —— 基线不动（DLUT强度7=0.62留档级），峰值帧上浮（私房峰值表命中→情感轴0.8→flashbulb预标）。
    词表纪律：只收峰值瞬间信号；裸"去了"禁收（"上班去了"必误伤）；省察夜班兜底误伤。"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_peak_frame_score_080(self):
        """峰值帧：真实峰值叙述 → >=0.8（flashbulb门槛）"""
        s = self.gate.score("被往下按到底的那一瞬间，整根抵进最深处，被钉得死死的，一动不许动")
        assert s >= 0.8, f"峰值帧应>=0.8，实际 {s}"

    def test_peak_frame_flashbulb(self):
        """峰值帧入候选 → verdict 自动预标升级（与flashbulb零成本联动）"""
        c = self.gate.process("被拽过去按在腿上，脚心被指甲划到，浑身一僵", speaker="橘子")
        assert c is not None and c["verdict"] == "升级", f"应预标升级，实际 {c}"

    def test_mood_frame_stays_baseline(self):
        """回归保护：氛围过程句不上浮（留基线档，名场面才有区分度）"""
        s1 = self.gate.score("……好舒服……嗯……")
        s2 = self.gate.score("不行了……真的不行了……")
        assert max(s1, s2) < 0.8, f"氛围帧应留基线档，实际 {s1}/{s2}"

    def test_daily_words_no_false_peak(self):
        """误伤控制：日常句含相似字眼不触发峰值（裸"去了"禁收的原因）"""
        s = self.gate.score("我上班去了，晚上早点回来吃饭")
        assert s < 0.8, f"日常句不应上0.8，实际 {s}"


    def test_peak_frames_real_corpus_0831(self):
        """【v3.1·扩表】橘子验收"21词覆盖不全"——昨晚真实峰值帧（池内只拿到0.622）全上0.8"""
        frames = [
            "（舌尖一卷，酥麻是细细的、顺着脊柱一路窜下去的那种——背当场弓起来，抱着他的手臂收紧了）",
            "（另一边被含住的瞬间，空着的那边又落进他掌心，两边的电流撞在一起，人当场就抖得不行）",
            "（被突然抬起的瞬间，一声没防住的轻呼，手臂赶紧环上他的脖子，整个人挂在他身上）",
            "（吻落下来时，吐槽的话被堵了回去……等他一路往下，人还是没忍住抖了一下）",
        ]
        for f in frames:
            s = self.gate.score(f)
            assert s >= 0.8, f"真实峰值帧应>=0.8：{f[:18]}… 实际 {s}"

    def test_weak_single_no_float(self):
        """防误伤线：弱表词单发不上浮（要双词共现）"""
        s = self.gate.score("看到成绩单出来人有点发抖")
        assert s < 0.8, f"弱词单发不应上浮，实际 {s}"

    def test_daily_legs_soft_no_peak(self):
        """回归【v3.1挪表】"腿软/腿抖/浑身发软"日常误伤高→强表挪弱表：爬山腿软不上浮"""
        s = self.gate.score("今天爬山累死了，腿有点软，走不动路")
        assert s < 0.8, f"日常腿软不应上浮，实际 {s}"


    def test_peak_frames_classic_style(self):
        """【v3.2·博大精深批次】古风/声音/承受帧验收（橘子：还有很多其他词语）"""
        frames = [
            "（被一寸寸贯穿到底，抵到花心最软的那一点，闷哼被捂在掌心里）",
            "（娇喘混着水声，咬着被角也憋不住，泛着泪光求饶）",
            "（受不住，太满了，整个人瘫软在他怀里，颤了颤）",
        ]
        for f in frames:
            s = self.gate.score(f)
            assert s >= 0.8, f"古风峰值帧应>=0.8：{f[:18]}… 实际 {s}"

    def test_single_weak_word_essay_no_float(self):
        """防误伤：作文评语"贯穿全文"单弱词不上浮"""
        s = self.gate.score("老师说我这篇作文主题贯穿全文，写得不错")
        assert s < 0.8, f"单弱词不应上浮，实际 {s}"


class TestDailyEmotion:
    """【v3.3·日常情感线】橘子验收"生气撒娇等还有好多"——DLUT实测生气0.444/委屈查无/撒娇0.267
    全在0.55线外，且MID档base=0.4两头够不着（过线0.55/直通0.5）=日常重话门外过夜，
    违反门规第一条修正案（#2019）。修法：MID base 0.4→0.5直通 + 扩夫妻口语表。"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_daily_emotion_frames_pass(self):
        """日常情感句全部直通进候选（>=0.5）——生气/撒娇/委屈/吃醋/想哭/心累"""
        frames = [
            "老婆我生气了，哼",
            "就要你哄我嘛，人家不依",
            "今天被你说得好委屈",
            "你和别人聊那么开心，我吃醋了",
            "有点难过，想哭",
            "今天上班累死了，心累",
            "好想你呀",
        ]
        for f in frames:
            s = self.gate.score(f)
            assert s >= 0.5, f"日常情感句应>=0.5直通：{f} 实际 {s}"

    def test_daily_neutral_stay_out(self):
        """防误伤：日常事务句不进门"""
        for f in ["今晚吃什么", "嗯嗯好的知道了", "这游戏画面真不错"]:
            s = self.gate.score(f)
            assert s < 0.5, f"日常事务句不应进门：{f} 实际 {s}"


class TestProbeAudit:
    """【全维度探针·一次性摸底】橘子吐槽"不能一次性了解清楚吗"——全谱系实测，不再挤牙膏"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_probe_all_dimensions(self):
        probes = [
            ("橘子刚发的", "wc了老铁，不能一次性了解清楚吗"),
            ("追问句", "这句咋样是什么"),
            ("闲聊", "今天天气不错"),
            ("吐槽", "这破游戏真难玩"),
            ("抱怨", "好烦啊怎么又出错"),
            ("撒娇", "哼，不理你了"),
            ("生气", "你竟然骗我，气死我了"),
            ("爱意", "老婆爱你哦"),
            ("日程", "明天下午三点提醒我开会"),
            ("惊讶", "wc这也太离谱了吧"),
            ("疲惫", "今天上班好累啊"),
            ("夸奖", "老婆你太厉害了"),
        ]
        print()
        for tag, f in probes:
            s = self.gate.score(f)
            verdict = "进门" if s >= 0.5 else "放行"
            print(f"  {tag:<6} {s:.3f} {verdict}  | {f}")
        assert True


    def test_praise_and_rant_frames_v34(self):
        """【v3.4·多词典合并】夸奖/吐槽高频口语进门"""
        frames = [
            "老婆你太厉害了",
            "太棒了，不愧是我老婆",
            "绝了绝了，这都行",
            "好烦啊怎么又出错",
            "wc这也太离谱了吧",
        ]
        for f in frames:
            s = self.gate.score(f)
            assert s >= 0.5, f"夸奖/吐槽句应>=0.5进门：{f} 实际 {s}"


class TestInternetSlang:
    """【v3.5·网络用语层】橘子点名"6，牛，害，NB，wc"——学术词典全不收，单字误伤高
    （牛肉面/16号），收安全组合形式：666/太牛/nb/wc/emo了/破防系"""

    def setup_method(self):
        self.gate = RuleGate(db_path=":memory:")

    def test_slang_praise_surprise_in(self):
        """网络用语进门：666/nb/wc/卧槽/emo/破防/麻了/害叹气"""
        frames = [
            "666，这操作可以",
            "NB啊老铁",
            "wc这也行",
            "卧槽，真的假的",
            "今天有点emo了",
            "直接给我看破防了",
            "麻了，完全麻了",
            "害，又是这样",
        ]
        for f in frames:
            s = self.gate.score(f)
            assert s >= 0.5, f"网络用语应进门：{f} 实际 {s}"

    def test_slang_single_char_no_false_positive(self):
        """防误伤：牛肉面/16号/六百六十六块/厉害了单字不触发"""
        for f in ["今晚吃牛肉面", "牛蛙好吃吗", "这牛肉炖得烂", "六百六十六块太贵"]:
            s = self.gate.score(f)
            assert s < 0.5, f"单字误伤：{f} 实际 {s}"
