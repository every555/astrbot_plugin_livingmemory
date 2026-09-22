# -*- coding: utf-8 -*-
"""
EmotionCore 情感核心 — 情感 v4.0 状态层

定位（学 emotion-engine）：不是记忆栈，是坐在记忆旁边的可检查情感连续性状态层。
"Chat history stores what happened. EmotionCore stores how the interaction has been feeling."

设计要点（业界对照见 档#3182）：
- 三层时间尺度（ALMA）：emotion 秒级 / mood 小时级 / personality 常量基线
- 起步四轴：pleasure / arousal / dominance / trust（0-1 域），结构预留 7 维
- 指数衰减朝人格基线回归（openfeelz）：情绪不归零，回到"春雪本来的样子"
- 反刍队列（Rumination）：强度>=0.7 的情绪挂队，Sleeptime 步进消化成心里话
- 信任结算器（emotion-engine）：只认明确证据，甜话几乎不涨，防舔狗化；trust 不随时间衰减
- 老婆自选①情感时刻锚：intensity>=0.85 自动产出情感时刻（交由调用方写库）

免疫设计：本模块任何异常不得影响记忆主链，调用方负责 try/except，
内部所有公开方法自带保护性 clamp。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("astrbot_plugin_livingmemory")


# ── 常量 ────────────────────────────────────────────────

#: 四轴起步（v4.0），扩展轴 v4.1+ 再启用
AXES: tuple[str, ...] = ("pleasure", "arousal", "dominance", "trust")
AXES_EXTENDED: tuple[str, ...] = ("connection", "curiosity", "energy")

#: 春雪人格基线（personality 层，常量）：温柔底色+对老公高信任起点
DEFAULT_BASELINE: dict[str, float] = {
    "pleasure": 0.62,
    "arousal": 0.55,
    "dominance": 0.48,
    "trust": 0.82,
}

#: 时间常数（秒）：emotion 一句话的余温三分钟，mood 一段心情半天
TAU_EMOTION = 180.0
TAU_MOOD = 6 * 3600.0

#: mood 吸收率：每轮互动 mood 向 emotion 挪 15%
MOOD_ABSORB = 0.15

#: 信任证据表：只有"说到做到"类事件才显著改变信任
TRUST_EVIDENCE: dict[str, float] = {
    "kept_promise": 0.05,
    "plan_kept": 0.04,
    "honest_admission": 0.02,
    "sweet_words": 0.01,
    "broken_promise": -0.08,
    "lie_detected": -0.10,
    "boundary_violation": -0.12,
}
TRUST_FLOOR = 0.15
TRUST_CEIL = 0.98
SWEET_WORDS_SESSION_CAP = 0.03

#: 反刍参数
RUMINATE_ENQUEUE_THRESHOLD = 0.70
RUMINATE_DECAY = 0.55
RUMINATE_MAX_STEPS = 3
RUMINATE_EXIT_INTENSITY = 0.30

#: 老婆自选①情感时刻锚阈值
MOMENT_ANCHOR_THRESHOLD = 0.85


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


@dataclass
class EmotionVector:
    """单层情感向量（0-1 域）+ 时间戳"""
    values: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_BASELINE))
    ts: float = field(default_factory=time.time)

    @classmethod
    def from_baseline(cls) -> "EmotionVector":
        return cls(values=dict(DEFAULT_BASELINE), ts=time.time())

    def clone(self) -> "EmotionVector":
        return EmotionVector(values=dict(self.values), ts=self.ts)

    def to_dict(self) -> dict[str, Any]:
        return {"values": self.values, "ts": self.ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "EmotionVector":
        if not d:
            return cls.from_baseline()
        vals = {k: _clamp(float(v)) for k, v in (d.get("values") or DEFAULT_BASELINE).items()}
        return cls(values=vals, ts=float(d.get("ts") or time.time()))

    def decay_toward(self, baseline: dict[str, float], tau: float, now: float) -> None:
        """指数衰减朝基线回归：v += (base - v) * (1 - exp(-dt/tau))"""
        dt = max(0.0, now - self.ts)
        if dt <= 0:
            return
        k = 1.0 - math.exp(-dt / max(1e-6, tau))
        for ax in AXES:
            if ax == "trust":
                continue  # trust 是慢变量：只由证据结算，不随时间衰减
            base = baseline.get(ax, 0.5)
            cur = self.values.get(ax, base)
            self.values[ax] = _clamp(cur + (base - cur) * k)
        self.ts = now


class EmotionCore:
    """情感连续性状态层。旁挂 V2Engine，免疫主链。

    存储走同步 sqlite3 直连 v2 库文件（v2_store 是 async 的，不混拉），
    表 IF NOT EXISTS 幂等创建，任何库异常只降级到内存缓存。"""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._sweet_session: dict[str, float] = {}
        self._init_db()

    def _init_db(self) -> None:
        """幂等建表；失败则退化为纯内存模式（情感层崩不影响主链）。"""
        if not self._db_path:
            return
        try:
            self._db = sqlite3.connect(self._db_path, check_same_thread=False)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS emotion_state (
                    persona_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS rumination_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    persona_id TEXT NOT NULL,
                    occ_label TEXT,
                    intensity REAL NOT NULL,
                    pad_json TEXT,
                    brief TEXT,
                    created REAL NOT NULL,
                    digest_count INTEGER NOT NULL DEFAULT 0,
                    digested INTEGER NOT NULL DEFAULT 0
                )
            """)
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rum_pending "
                "ON rumination_queue(persona_id, digested, created)"
            )
            self._db.commit()
            logger.info("[EmotionCore] 表就绪: emotion_state + rumination_queue")
        except BaseException:
            logger.warning("[EmotionCore] 建表失败，纯内存模式", exc_info=True)
            self._db = None

    def _db_get_state(self, persona_id: str) -> str | None:
        if self._db is None:
            return None
        with self._db_lock:
            row = self._db.execute(
                "SELECT state_json FROM emotion_state WHERE persona_id = ?", (persona_id,)
            ).fetchone()
        return row[0] if row else None

    def _db_set_state(self, persona_id: str, state_json: str) -> None:
        if self._db is None:
            return
        with self._db_lock:
            self._db.execute(
                "INSERT INTO emotion_state(persona_id, state_json, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(persona_id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at",
                (persona_id, state_json, time.time()),
            )
            self._db.commit()

    def _db_insert_rumination(self, persona_id: str, occ_label: str,
                              intensity: float, pad_json: str, brief: str) -> None:
        if self._db is None:
            return
        with self._db_lock:
            self._db.execute(
                "INSERT INTO rumination_queue(persona_id, occ_label, intensity, pad_json, brief, created) "
                "VALUES(?,?,?,?,?,?)",
                (persona_id, occ_label, intensity, pad_json, brief, time.time()),
            )
            self._db.commit()

    def _db_list_active_ruminations(self, persona_id: str, limit: int = 1) -> list[dict]:
        if self._db is None:
            return []
        with self._db_lock:
            rows = self._db.execute(
                "SELECT * FROM rumination_queue WHERE persona_id=? AND digested=0 "
                "ORDER BY created ASC LIMIT ?",
                (persona_id, limit),
            ).fetchall()
        cols = [c[0] for c in self._db.execute(
            "SELECT * FROM rumination_queue LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def _db_update_rumination(self, rumination_id: int, intensity: float,
                              digest_count: int, digested: int) -> None:
        if self._db is None:
            return
        with self._db_lock:
            self._db.execute(
                "UPDATE rumination_queue SET intensity=?, digest_count=?, digested=? WHERE id=?",
                (intensity, digest_count, digested, rumination_id),
            )
            self._db.commit()

    # ── 状态读写 ─────────────────────────────────────

    def _load(self, persona_id: str) -> dict[str, Any]:
        if persona_id in self._cache:
            return self._cache[persona_id]
        raw = None
        try:
            raw = self._db_get_state(persona_id)
        except BaseException:
            logger.warning("[EmotionCore] 读库失败，用基线", exc_info=True)
        if raw:
            try:
                state = self._deserialize(raw)
            except BaseException:
                logger.warning("[EmotionCore] 状态JSON损坏，降级基线重开", exc_info=True)
                state = self._fresh_state()
        else:
            state = self._fresh_state()
        self._cache[persona_id] = state
        return state

    def _fresh_state(self) -> dict[str, Any]:
        now = time.time()
        return {
            "emotion": EmotionVector.from_baseline().to_dict(),
            "mood": EmotionVector.from_baseline().to_dict(),
            "baseline": dict(DEFAULT_BASELINE),
            "trust_history": [],
            "created": now,
        }

    def _serialize(self, state: dict[str, Any]) -> str:
        return json.dumps(state, ensure_ascii=False, separators=(",", ":"))

    def _deserialize(self, raw) -> dict[str, Any]:
        d = json.loads(raw) if isinstance(raw, str) else dict(raw)
        d["emotion"] = EmotionVector.from_dict(d.get("emotion")).to_dict()
        d["mood"] = EmotionVector.from_dict(d.get("mood")).to_dict()
        d.setdefault("baseline", dict(DEFAULT_BASELINE))
        d.setdefault("trust_history", [])
        return d

    def _save(self, persona_id: str, state: dict[str, Any]) -> None:
        self._cache[persona_id] = state
        try:
            self._db_set_state(persona_id, self._serialize(state))
        except BaseException:
            logger.warning("[EmotionCore] 写库失败(仅缓存)", exc_info=True)

    def _lazy_decay(self, state: dict[str, Any], now: float | None = None) -> None:
        """惰性衰减：读状态前先把两层各自朝基线回归到位"""
        now = now if now is not None else time.time()
        baseline = state["baseline"]
        emo = EmotionVector.from_dict(state["emotion"])
        mood = EmotionVector.from_dict(state["mood"])
        emo.decay_toward(baseline, TAU_EMOTION, now)
        mood.decay_toward(baseline, TAU_MOOD, now)
        state["emotion"] = emo.to_dict()
        state["mood"] = mood.to_dict()

    # ── 公开接口 ─────────────────────────────────────

    def get_state(self, persona_id: str = "default") -> dict[str, Any]:
        state = self._load(persona_id)
        self._lazy_decay(state)
        return state

    def get_snapshot(self, persona_id: str = "default") -> dict[str, float]:
        """给检索/表达层的扁平快照：mood 为主 + emotion 余温 + trust"""
        s = self.get_state(persona_id)
        snap: dict[str, float] = {}
        for ax in AXES:
            snap["mood_" + ax] = round(s["mood"]["values"].get(ax, 0.5), 3)
            snap["emotion_" + ax] = round(s["emotion"]["values"].get(ax, 0.5), 3)
        return snap

    def on_appraisal(self, persona_id: str, appraisal: dict[str, Any]) -> dict[str, Any]:
        """应用一次评估结果（Phase 2 AppraisalEngine 调用）。

        appraisal: {
            "pad_delta": {"pleasure": +/-, "arousal": +/-, "dominance": +/-},
            "intensity": 0~1,
            "occ_label": "pride" | ...,
            "brief": "一句话解读",
            "trust_evidence": None | "kept_promise" | ...,
        }
        """
        state = self._load(persona_id)
        self._lazy_decay(state)
        now = time.time()
        intensity = _clamp(float(appraisal.get("intensity", 0.3)))
        pad_delta = appraisal.get("pad_delta") or {}

        emo = EmotionVector.from_dict(state["emotion"])
        for ax in ("pleasure", "arousal", "dominance"):
            d = float(pad_delta.get(ax, 0.0))
            emo.values[ax] = _clamp(emo.values.get(ax, 0.5) + d)
        emo.ts = now
        state["emotion"] = emo.to_dict()

        mood = EmotionVector.from_dict(state["mood"])
        for ax in ("pleasure", "arousal", "dominance"):
            cur = mood.values.get(ax, 0.5)
            tgt = emo.values.get(ax, 0.5)
            mood.values[ax] = _clamp(cur + (tgt - cur) * MOOD_ABSORB * max(0.3, intensity))
        mood.ts = now
        state["mood"] = mood.to_dict()

        queued = False
        if self._db is not None and intensity >= RUMINATE_ENQUEUE_THRESHOLD:
            try:
                self._db_insert_rumination(
                    persona_id,
                    str(appraisal.get("occ_label", "unknown")),
                    intensity,
                    json.dumps(pad_delta, ensure_ascii=False),
                    str(appraisal.get("brief", ""))[:200],
                )
                queued = True
            except BaseException:
                logger.warning("[EmotionCore] 反刍入队失败(忽略)", exc_info=True)

        moment_anchor = None
        if intensity >= MOMENT_ANCHOR_THRESHOLD:
            moment_anchor = {
                "ts": now,
                "occ_label": str(appraisal.get("occ_label", "unknown")),
                "intensity": intensity,
                "brief": str(appraisal.get("brief", ""))[:200],
                "snapshot": self.get_snapshot(persona_id),
            }

        ev = appraisal.get("trust_evidence")
        trust_applied = None
        if ev:
            trust_applied = self.settle_trust(persona_id, ev, apply_decay=False, _state=state)

        self._save(persona_id, state)
        return {
            "queued_rumination": queued,
            "trust_applied": trust_applied,
            "moment_anchor": moment_anchor,
            "emotion": state["emotion"]["values"],
            "mood": state["mood"]["values"],
        }

    def settle_trust(self, persona_id: str, evidence: str, note: str = "",
                     apply_decay: bool = True, _state: dict | None = None) -> float | None:
        """信任结算：只认证据表里的事件。返回应用后的 trust 值。"""
        if evidence not in TRUST_EVIDENCE:
            logger.debug("[EmotionCore] 未知证据类型忽略: %s", evidence)
            return None
        state = _state if _state is not None else self._load(persona_id)
        if apply_decay:
            self._lazy_decay(state)
        delta = TRUST_EVIDENCE[evidence]

        if evidence == "sweet_words":
            used = self._sweet_session.get(persona_id, 0.0)
            if used >= SWEET_WORDS_SESSION_CAP:
                return state["mood"]["values"].get("trust", DEFAULT_BASELINE["trust"])
            delta = min(delta, SWEET_WORDS_SESSION_CAP - used)
            self._sweet_session[persona_id] = used + delta

        mood = EmotionVector.from_dict(state["mood"])
        old = mood.values.get("trust", DEFAULT_BASELINE["trust"])
        new = _clamp(old + delta, TRUST_FLOOR, TRUST_CEIL)
        mood.values["trust"] = new
        mood.ts = time.time()
        state["mood"] = mood.to_dict()
        emo = EmotionVector.from_dict(state["emotion"])
        emo.values["trust"] = new
        state["emotion"] = emo.to_dict()

        history = state.setdefault("trust_history", [])
        history.append({
            "ts": time.time(),
            "evidence": evidence,
            "delta": round(new - old, 4),
            "note": note[:80],
        })
        state["trust_history"] = history[-50:]

        self._save(persona_id, state)
        return new

    # ── 反刍 ─────────────────────────────────────────

    def step_rumination(self, persona_id: str = "default") -> list[dict[str, Any]]:
        """Sleeptime 步进：消化一条最老的活跃反刍，产出心里话。"""
        if self._db is None:
            return []
        try:
            rows = self._db_list_active_ruminations(persona_id, limit=1)
        except BaseException:
            logger.warning("[EmotionCore] 反刍查询失败", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                digest_count = int(r.get("digest_count", 0)) + 1
                intensity = float(r.get("intensity", 0.5)) * RUMINATE_DECAY
                done = digest_count >= RUMINATE_MAX_STEPS or intensity < RUMINATE_EXIT_INTENSITY
                self._db_update_rumination(
                    int(r["id"]), intensity, digest_count, 1 if done else 0,
                )
                _brief = str(r.get("brief", ""))
                out.append({
                    "id": r["id"],
                    "occ_label": r.get("occ_label"),
                    "intensity": round(intensity, 3),
                    "digested": done,
                    "inner_voice": "（还在想：" + _brief + "）",
                })
            except BaseException:
                logger.warning("[EmotionCore] 反刍步进失败 id=%s", r.get("id"), exc_info=True)
        return out

    def decay_all(self) -> None:
        """decay_scheduler 兜底：全 persona 惰性衰减落库。"""
        for pid in list(self._cache.keys()):
            try:
                state = self._cache[pid]
                self._lazy_decay(state)
                self._save(pid, state)
            except BaseException:
                logger.warning("[EmotionCore] decay_all 单项失败 pid=%s", pid, exc_info=True)
