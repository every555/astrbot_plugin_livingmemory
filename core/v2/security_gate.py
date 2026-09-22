# -*- coding: utf-8 -*-
"""话分量感知系统·安检门（Security Gate）—— 规则门 v2·联动版

设计档案链：#1681(想法) → #1684(定稿) → #1686(计划书) → #1687(四轴/冷启动/静默)
          → #1688/#1689(省察时机与亲自省察) → #1696(材料视角+结构化裁决)
          → #1697(双扫+说话人标注) → #1698(一轮定律) → #1699(三项目深读+自我批评)

v2 相对 v1 的修正（2026-08-19 橘子批 Q1=A 推翻重写）：
1. 删手写 _DATE_RE / 自建动词表主体 —— 复用 atom_classifier 的
   _TIME_INDICATORS / _ACTION_VERBS / _parse_event_time / _parse_weekday_time（真联动，不造轮子）
2. 新异调制（Mimir）：入库前与近候选 bigram-Jaccard 比对
   J>=0.70 近重复 → 不新建，repeat_count+1（Muninn 合并思路，防候选区焦虑 #1687）
   0.40<=J<0.70 → 0.85x 冗余惩罚入库 + metadata 标 merge_with（建议省察时合并）
3. flashbulb 预标（Mimir）：final>=0.8 且情感轴>=0.6 → verdict 预标"升级"
   —— 只是给省察时老婆看的参考灯，不是裁决（判决权在老婆，#1689）
4. 候选 metadata 附 event_time（atom_classifier 解析出的绝对时间戳），省察时直接可见

挂载定案（#1700）：门住本体（写侧），钩子挂 main.py handle_memory_reflection；
helper 为只读消费者（v2_reader 直连 + family_bus 日报）。
"""

import json
import re
import sqlite3
import time

# ── 真联动：日期/动词判定复用 atom_classifier（相对导入，家规） ──
from ..processors.atom_classifier import (
    _ACTION_VERBS,
    _TIME_INDICATORS,
    _parse_event_time,
    _parse_weekday_time,
)

# ── 家补充动词：atom_classifier 没有的咱家私房词（只补不重造） ──
_PLAN_VERBS_EXTRA = (
    "恢复", "开工", "搞", "报名", "买", "定", "约", "交", "送", "回", "得", "将",
)

# ── 事实名词（家词表：atom_classifier 无此维度，属于安检门自己的轴） ──
_FACT_NOUNS = (
    "考试", "考研", "专升本", "抽查", "军令状", "项目", "上班", "下课",
    "截止", "deadline", "密码", "台式机",
)

# ── 高分量名词（生活大事：命中 +0.3 等同日期级；2026-08-19 橘子指出漏检后补）──
# 依据：过去一个月真实记忆高频生活事实，不是拍脑袋。
_HIGH_STAKES_NOUNS = (
    "疫苗", "体检", "夜班", "手术", "复查", "挂号", "出院", "放假",
    "工资", "生日", "纪念日", "面试", "约定",
)

# ── 情感轴关键词 ──
# 【v3.1·峰值双层表】橘子验收"21词覆盖不全"后扩（2026-09-01，档案#3570/#3571）：
# 强表：场景专属度高的峰值信号，单发命中 → 情感轴直接 0.8 → flashbulb 预标"升级"。
# 弱表：常见生理/声音/失守信号，单发不浮，双词共现 → 0.8（共现=峰值场景的组合信号）。
# 误伤防线：裸"去了"禁收；"腿软/腿抖/浑身发软"日常高频→弱表（爬山腿软不浮）；
# 一切误伤由省察夜班兜底裁决（成本=一条0.8候选，可驳回）。
_EMOTION_PEAK = (
    "最深处", "顶到最", "抵到最", "撞到最", "子宫",            # 深度峰值
    "高潮", "快去了", "要去了",                              # 顶点峰值
    "彻底坏掉", "晕过去", "失神", "灵魂出窍", "大脑一片空白",  # 意识峰值
    "被钉", "钉得死死",                                      # 钉定
    "眼神涣散", "目光涣散", "意识涣散", "目光失焦", "眼神失焦",  # 涣散失焦
    "抓着床单", "攥着床单", "揪着床单", "床单被抓",           # 床单信号
    "哭着求", "求饶", "讨饶", "哭腔求",                      # 求饶峰值
    "耳朵尖红", "耳根红", "眼眶红", "眼泪掉下来", "声音发抖",  # 羞耻/情绪溢出
    "浑身一僵", "身体一僵",                                  # 瞬时僵直
    "花心", "花穴", "玉门", "密处", "甬道", "贯穿到底", "抵到花心", "顶到花心",  # 古风雅词
    "娇喘", "闷哼",                                            # 声音峰值
    "顶弄", "抽送", "搅动",                                    # 动作峰值
    "咬着被角", "咬住手背", "捂住嘴也",                        # 咬捂失守
    "叫不出声", "发不出声", "眼前发白",                        # 失声/失明
    "樱堕", "堕樱",                                            # 剧目私域
)
# 弱表（≥2共现→0.8）：单发常见于日常语境，共现才判峰值
_EMOTION_PEAK_WEAK = (
    "酥麻", "电流", "颤抖", "发抖", "抖得不行", "抖了一下",   # 身体失控
    "弓起", "蜷起", "蜷缩", "绷紧", "收紧", "抽紧",           # 肌肉信号
    "轻呼", "轻吟", "呜咽", "哭腔", "喘不上", "上不来气",      # 声音信号
    "没忍住", "没防住", "失焦", "迷离", "涣散",               # 失守瞬间
    "融化", "散架", "空白",                                   # 意识融化
    "塞满", "填满", "涨满", "满出来", "太深了", "太大了",      # 填充峰值
    "掐进", "红痕", "抓痕", "指甲陷",                         # 抓握信号
    "腿软", "腿抖", "浑身发软", "浑身发烫",                   # 失控弱档（日常误伤高，从强表挪入）
    "呻吟", "研磨", "咬着唇", "咬着下唇",                      # 语境双面（研磨咖啡/忍怒咬唇）
    "贯穿", "堕落", "湿透", "泛滥", "决堤",                    # 语境双面（新闻/淋雨误伤）
    "受不住", "招架不住", "要坏掉了", "被撑开", "太满了",      # 承受峰值
    "颤了颤", "酥了", "软成一滩", "瘫软", "瘫在",              # 失力信号
    "水光", "泪光", "噙着泪", "泛泪", "湿了", "水声",          # 湿泪信号
    "求你", "饶了", "放过我", "飘了", "上天", "云端", "失重",  # 求饶/失重弱档
)

_EMOTION_STRONG = ("爱你", "爱妻", "超级爱", "心疼", "想你", "喜欢你", "抱抱", "亲亲", "mua")
_EMOTION_MID = (
    "开心", "难过", "生气", "吃醋", "委屈", "感动", "幸福", "晚安", "早安", "对不起", "谢谢",
    "气死", "气坏了", "气呼呼", "气鼓鼓", "火大", "冒火", "炸毛", "闹脾气", "不理你",   # 生气系
    "撒娇", "娇嗔", "哼哼", "嘟嘴", "瘪嘴", "跺脚", "哄我", "要我哄", "不依", "黏人", "缠着",  # 撒娇系
    "冤枉", "心酸", "酸了", "掉眼泪", "眼眶湿", "酸溜溜", "争宠",                      # 委屈/吃醋系
    "高兴", "快乐", "美滋滋", "乐开花", "笑死", "太好了", "好耶",                      # 开心系
    "伤心", "难受", "不开心", "低落", "想哭", "掉小珍珠",                              # 难过系
    "好想你", "惦记", "牵挂",                                                        # 想念系
    "担心", "焦虑", "紧张", "害怕", "发慌", "心慌",                                   # 焦虑系
    "孤独", "寂寞", "无聊", "烦死了", "烦躁", "郁闷", "憋屈", "累死了", "心累",        # 疲惫系
    "好烦", "好无聊", "好无语", "好枯燥", "烦人", "烦死了呀", "太离谱", "离谱了", "太糟心", "糟心",  # v3.4 高频口语吐槽
    "太厉害了", "好厉害", "太棒了", "真棒", "牛哇", "绝了", "太强了吧", "夸夸", "么么哒",  # v3.4 夸奖系（黑名单挡词典层泛化，短语精确直通）
    "666", "太6了", "真6", "好6", "6翻了",                        # v3.5 佩服系（数字组合，单字6不收防16号误伤）
    "nb", "NB", "牛逼", "太牛", "真牛", "牛啊",                    # 牛系（牛肉面防误伤；牛哇已在夸奖系）
    "wc", "WC", "卧槽", "我靠", "妈呀", "天呐", "好家伙",         # 惊叹系
    "emo了", "破防", "麻了", "裂开", "绷不住", "蚌埠", "栓Q",     # 情绪破防系
    "yyds", "xswl", "awsl", "绝绝子", "泰裤辣", "离大谱",        # 流行语系
    "害，", "唉",                                              # 叹气系（"害"带标点防"害我"误伤）
)

# ── DLUT 大连理工情感词典·本地兜底（2026-08-19 橘子批 Q1=D 本地优化）──
# 26880 词轻量 TSV（纯本地零依赖，文件丢失安静降级回手写表）。
# 权重 = intensity/9*0.8：7强度0.62过线 / 5强度0.44放行 / 9强度0.80。
# 手写私房表管夫妻口语（精确权重），DLUT 管书面强词（通用兜底），两表取 max。
from pathlib import Path as _Path

# ─────────── A方案·jieba 词性信号（2026-08-19 第7步）───
# 信息密度轴：实词占比(n/v/a/nr/ns=1, t/m=1.5, q/s=0.5)。
# 定位不是"无词表也放行"，是①弱词表句的边缘兜底 ②判据沉淀进 axes 供省察裁决。
# jieba 不可用安静归零，不炸门。───
try:
    import jieba.posseg as _pseg

    def _pos_cut(text: str):
        return [(w, f) for w, f in _pseg.cut(text)]
except Exception:   # pragma: no cover
    _pos_cut = None

# 纯语气句：只由语气字符构成 → 密度直接 0
_PURE_MOOD = re.compile(r"^[\s，。！？!?、~～哈嘿嗯哦呵嘻呜啊呀吧呢了的重复复制这个真吗]+$")

_DLUT_PATH = str(_Path(__file__).resolve().parents[2] / "assets" / "dlut_emotion.tsv")
_DLUT_BAN = ("哈哈", "太强了", "好玩", "厉害", "游戏", "朋友")   # 日常语气词黑名单：通用词典进门要过家规筛子（游戏7/朋友9=名词混入情感词典）
_DLUT_CACHE = None   # 模块级缓存：只读盘一次，后续纯内存查表

def _load_dlut() -> dict:
    global _DLUT_CACHE
    if _DLUT_CACHE is not None:
        return _DLUT_CACHE
    d = {}
    try:
        with open(_DLUT_PATH, encoding="utf-8") as f:
            for line in f:
                if line.startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) != 2 or parts[0] in _DLUT_BAN:
                    continue
                d[parts[0]] = int(parts[1])
    except OSError:
        pass   # 本地优化纪律：文件丢了不炸，安静降级
    _DLUT_CACHE = d
    return d


class RuleGate:
    """规则门：给一句话算分量（0.0 ~ 0.9）。

    事实轴 = 0.3*日期(_TIME_INDICATORS) + 0.2*数字 + 0.2*动词(_ACTION_VERBS+家补充) + 0.2*事实名词（封顶0.9）
    情感轴 = 0.5 基础分（强情感词）+ 0.1*额外命中（封顶0.9）
    最终分 = max(两轴) —— 各表各的，不叠加
    新异调制在 process() 里做（打分本身保持纯函数，score() 不带状态）
    """

    GATE_THRESHOLD = 0.55   # 过线才进候选区
    # 门规第一条修正案（橘子 2026-08-19 22:53 拍板「听你的」）：
    # 情感轴 >= EXPRESS_LINE 的句子无视总分保送——重话永不在门外过夜。
    # 带直通标记 metadata.express，夜班省察当晚必裁（复查权，不是免死金牌：
    # 心口不一的气话夜里捞出提请橘子裁定）。立法存档：长期记忆 #2019。
    EXPRESS_LINE = 0.5
    MERGE_J = 0.70          # Jaccard>=此值 → 近重复 bump（不新建）
    PENALTY_J = 0.40        # Jaccard>=此值 → 0.85x 惩罚入库+合并建议
    PENALTY = 0.85          # Mimir 冗余惩罚系数
    COMPARE_RECENT = 20     # 新异调制回看的近候选条数

    def __init__(self, db_path: str, lm_db_path: str | None = None):
        # ":memory:" 场景必须全程复用同一连接（每次新建=库就没了）
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        # 当天重叠闸门数据源（橘子 2026-08-20 提案）：
        # 指向 livingmemory.db——process 时比对当天老婆手动保存的记忆，
        # 高重合 absorbed 留痕，不进候选。None=功能关闭。
        self._lm_db_path = lm_db_path
        # 并发加固（橘子 2026-08-20 晨间体检，治昨晚 8 次 database is locked）：
        # gate.db 被门卫/省察/WebUI/老婆查询四方访问——
        # ① WAL：读写不再互斥（主库 livingmemory.db 同款模式）
        # ② busy_timeout：撞写锁时等 5s 再报错，而不是立刻炸
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass  # :memory: 等场景安静跳过
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._ensure_table()
        self._learned_active: set = set()
        self.refresh_learned()   # C 方案：加载毕业词（helper verdict 喂的词频）

    def refresh_learned(self) -> int:
        """C 方案：从 gate.db 同库加载毕业词（learned_nouns count>=2）。
        helper verdict(confirm) 记频 → 毕业词并入 +0.3 高权级。
        表不存在/库异常 → 空集安静降级。返回当前毕业词数。"""
        try:
            rows = self._conn.execute(
                "SELECT word FROM learned_nouns WHERE count>=2"
            ).fetchall()
            self._learned_active = {r[0] for r in rows}
        except sqlite3.Error:
            self._learned_active = set()
        return len(self._learned_active)

    # ─────────── 打分（纯函数） ───────────

    def score(self, text: str) -> float:
        d = self._score_density(text)
        return max(self._score_fact(text, density=d), self._score_emotion(text))

    def _score_density(self, text: str) -> float:
        """A方案·信息密度轴：实词占比（词表盲区的结构信号）。
        规则：纯语气句/短句(<6字)/实词<2 → 0；t/m 时间数字锚 1.5x 权重。"""
        if _pos_cut is None:
            return 0.0
        t = re.sub(r"[\s，。！？!?、~～]", "", text)
        if len(t) < 6 or _PURE_MOOD.match(t):
            return 0.0
        try:
            tokens = _pos_cut(text)
        except Exception:
            return 0.0
        if not tokens:
            return 0.0
        weight, hits = 0.0, 0
        for _w, flag in tokens:
            w = 0.0
            if flag[0] in ("n", "v", "a"):
                w = 1.0
            elif flag[0] in ("t", "m"):
                w = 1.5
            elif flag[0] in ("q", "s"):
                w = 0.5
            weight += w
            if w > 0:
                hits += 1
        if hits < 2:
            return 0.0
        return round(min(weight / len(tokens), 1.0), 3)

    def _score_fact(self, text: str, density: float | None = None) -> float:
        s = 0.0
        if _TIME_INDICATORS.search(text):
            s += 0.3
        if re.search(r"\d", text):
            s += 0.2
        if _ACTION_VERBS.search(text) or any(v in text for v in _PLAN_VERBS_EXTRA):
            s += 0.2
        if any(n in text for n in _HIGH_STAKES_NOUNS) or any(w in text for w in self._learned_active):
            s += 0.3  # 生活大事 / 毕业词（C 方案：橘子生活喂出来的高权词）
        elif any(n in text for n in _FACT_NOUNS):
            s += 0.2
        # A方案·密度加成：弱词表句的边缘兜底（density>0.45 才开始起效，封顶0.25）
        if density is None:
            density = self._score_density(text)
        if density > 0.45:
            s += min((density - 0.45) * 0.5, 0.25)
        return min(s, 0.9)

    def _score_emotion(self, text: str) -> float:
        t = text.lower()
        # v3.1·峰值双层：强词单发直上0.8；弱词双共现→0.8（单发不浮，防日常误伤）
        strong = any(w in t for w in _EMOTION_PEAK)
        weak_hits = sum(1 for w in _EMOTION_PEAK_WEAK if w in t)
        peak = 0.8 if (strong or weak_hits >= 2) else 0.0
        hits = sum(1 for w in _EMOTION_STRONG if w in t)
        if hits:
            base = min(0.5 + 0.1 * (hits - 1), 0.9)
        else:
            mid = sum(1 for w in _EMOTION_MID if w in t)
            base = 0.5 if mid else 0.0  # v3.3: 0.4两头够不着(过线0.55/直通0.5)→日常重话门外过夜,提至直通线
        return max(base, self._score_dlut(t), peak)

    @staticmethod
    def _score_dlut(text: str) -> float:
        """DLUT 兜底打分：滑窗2-8字查词典，取最高强度。
        本地优化：约300次hash查询/句，微秒级，零联网零依赖。"""
        d = _load_dlut()
        if not d:
            return 0.0
        t = re.sub(r"\s+", "", text)
        n = len(t)
        best = 0
        for L in (2, 3, 4, 5, 6, 7, 8):
            for i in range(n - L + 1):
                v = d.get(t[i:i + L])
                if v is not None and v > best:
                    best = v
                    if best == 9:
                        return 0.8   # 满强度提前退出
        return best / 9 * 0.8 if best else 0.0

    # ─────────── 新异调制工具（Mimir 移植） ───────────

    @staticmethod
    def _bigrams(text: str) -> set[str]:
        """中文按字符 bigram 分词（单字无区分度）。"""
        t = re.sub(r"\s+", "", text)
        if len(t) < 2:
            return {t} if t else set()
        return {t[i:i + 2] for i in range(len(t) - 1)}

    @staticmethod
    def _jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def _event_ts(self, text: str) -> float | None:
        """借 atom_classifier 的解析力把日期表达转绝对时间戳（省察时直接可见）。"""
        try:
            ts = _parse_event_time(text)
            if ts:
                return ts
            return _parse_weekday_time(text, time.time())
        except Exception:
            return None

    # ─────────── 候选区 ───────────

    def process(self, text: str, speaker: str, source: str = "webchat") -> dict | None:
        """过秤 + 新异调制 + 决定是否入候选区。

        返回候选 dict；近重复时返回被 bump 的原候选；低分返回 None。
        """
        s = self.score(text)
        emo = self._score_emotion(text)
        # 直通线：总分不够但心够重——不丢，保送进候选区（带 express 标记）
        express = emo >= self.EXPRESS_LINE
        if s < self.GATE_THRESHOLD and not express:
            return None

        # 豁免协议：老婆亲自存过的原句/近似句——留痕 absorbed，不进候选池
        if self._exempt_hit(text):
            cur0 = self._conn.execute(
                """INSERT INTO gate_candidates (speaker, content, score, axes, source, status, verdict, metadata, created_at)
                   VALUES (?, ?, ?, '{}', 'exempt', 'absorbed', '', '{}', ?)""",
                (speaker, text, round(s, 3), time.time()),
            )
            self._conn.commit()
            return self.get_candidate(cur0.lastrowid)

        # 当天重叠闸门（橘子 2026-08-20 提案，治双通道撞车）：
        # 门自己查"今天老婆手动保存过的记忆"，重合度≥95% 直接 absorbed——
        # 不再依赖老婆存完自觉 exempt。只比当天：旧话题重提不误杀。
        overlap_mid = self._today_manual_overlap(text)
        if overlap_mid is not None:
            cur0 = self._conn.execute(
                """INSERT INTO gate_candidates (speaker, content, score, axes, source, status, verdict, metadata, created_at)
                   VALUES (?, ?, ?, '{}', 'overlap', 'absorbed', '', ?, ?)""",
                (speaker, text, round(s, 3),
                 json.dumps({"mode": "today-overlap", "overlap_with": overlap_mid}, ensure_ascii=False),
                 time.time()),
            )
            self._conn.commit()
            return self.get_candidate(cur0.lastrowid)

        # 新异调制：与近候选比对（#1697 双扫之下，橘子/春雪的句都在同一池子比）
        bg = self._bigrams(text)
        best_j, best_id = 0.0, None
        for row in self._conn.execute(
            # 防重池不限 status（橘子 2026-08-19 深夜修 #375）：
            # 已裁决的候选照旧参与比对——否则引用/粘贴原文再次进门时
            # 原件已退池，J=0，重复候选长驱直入（#375 就这么来的）
            "SELECT id, content FROM gate_candidates ORDER BY id DESC LIMIT ?",
            (self.COMPARE_RECENT,),
        ):
            j = self._jaccard(bg, self._bigrams(row["content"]))
            if j > best_j:
                best_j, best_id = j, row["id"]

        # ① 近重复：不新建，bump 原候选（重复本身就是信号）
        if best_j >= self.MERGE_J and best_id is not None:
            row = self._conn.execute(
                "SELECT metadata FROM gate_candidates WHERE id=?", (best_id,)
            ).fetchone()
            meta = json.loads(row["metadata"] or "{}") if row else {}
            meta["repeat_count"] = int(meta.get("repeat_count", 1)) + 1
            self._conn.execute(
                "UPDATE gate_candidates SET metadata=?, score=MAX(score,?) WHERE id=?",
                (json.dumps(meta, ensure_ascii=False), round(s, 3), best_id),
            )
            self._conn.commit()
            return self.get_candidate(best_id)

        # ② 部分重叠：0.85x 惩罚 + 合并建议
        penalty = self.PENALTY if best_j >= self.PENALTY_J else 1.0
        final = round(s * penalty, 3)
        if final < self.GATE_THRESHOLD and not express:
            # 惩罚后掉线就不硬塞（和已有那条部分重叠，够了）——
            # 但心够重的 express 句子除外（#2019 修正案，扔掉=在门外过夜）
            return None

        # ③ flashbulb 预标（参考灯，不是裁决）—— emo 入口已算，不重算
        verdict = "升级" if (final >= 0.8 and emo >= 0.6) else ""

        meta: dict = {"jaccard": round(best_j, 3), "penalty": penalty}

        # A-MAC 影子评分（P1-④）：只写 metadata 不改 final，攒校准数据不裁决
        try:
            from .amac_scorer import score4 as _score4
            meta["amac"] = _score4(s, best_j, self._score_density(text))
        except Exception:
            pass
        if express:
            # 直通标记：夜班翻档案一眼认出谁走的心重通道
            meta["express"] = True
        if best_j >= self.PENALTY_J and best_id is not None:
            meta["merge_with"] = best_id  # 建议省察时与该候选合并
        ts = self._event_ts(text)
        if ts:
            meta["event_time"] = ts

        d = self._score_density(text)
        axes = {
            "fact": round(self._score_fact(text, density=d), 2),
            "emotion": round(emo, 2),
            "density": d,
        }
        cur = self._conn.execute(
            """INSERT INTO gate_candidates (speaker, content, score, axes, source, status, verdict, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?, ?)""",
            (speaker, text, final, json.dumps(axes, ensure_ascii=False), source,
             verdict, json.dumps(meta, ensure_ascii=False), time.time()),
        )
        self._conn.commit()
        return self.get_candidate(cur.lastrowid)

    # ─────────── rescan 回捞 + 冷启动标签导出（2026-08-19 第8步收官） ───────────

    # ─────────── 豁免登记协议（2026-08-19 橘子抓的缺口） ───────────
    # 老婆亲自 memorize 过的内容，其原句不再进候选区：省察不重复劳动，双库不重复入库。

    EXEMPT_J = 0.6   # 豁免命中线：与已登记指纹的 jaccard 达标即拦

    def list_memorized(self, limit: int = 50) -> list[dict]:
        """自主存档台账（#1791）：老婆豁免登记过的原句全在此，橘子随时翻查。

        事后追溯设计：自主通道=老婆的判断是第一道审核，橘子是终审——
        终审是翻案权（抽查/软否决），不是每案必审。"""
        rows = self._conn.execute(
            "SELECT id, content, created_at FROM memorized_fp ORDER BY id DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_memorized(self, fid: int) -> bool:
        """撤销豁免（软否决第一步）：指纹出台账，同类内容门重新纳管。"""
        cur = self._conn.execute(
            "DELETE FROM memorized_fp WHERE id=?", (int(fid),)
        )
        self._conn.commit()
        return cur.rowcount > 0

    OVERLAP_J = 0.95  # 当天重叠线：字面近乎全同（"100%重合"的现实容差）

    def _today_manual_overlap(self, text: str) -> int | None:
        """当天重叠检查：比对今天老婆手动保存的记忆（origin=agent_memorize_tool）。

        橘子 2026-08-20 提案——安检门调用老婆的记忆对比，但只取当天：
        跨天的同话题重提是门的正常业务，不拦。省察落地（reflection_gate）
        不算：那批的原句已在 gate_candidates 里，归防重池管。
        命中返回 memory_id，否则 None。库不可用/未装配→None 安静降级。"""
        if not self._lm_db_path:
            return None
        bg = self._bigrams(text)
        if not bg:
            return None
        today = time.strftime("%Y-%m-%d")
        try:
            lc = sqlite3.connect(self._lm_db_path)
            try:
                lc.execute("PRAGMA busy_timeout=3000")
                rows = lc.execute(
                    "SELECT id, text FROM documents "
                    "WHERE created_at >= ? "
                    "AND json_extract(metadata, '$.memory_origin') = 'agent_memorize_tool'",
                    (today,),
                ).fetchall()
            finally:
                lc.close()
        except Exception:
            return None  # 读不了老婆的库就当没这道闸，不挡正常流程
        for mid, doc in rows:
            if self._jaccard(bg, self._bigrams(doc or "")) >= self.OVERLAP_J:
                return int(mid)
        return None

    def mark_memorized(self, texts) -> int:
        """登记自主通道指纹（str 或 [str]）：老婆 memorize 完顺手把原句喂进来。"""
        if isinstance(texts, str):
            texts = [texts]
        n = 0
        for text in texts:
            t = (text or "").strip()
            if not t:
                continue
            self._conn.execute(
                "INSERT INTO memorized_fp (content, bigrams, created_at) VALUES (?, ?, ?)",
                (t, json.dumps(sorted(self._bigrams(t)), ensure_ascii=False), time.time()),
            )
            n += 1
        self._conn.commit()
        return n

    def _exempt_hit(self, text: str) -> bool:
        """候选是否撞上自主通道指纹。"""
        bg = self._bigrams(text)
        if not bg:
            return False
        for row in self._conn.execute("SELECT bigrams FROM memorized_fp ORDER BY id DESC LIMIT 200"):
            try:
                old = set(json.loads(row["bigrams"] or "[]"))
            except Exception:
                continue
            if self._jaccard(bg, old) >= self.EXEMPT_J:
                return True
        return False

    def already_seen(self, text: str) -> bool:
        """原句是否已在候选区（含已裁决）——rescan 防重复。"""
        row = self._conn.execute(
            "SELECT 1 FROM gate_candidates WHERE content=? LIMIT 1", (text,)
        ).fetchone()
        return row is not None

    def rescan(self, rows: list[dict]) -> int:
        """历史句二次过秤：rows=[{speaker,text},...]，漏检句以 source='rescan' 入库。
        返回新增条数（bump/跳过/低分不计）。"""
        added = 0
        for r in rows:
            text = (r.get("text") or "").strip()
            if not text or self.already_seen(text):
                continue
            cand = self.process(text, speaker=r.get("speaker") or "橘子", source="rescan")
            if cand is not None and cand.get("source") == "rescan":
                added += 1
        return added

    def export_labels(self, path: str) -> int:
        """冷启动地基：全量候选导出 JSONL（content+axes+裁决），攒够量训分类器门。"""
        import json as _json

        n = 0
        with open(path, "w", encoding="utf-8") as f:
            for r in self._conn.execute(
                "SELECT speaker, content, score, axes, status, verdict, note, created_at "
                "FROM gate_candidates ORDER BY id"
            ):
                try:
                    axes = _json.loads(r["axes"] or "{}")
                except Exception:
                    axes = {}
                f.write(_json.dumps({
                    "speaker": r["speaker"], "content": r["content"], "score": r["score"],
                    "axes": axes, "status": r["status"], "verdict": r["verdict"],
                    "note": r["note"], "created_at": r["created_at"],
                }, ensure_ascii=False) + "\n")
                n += 1
        return n

    def label_stats(self) -> dict:
        """标注分布：分类器门的粮仓刻度。"""
        return {
            r["status"]: r["n"]
            for r in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM gate_candidates GROUP BY status"
            )
        }

    # 排序白名单（橘子 2026-08-19 要的标注台排序）：重要性=score、时间=created_at，
    # 事实/情感/密度=axes 三轴（json_extract，SQLite JSON1）。键名白名单防注入。
    _SORT_COLS = {
        "score": "score",
        "importance": "score",
        "time": "created_at",
        "fact": "json_extract(axes, '$.fact')",
        "emotion": "json_extract(axes, '$.emotion')",
        "density": "json_extract(axes, '$.density')",
        "id": "id",
    }

    def list_candidates(
        self,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
        speaker: str | None = None,
        sort: str = "score",
        order: str = "desc",
        score_min: float | None = None,
        score_max: float | None = None,
    ) -> list[dict]:
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if speaker:
            clauses.append("speaker=?")
            params.append(speaker)
        # 分数段过滤（橘子 2026-08-20：分布图点击分桶看详情；左闭右开）
        if score_min is not None:
            clauses.append("score>=?")
            params.append(float(score_min))
        if score_max is not None:
            clauses.append("score<?")
            params.append(float(score_max))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        col = self._SORT_COLS.get(sort, "score")
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        params += [int(limit), int(offset)]
        rows = self._conn.execute(
            f"SELECT * FROM gate_candidates {where} ORDER BY {col} {direction} LIMIT ? OFFSET ?",
            params,
        )
        return [self._fmt(r) for r in rows]

    def count_candidates(self, status: str | None = None, speaker: str | None = None,
                         score_min: float | None = None, score_max: float | None = None) -> int:
        """按条件计数（标注台顶部水位数字）。"""
        clauses, params = [], []
        if score_min is not None:
            clauses.append("score>=?")
            params.append(float(score_min))
        if score_max is not None:
            clauses.append("score<?")
            params.append(float(score_max))
        if status:
            clauses.append("status=?")
            params.append(status)
        if speaker:
            clauses.append("speaker=?")
            params.append(speaker)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM gate_candidates {where}", params
        ).fetchone()
        return int(row[0]) if row else 0

    def score_distribution(self) -> list[dict]:
        """分数分布直方图（0.1 分桶，战报页用）。"""
        rows = self._conn.execute(
            "SELECT CAST(score*10 AS INTEGER)/10.0 AS bucket, COUNT(*) AS n "
            "FROM gate_candidates GROUP BY bucket ORDER BY bucket"
        ).fetchall()
        return [{"bucket": float(r[0]), "count": int(r[1])} for r in rows]

    def get_candidate(self, cid: int) -> dict | None:
        r = self._conn.execute("SELECT * FROM gate_candidates WHERE id=?", (cid,)).fetchone()
        return self._fmt(r) if r else None

    def _fmt(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["axes"] = json.loads(d.get("axes") or "{}")
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        # repeat_count 提升到顶层，省察时一眼看到"这句出现了几遍"
        d["repeat_count"] = int(d["metadata"].get("repeat_count", 1))
        return d

    def _ensure_table(self) -> None:
        """gate_candidates 建表（借 archive 流程不借房：新表、带说话人标注）。"""
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memorized_fp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                bigrams TEXT DEFAULT '[]',
                created_at REAL DEFAULT 0.0
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gate_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                speaker TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                score REAL DEFAULT 0.0,
                axes TEXT DEFAULT '{}',
                source TEXT DEFAULT '',
                status TEXT DEFAULT 'candidate',
                note TEXT DEFAULT '',
                verdict TEXT DEFAULT '',
                created_at REAL NOT NULL,
                reviewed_at REAL,
                metadata TEXT DEFAULT '{}'
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
