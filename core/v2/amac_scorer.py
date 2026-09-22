# -*- coding: utf-8 -*-
"""A-MAC 影子评分器（P1-④，升级路线第7条）。

A-MAC(2603.04549) 四维准入评分的家庭落地版：
- utility     基础分（security_gate.score 的 max(fact, emotion)）
- novelty     新颖度 = 1 - jaccard（近候选 bigram 重叠越高越不新颖）
- confidence  信息密度（_score_density 词典命中密度）
- recency     当日新句 1.0 / 跨日 0.5

影子模式铁律：只写 metadata["amac"]，不改 final、不自动改 GATE_THRESHOLD。
calibrate() 吃 gate.db 的 verdict 历史（confirmed vs declined），出分段
确认率和建议阈值——建议只进日志/例会，改不改橘子说了算。"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS = {"utility": 0.35, "novelty": 0.25, "confidence": 0.25, "recency": 0.15}
BINS = [(0.8, "0.8+"), (0.5, "0.5-0.8"), (0.0, "<0.5")]
CONFIRM_RATE_TARGET = 0.7
MIN_BIN_SAMPLES = 5


def score4(base_score, jaccard, density, is_today=True, weights=None):
    """四维评分（纯函数）。weights 可覆盖默认权重。"""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)
    dims = {
        "utility": round(max(0.0, min(1.0, float(base_score))), 3),
        "novelty": round(max(0.0, 1.0 - float(jaccard)), 3),
        "confidence": round(max(0.0, min(1.0, float(density))), 3),
        "recency": 1.0 if is_today else 0.5,
    }
    dims["amac_final"] = round(sum(w[k] * dims[k] for k in w), 3)
    return dims


class AMacScorer:
    """verdict 历史校准器：从 gate.db 学阈值，不替橘子做决定。"""

    def __init__(self, gate_db_path: str):
        self.gate_db_path = gate_db_path

    def calibrate(self) -> dict:
        """统计分数段确认率 + 阈值建议。库不存在/表不存在 → {} 安静降级。"""
        try:
            conn = sqlite3.connect(self.gate_db_path)
            rows = conn.execute(
                "SELECT score, status FROM gate_candidates "
                "WHERE status IN ('confirmed','declined')"
            ).fetchall()
            conn.close()
        except sqlite3.Error:
            return {}
        if not rows:
            return {}
        buckets = {label: [0, 0] for _, label in BINS}  # label -> [total, confirmed]
        for score, status in rows:
            for cut, label in BINS:
                if score >= cut:
                    buckets[label][0] += 1
                    if status == "confirmed":
                        buckets[label][1] += 1
                    break
        bins = []
        for _, label in BINS:
            total, confirmed = buckets[label]
            if total == 0:
                continue
            bins.append({
                "bin": label,
                "total": total,
                "confirmed": confirmed,
                "confirm_rate": round(confirmed / total, 3),
            })
        suggested = None
        for cut, label in BINS:
            total, confirmed = buckets[label]
            if total >= MIN_BIN_SAMPLES and confirmed / total >= CONFIRM_RATE_TARGET:
                suggested = cut
                break
        report = {"bins": bins, "samples": len(rows),
                  "confirm_rate_target": CONFIRM_RATE_TARGET}
        if suggested is not None:
            report["suggested_threshold"] = suggested
        return report

    @staticmethod
    def attach_shadow(meta: dict, base_score, jaccard, density, is_today=True) -> dict:
        """把四维写进候选 metadata（影子），保留原有键不动。"""
        try:
            meta["amac"] = score4(base_score, jaccard, density, is_today=is_today)
        except Exception as e:  # 影子失败绝不影响主链
            logger.debug(f"[AMac] 影子评分失败(忽略): {e}")
        return meta