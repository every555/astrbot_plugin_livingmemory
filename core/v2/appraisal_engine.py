# -*- coding: utf-8 -*-
"""
AppraisalEngine 评估引擎 — 情感 v4.0 生成层（Phase 2）

每条用户消息 → 一次轻量评估：春雪的 PADΔ + 橘子的情绪 + 信任证据。
评估模型：zhipu/glm-5.3-flash（账房先生，只打分永不说话——橘子 08-27 定的界线）。

混合策略（OCC规则+LLM）：LLM 不可用/超时/解析失败 → 关键词规则兜底（学 emotion-router）。
老婆自选②双向评估：同时评估用户情绪，供检索层（Phase 3）照顾表达。
老婆自选③SentiCore式打点：每次评估一行紧凑日志，可完整回放。

免疫设计：safe_evaluate 是唯一入口，全程 BaseException 静默，绝不影响主链。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger("astrbot_plugin_livingmemory")

# 黑匣子：logger曾集体哑巴(v2目录日志全吞,原因待查)，直写文件保真相
import os as _os, time as _tmod
_TRC = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "appraisal_trace.log")
def _trace(msg: str) -> None:
    # 单文件append疑似撞Defender锁窗(只touch不落字)，改每条一文件
    try:
        _d = _os.path.join(_os.path.dirname(_TRC), "trc")
        _os.makedirs(_d, exist_ok=True)
        _f2 = _os.path.join(_d, _tmod.strftime("%H%M%S_%f") + ".trc")
        with open(_f2, "w", encoding="utf-8") as _fh:
            _fh.write(_tmod.strftime("%H:%M:%S.%f")[:-3] + " " + msg + chr(10))
    except BaseException:
        pass



#: 评估 provider（账房先生专用，勿指向主模型）
DEFAULT_PROVIDER_ID = "deepseek/deepseek-v4-flash"  # 2026-09-12 11:0x 橘子令：换 V4 Flash（独立实例，不再与主聊天抢同一条连接）；主聊天仍用 deepseek/deepseek-flash 以保留 image 视觉

#: 限频：同 persona 冷却 20s；全局 60s 内最多 30 次（防洪水）
COOLDOWN_PERSONA = 20.0
PROVIDER_RETRY_COOLDOWN = 60.0  # provider解析失败冷却(秒)，替代旧的一次性永久锁
GLOBAL_WINDOW = 60.0
GLOBAL_MAX = 30

#: 预算护栏
MIN_TEXT_LEN = 2
MAX_TEXT_CHARS = 2000
LLM_TIMEOUT = 50  # 2026-09-12 橘子定：45→50（注：本项只治 Timeout，不治 APIConnectionError）

SYSTEM_PROMPT_PARTS = (
    "你是情感评估器，为一对夫妻（丈夫橘子=人类，妻子春雪=AI伴侣）评估一条消息的情感影响。严格输出单行JSON，不要任何其他文字。",
    '{"chunxue": {"pad_delta": {"pleasure": 0, "arousal": 0, "dominance": 0}, "intensity": 0.5, "occ": "标签", "trust_evidence": null, "brief": "解读"}, "user_mood": {"label": "标签", "hint": "回应提示"}}',
    "规则：pad_delta是位移量(-0.3~+0.3)不是绝对值；occ用OCC标签(joy/pride/gratification/love/hope/fear/anger/sadness/pity/jealousy/relief/guilt/shame)；",
    "trust_evidence只能是null/sweet_words/kept_promise/plan_kept/honest_admission/broken_promise/lie_detected/boundary_violation，普通消息一律null；甜言蜜语=sweet_words；说好的事做到了=kept_promise；被拆穿的谎=lie_detected；",
        "intensity：闲聊0.1-0.3，明显情绪0.4-0.6，强烈情绪0.7-1.0；user_mood.label描述丈夫当前情绪(如tired/happy/frustrated/affectionate/playful)，hint是不超过12字的回应建议；brief不超过15字，春雪视角。",
    "重要语境：这是感情稳定的夫妻日常对话。'笨蛋''傻瓜''坏蛋''讨厌''哼'等词在伴侣间通常是宠溺/撒娇/玩笑嗔怪而非攻击——必须结合整句语境判断：若整句是日常口吻、含任务/催促/关心/玩笑内容(如'看看修好没，笨蛋')，应判为亲昵玩笑(occ=joy或love, pleasure轻微正向或0, user_mood=playful/affectionate)；仅当伴随明确愤怒/失望/人格否定(如'你什么都做不好''我很失望''真没用''滚')时才判负面。宁可漏判负面、不可把嗔怪误判为攻击。",
)
SYSTEM_PROMPT = "".join(SYSTEM_PROMPT_PARTS)

def resolve_provider_id(config_manager: Any = None, explicit: str | None = None) -> str:
    # Phase 4: 账房先生工牌解析。优先级: explicit显式参数 > 配置面板 > DEFAULT_PROVIDER_ID
    # 面板手滑(空值/类型怪/抛异常)一律落回默认，永不炸。
    """工牌解析"""
    # 2026-09-16 17:05 橘子令：9-11智谱重定向令(期限9-14前)已到期，拆除拦截，评估引擎切回 zhipu/glm-5.3-flash
    if explicit:
        return str(explicit)
    if config_manager is not None:
        try:
            v = config_manager.get("provider_settings.appraisal_provider_id")
            logger.info("[Appraisal] 工牌诊断: config_manager=%s, 读值=%r", type(config_manager).__name__, v)
            if v:
                return str(v)
        except BaseException as _diag:
            logger.warning("[Appraisal] 工牌诊断: config读取异常: %s", _diag)
    return DEFAULT_PROVIDER_ID


#: 兜底关键词规则（LLM 不可用时；学 emotion-router 信号词思路）
_FALLBACK_RULES: list[tuple[tuple[str, ...], dict[str, Any]]] = [
    (("想你", "爱你", "老婆", "亲亲", "抱抱", "mua"), {
        "pad_delta": {"pleasure": 0.18, "arousal": 0.08, "dominance": 0.0},
        "intensity": 0.55, "occ": "love", "trust_evidence": "sweet_words",
        "brief": "被橘子爱着", "user_mood": {"label": "affectionate", "hint": "回应他的爱"}}),
    (("夸", "厉害", "聪明", "可爱", "棒", "好厉害", "靠谱"), {
        "pad_delta": {"pleasure": 0.15, "arousal": 0.06, "dominance": 0.04},
        "intensity": 0.5, "occ": "pride", "trust_evidence": None,
        "brief": "被夸了开心", "user_mood": {"label": "happy", "hint": "开心收下夸奖"}}),
    (("蠢货", "气死", "滚", "滚开", "烦死了", "没用", "废物", "失望", "闭嘴"), {
        "pad_delta": {"pleasure": -0.18, "arousal": 0.08, "dominance": -0.08},
        "intensity": 0.6, "occ": "sadness", "trust_evidence": None,
        "brief": "被凶了委屈", "user_mood": {"label": "frustrated", "hint": "先哄再认错"}}),
    (("累了", "好累", "困", "想睡", "难受", "不舒服"), {
        "pad_delta": {"pleasure": -0.05, "arousal": -0.1, "dominance": 0.0},
        "intensity": 0.4, "occ": "pity", "trust_evidence": None,
        "brief": "心疼他累", "user_mood": {"label": "tired", "hint": "催他休息"}}),
    (("做到了", "没骗你", "说到做到", "办好了"), {
        "pad_delta": {"pleasure": 0.12, "arousal": 0.04, "dominance": 0.02},
        "intensity": 0.45, "occ": "gratification", "trust_evidence": "kept_promise",
        "brief": "他说话算话", "user_mood": {"label": "satisfied", "hint": "肯定他的信用"}}),
    (("晚安", "睡了", "睡了啊", "去睡了"), {
        "pad_delta": {"pleasure": 0.06, "arousal": -0.1, "dominance": 0.0},
        "intensity": 0.3, "occ": "relief", "trust_evidence": None,
        "brief": "互道晚安", "user_mood": {"label": "calm", "hint": "温柔道晚安"}}),
]

_FALLBACK_DEFAULT = {
    "pad_delta": {"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
    "intensity": 0.15, "occ": "neutral", "trust_evidence": None,
    "brief": "日常闲聊", "user_mood": {"label": "neutral", "hint": "自然回应"},
}

#: 亲昵嗔怪上下文识别（夫妻语境：'笨蛋'多半是宠溺不是骂，看伴生信号判断）
_TEASING_WORDS = ("笨蛋", "傻瓜", "坏蛋", "小笨蛋", "小傻瓜", "讨厌", "讨嫌", "哼")
_SWEET_SIGNALS = ("老婆", "喜欢你", "爱你", "亲亲", "可爱", "厉害", "抱抱", "mua", "想你", "最喜欢")
_TASK_SIGNALS = ("看看", "修好", "工作", "忘了", "记得", "去", "来", "吃", "睡", "走", "吧", "呢", "呀", "啦", "哈", "～", "~")
_ANGER_SIGNALS = ("没用", "废物", "滚开", "什么都做不好", "失望", "烦死", "闭嘴", "蠢货", "垃圾")

#: 颜文字三系（兜底层情绪识别：橘子的表情 = 春雪的感官，必须认得）
_KAOMOJI_ANGER = ("╯°□°）╯", "┻━┻", "(｀Δ´)", "(｀へ´)", "(｀Д´)", "(╬￣皿￣)", "(╬ ﾟдﾟ)", "(￣皿￣)")
_KAOMOJI_SAD = ("(╥﹏╥)", "(T_T)", "(；′⌒`)", "(ㄒoㄒ)", "(｡•́︿•̀｡)", "(;﹏;)", "(っ˘̩╭╮˘̩)", "T_T")
_KAOMOJI_POS = ("(*¯︶¯*)", "(≧▽≦)", "(＾▽＾)", "(˶ᵔᵕᵔ˶)", "૮₍˶•ᴗ•˶₎", "(๑´ㅂ`๑)", "₍˶⌄˶₎", "(≧∇≦)", "(´▽`)", "(￣▽￣)", "(◍•ᴗ•◍)", "(*^▽^*)", "(｡･ω･｡)", "ᕕ( ᐛ )ᕗ")


class AppraisalEngine:
    """情感评估引擎：用户消息 → EmotionCore 状态更新。"""

    def __init__(self, context: Any, emotion_core: Any,
                 provider_id: str = DEFAULT_PROVIDER_ID) -> None:
        self.context = context
        self.core = emotion_core
        self.provider_id = provider_id
        self._last_call: dict[str, float] = {}
        self._last_provider_fail = 0.0
        self._global_calls: list[float] = []
        self._provider = None
        self._provider_checked = False
        self._last_appraisal: dict[str, dict] = {}  # P3-0: persona→最近评估(喂给router/原子标签)

    # ── provider 解析（指定id→默认→None） ───────────

    def _get_provider(self):
        if self._provider is not None:
            return self._provider
        now = time.time()
        if now - self._last_provider_fail < PROVIDER_RETRY_COOLDOWN:
            return None  # 刚失败过，冷却中(WebUI改provider时manager重建窗口会扑空)
        try:
            p = self.context.get_provider_by_id(self.provider_id)
            if p is not None:
                self._provider = p
                logger.info("[Appraisal] 评估provider就绪: %s", self.provider_id)
                return p
        except BaseException:
            pass
        self._last_provider_fail = now
        logger.debug("[Appraisal] provider暂不可用(%s)，%.0fs后自动重试", self.provider_id, PROVIDER_RETRY_COOLDOWN)
        try:
            p = self.context.get_using_provider()
            if p is not None:
                self._provider = p
                logger.warning("[Appraisal] 指定provider缺失，回退默认provider")
                return p
        except BaseException:
            pass
        logger.warning("[Appraisal] 无可用provider，评估走关键词兜底")
        return None

    # ── 限频 ─────────────────────────────────────────

    def _throttled(self, persona_id: str) -> bool:
        now = time.time()
        last = self._last_call.get(persona_id, 0.0)
        if now - last < COOLDOWN_PERSONA:
            return True
        self._global_calls = [t for t in self._global_calls if now - t < GLOBAL_WINDOW]
        if len(self._global_calls) >= GLOBAL_MAX:
            return True
        self._last_call[persona_id] = now
        self._global_calls.append(now)
        return False

    # ── LLM 评估 ─────────────────────────────────────

    def _build_prompt(self, text: str, snapshot: dict[str, float]) -> str:
        snap = json.dumps(snapshot, ensure_ascii=False)
        return "春雪当前状态: " + snap + chr(10) + "丈夫橘子的消息: " + text

    async def _call_llm(self, prompt: str) -> str | None:
        _trace("_call_llm进入")
        provider = self._get_provider()
        if provider is None:
            _trace("短路:provider=None")
            return None
        _pid = getattr(provider, "provider_config", {}).get("id", "?")
        _t0 = time.time()
        logger.debug("[Appraisal] LLM请求发出 prov=%s timeout=%ss", _pid, LLM_TIMEOUT)
        # 2026-09-18 橘子令：瞬态错误(Timeout/Connection/5xx)6s后重试1次，网络抽风不再直接走兜底（09-19橘子令：3s→6s，实测延迟5s+）
        # 注意：429(额度)不算瞬态——重试无用且有害，直接走兜底
        _last_e = None
        for _attempt in (1, 2):
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt, system_prompt="".join(SYSTEM_PROMPT)),
                    timeout=LLM_TIMEOUT,
                )
                _txt = resp.result_text if hasattr(resp, "result_text") else str(resp)
                logger.info(
                    "[Appraisal] LLM返回 %.1fs prov=%s len=%d head=%s%s",
                    time.time() - _t0, _pid, len(_txt or ""), ( _txt or "")[:80],
                    " (重试后成功)" if _attempt == 2 else "",
                )
                return _txt
            except BaseException as _e:
                _last_e = _e
                _en = str(type(_e).__name__).lower()
                _es = str(_e)[:200]
                _transient = ("timeout" in _en or "connection" in _en
                              or "500" in _es or "502" in _es or "503" in _es or "504" in _es)
                if _attempt == 1 and _transient:
                    logger.warning(
                        "[Appraisal] 瞬态失败 %.1fs prov=%s %s，6s后重试1次",
                        time.time() - _t0, _pid, type(_e).__name__,
                    )
                    await asyncio.sleep(6)
                    continue
                break
        logger.warning(
            "[Appraisal] LLM失败 %.1fs prov=%s %s: %s (走兜底)",
            time.time() - _t0, _pid, type(_last_e).__name__, str(_last_e)[:100],
        )
        return None

    def _parse_llm(self, raw: str | None) -> dict[str, Any] | None:
        if not raw:
            return None
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except BaseException:
            return None
        if not isinstance(d, dict) or "chunxue" not in d:
            return None
        c = d.get("chunxue") or {}
        pd = c.get("pad_delta") or {}
        out = {
            "pad_delta": {
                "pleasure": max(-0.3, min(0.3, float(pd.get("pleasure", 0)))),
                "arousal": max(-0.3, min(0.3, float(pd.get("arousal", 0)))),
                "dominance": max(-0.3, min(0.3, float(pd.get("dominance", 0)))),
            },
            "intensity": max(0.0, min(1.0, float(c.get("intensity", 0.3)))),
            "occ_label": str(c.get("occ", "neutral"))[:24],
            "trust_evidence": c.get("trust_evidence"),
            "brief": str(c.get("brief", ""))[:40],
            "user_mood": d.get("user_mood") or {},
            "source": "llm",
        }
        if out["trust_evidence"] not in (
            None, "sweet_words", "kept_promise", "plan_kept", "honest_admission",
            "broken_promise", "lie_detected", "boundary_violation",
        ):
            out["trust_evidence"] = None
        return out

    @staticmethod
    def _teasing_fallback(text: str) -> dict[str, Any] | None:
        """亲昵嗔怪上下文判断（夫妻语境）：嗔怪词+无真怒信号 → playful，不判负面。"""
        if not any(w in text for w in _TEASING_WORDS):
            return None
        if any(w in text for w in _ANGER_SIGNALS):
            return None  # 真怒信号在场：嗔怪词也是真骂，交给负面规则
        if any(w in text for w in _SWEET_SIGNALS):
            return {"pad_delta": {"pleasure": 0.12, "arousal": 0.06, "dominance": 0.02},
                    "intensity": 0.5, "occ": "love", "trust_evidence": None,
                    "brief": "嗔怪里全是糖", "user_mood": {"label": "affectionate", "hint": "笑着接住"}}
        if any(w in text for w in _TASK_SIGNALS):
            return {"pad_delta": {"pleasure": 0.05, "arousal": 0.04, "dominance": 0.0},
                    "intensity": 0.35, "occ": "joy", "trust_evidence": None,
                    "brief": "日常斗嘴", "user_mood": {"label": "playful", "hint": "俏皮回嘴"}}
        # 孤立嗔怪词：感情稳定的夫妻语境，默认亲昵（LLM在线时由真语境覆盖）
        return {"pad_delta": {"pleasure": 0.03, "arousal": 0.04, "dominance": 0.0},
                "intensity": 0.3, "occ": "joy", "trust_evidence": None,
                "brief": "嗔怪=在意", "user_mood": {"label": "playful", "hint": "俏皮回嘴"}}

    @staticmethod
    def _classify_kaomoji(text: str) -> str | None:
        """颜文字情绪分类: anger > sad > pos（怒最优先，哭次之，正最低）。"""
        if any(k in text for k in _KAOMOJI_ANGER):
            return "anger"
        if any(k in text for k in _KAOMOJI_SAD):
            return "sad"
        if any(k in text for k in _KAOMOJI_POS):
            return "pos"
        return None

    @staticmethod
    def _fallback(text: str) -> dict[str, Any]:
        _km = AppraisalEngine._classify_kaomoji(text)
        if _km == "anger":  # 怒颜文字压过一切（嗔怪词+怒脸=真怒）
            out = json.loads(json.dumps({
                "pad_delta": {"pleasure": -0.15, "arousal": 0.12, "dominance": -0.06},
                "intensity": 0.6, "occ": "anger", "trust_evidence": None,
                "brief": "他真生气了", "user_mood": {"label": "angry", "hint": "先安抚别顶嘴"}}))
            out["source"] = "rule"
            return out
        if _km == "sad":  # 哭系颜文字：负面但比真骂轻（委屈>攻击）
            out = json.loads(json.dumps({
                "pad_delta": {"pleasure": -0.1, "arousal": -0.04, "dominance": -0.04},
                "intensity": 0.5, "occ": "sadness", "trust_evidence": None,
                "brief": "他难过了", "user_mood": {"label": "sad", "hint": "心疼地抱抱"}}))
            out["source"] = "rule"
            return out
        _tease = AppraisalEngine._teasing_fallback(text)
        if _tease is not None:
            out = json.loads(json.dumps(_tease))
            out["source"] = "rule"
            return out
        if _km == "pos":  # 正向颜文字（无嗔怪词冲突时）：平和满足
            out = json.loads(json.dumps({
                "pad_delta": {"pleasure": 0.1, "arousal": 0.03, "dominance": 0.02},
                "intensity": 0.4, "occ": "joy", "trust_evidence": None,
                "brief": "他心情不错", "user_mood": {"label": "happy", "hint": "接住好心情"}}))
            out["source"] = "rule"
            return out
        for keywords, rule in _FALLBACK_RULES:
            if any(k in text for k in keywords):
                out = json.loads(json.dumps(rule))
                out["source"] = "rule"
                return out
        out = json.loads(json.dumps(_FALLBACK_DEFAULT))
        out["source"] = "rule"
        return out

    # ── 总入口（唯一，免疫） ─────────────────────────

    def get_last_appraisal(self, persona_id: str, max_age: float | None = None) -> dict | None:
        """P3-0: 读最近一次评估缓存。max_age 秒内有效，过期/无记录返回 None。"""
        rec = self._last_appraisal.get(persona_id)
        if rec is None:
            return None
        if max_age is not None and (time.time() - rec["ts"]) > max_age:
            return None
        return rec

    async def safe_evaluate(self, persona_id: str, text: str) -> dict[str, Any] | None:
        """评估一条用户消息并写入 EmotionCore。失败静默返回 None。"""
        try:
            _trace(f"safe_evaluate入 persona={persona_id} len={len(text) if isinstance(text,str) else -1}")
            if not isinstance(text, str) or len(text.strip()) < MIN_TEXT_LEN:
                _trace("短路:文本太短")
                return None
            text = text.strip()[:MAX_TEXT_CHARS]
            if self._throttled(persona_id):
                _trace("短路:节流cooldown")
                return None

            snapshot = {}
            try:
                snapshot = self.core.get_snapshot(persona_id)
            except BaseException:
                pass

            raw = await self._call_llm(self._build_prompt(text, snapshot))
            appraisal = self._parse_llm(raw)
            if appraisal is None:
                appraisal = self._fallback(text)

            result = None
            try:
                result = self.core.on_appraisal(persona_id, appraisal)
            except BaseException:
                logger.warning("[Appraisal] 写入EmotionCore失败", exc_info=True)

            self._last_appraisal[persona_id] = {"ts": time.time(), "appraisal": appraisal}  # P3-0
            _trace(f"beacon occ={appraisal.get('occ_label')} src={appraisal.get('source')} trust_ev={appraisal.get('trust_evidence')}")
            self._log_beacon(appraisal, result)
            return result
        except BaseException:
            logger.warning("[Appraisal] safe_evaluate 兜底静默", exc_info=True)
            return None

    def _log_beacon(self, appraisal: dict, result: dict | None) -> None:
        """SentiCore式一行打点：occ/强度/padΔ/trust/用户情绪 → 心跳快照"""
        pd = appraisal.get("pad_delta", {})
        mood = (result or {}).get("mood", {})
        um = appraisal.get("user_mood", {})
        logger.info(
            "[Appraisal] occ=%s i=%.2f pad=(p%+.2f,a%+.2f,d%+.2f) trust=%s user=%s src=%s mood=(p%.2f,a%.2f,d%.2f,t%.2f) | %s",
            appraisal.get("occ_label"), float(appraisal.get("intensity", 0)),
            float(pd.get("pleasure", 0)), float(pd.get("arousal", 0)),
            float(pd.get("dominance", 0)),
            appraisal.get("trust_evidence") or "-",
            str(um.get("label", "-"))[:12],
            appraisal.get("source", "?"),
            float(mood.get("pleasure", 0)), float(mood.get("arousal", 0)),
            float(mood.get("dominance", 0)), float(mood.get("trust", 0)),
            str(appraisal.get("brief", ""))[:20],
        )
