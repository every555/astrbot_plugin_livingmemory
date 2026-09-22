"""Lightweight real-time atom extractor (v2.5).

Extracts memory atoms from individual messages using regex patterns —
no LLM call needed.  This runs on every message as it enters the store,
so time-sensitive information (reminders, plans, preferences) is captured
immediately instead of waiting for the next batch summarisation.

The output is a list of StreamAtom objects that the caller can persist
to AtomStore.
"""

from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class StreamAtomType(str, Enum):
    """Atom types extractable by the stream extractor."""
    PREFERENCE = "preference"      # user likes / dislikes something
    FACT = "fact"                  # declarative statement about self/world
    PLAN = "plan"                  # intention or scheduled action
    REMINDER = "reminder"          # explicit request to remember
    RELATIONSHIP = "relationship"  # A is B's colleague / friend / etc.


@dataclass(slots=True)
class StreamAtom:
    """A single atom extracted by the stream extractor."""
    content: str
    atom_type: StreamAtomType
    confidence: float = 0.75       # rule-based extraction gets moderate confidence
    importance: float = 0.5
    entities: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "atom_type": self.atom_type.value,
            "confidence": self.confidence,
            "importance": self.importance,
            "entities": list(self.entities),
            "metadata": dict(self.metadata),
        }


# ── Pattern definitions ──────────────────────────────────────────────

# Preference patterns
_PREFERENCE_PATTERNS = [
    re.compile(r"(?:我)?(?:比较)?喜欢(.{1,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?(?:比较)?讨厌(.{1,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?(?:比较)?不喜欢(.{1,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?偏好(.{1,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?习惯(.{1,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?最爱(.{1,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?(?:最)?不(?:想|愿意)(.{1,40}?)(?:[，。！,\.]|$)", re.S),
]

# Fact patterns
_FACT_PATTERNS = [
    re.compile(r"(?:我)?叫(.{1,20}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?是(.{2,30}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?(?:现在)?(?:在|住在)(.{2,30}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?(?:今年)?(\d{1,3})\s*岁", re.S),
    re.compile(r"(?:我)?(?:的)?(?:生日|生日是)(.{2,20}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:我)?(?:在|从事)(.{2,20}?)\s*(?:工作|上班|学习)", re.S),
]

# Plan patterns
_PLAN_PATTERNS = [
    re.compile(r"(?:我)?(?:打算|准备|计划|想要|要)(.{3,50}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:明天|后天|下周|下个月|今天|这周)(.{2,50}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(\d{1,2}月\d{1,2}日?)(.{2,40}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(\d{1,2}[:点]\d{0,2}分?)(.{2,40}?)(?:[，。！,\.]|$)", re.S),
]

# Reminder patterns
_REMINDER_PATTERNS = [
    re.compile(r"(?:记得|别忘了|不要忘记|提醒我)(.{3,60}?)(?:[，。！,\.]|$)", re.S),
    re.compile(r"(?:帮我)?记(?:一下|住)(.{3,60}?)(?:[，。！,\.]|$)", re.S),
]

# Relationship patterns
_RELATIONSHIP_PATTERNS = [
    re.compile(r"(.{1,15}?)\s*(?:是|和)\s*(.{1,15}?)\s*(?:的)?(?:同事|朋友|同学|兄弟|姐妹|夫妻|情侣|老师|学生|老板|员工)", re.S),
    re.compile(r"(.{1,15}?)\s*(?:和|跟|与)\s*(.{1,15}?)\s*(?:是)?(?:同事|朋友|同学|在?一起)", re.S),
]

# Confidence adjustments by pattern quality
_CONFIDENCE_BY_TYPE = {
    StreamAtomType.REMINDER: 0.90,     # v5.5: explicit "remember this" → 0.90 confidence
    StreamAtomType.PREFERENCE: 0.75,   # "I like X" → moderate-high
    StreamAtomType.PLAN: 0.65,         # "I plan to" → moderate (plans change)
    StreamAtomType.FACT: 0.70,         # "I am X" → moderate
    StreamAtomType.RELATIONSHIP: 0.70, # "A and B are colleagues" → moderate
}

# v5.5: Explicit memory command patterns — high priority, synchronous write
_EXPLICIT_MEMORY_PATTERNS = [
    # 直接指令: "记住X" / "记下来X" / "别忘了X" / "不要忘记X" / "记着X"
    re.compile(r"(?:记住|记下来|别忘了|不要忘记|别忘记|别忘|记着)(.{2,80}?)(?:[，。！,\.]|$)", re.S),
    # "帮我记一下X" / "帮我记住X" / "帮我记着X" / "记一下X"
    re.compile(r"(?:帮我)?记(?:一下|住|着)(.{2,80}?)(?:[，。！,\.]|$)", re.S),
    # "记得X" — 排除 "我还记得" / "我记得" 等陈述句（lookbehind 检查前一字符）
    re.compile(r"(?<![我还])记得(.{2,80}?)(?:[，。！,\.]|$)", re.S),
    # English
    re.compile(r"remember\s+(.{3,80}?)(?:[,\.!]|$)", re.S | re.I),
]


def detect_explicit_memory(content: str) -> tuple[bool, str]:
    """v5.5: Detect if the user explicitly asked to remember something.

    Returns (is_explicit, extracted_content).
    The extracted_content is the thing the user wants remembered.
    """
    if not content or not content.strip():
        return False, ""
    for pat in _EXPLICIT_MEMORY_PATTERNS:
        m = pat.search(content)
        if m:
            text = m.group(1).strip()
            if len(text) >= 2:
                return True, text
    return False, ""

# Importance adjustments
_IMPORTANCE_BY_TYPE = {
    StreamAtomType.REMINDER: 0.8,
    StreamAtomType.PREFERENCE: 0.6,
    StreamAtomType.PLAN: 0.7,
    StreamAtomType.FACT: 0.5,
    StreamAtomType.RELATIONSHIP: 0.55,
}


def _extract_entities(text: str) -> list[str]:
    """Simple entity extraction: capitalized words, known name patterns."""
    entities = []
    # Chinese names (2-4 chars, common surnames)
    name_match = re.findall(r"[\u4e00-\u9fa5]{2,4}", text)
    # Filter to likely names (short, not common words)
    common_words = {"的", "是", "在", "和", "跟", "与", "了", "也", "都", "不", "这", "那", "我", "你", "他", "她"}
    for m in name_match:
        if m not in common_words and 2 <= len(m) <= 4:
            entities.append(m)
    return entities[:5]  # cap at 5


def _deduplicate(atoms: list[StreamAtom]) -> list[StreamAtom]:
    """Remove near-duplicate atoms within the same extraction batch."""
    seen = set()
    result = []
    for atom in atoms:
        key = atom.content.strip().lower()
        if key not in seen:
            seen.add(key)
            result.append(atom)
    return result


def extract_from_message(
    content: str,
    role: str = "user",
    session_id: str | None = None,
) -> list[StreamAtom]:
    """Extract memory atoms from a single message.

    Args:
        content: The message text.
        role: "user" or "assistant".
        session_id: Optional session identifier for metadata.

    Returns:
        List of StreamAtom objects, possibly empty.
    """
    if not content or not content.strip():
        return []

    # Only extract from user messages (assistant messages are reflections,
    # not source-of-truth facts about the user)
    if role != "user":
        return []

    # Skip very short messages
    if len(content.strip()) < 4:
        return []

    atoms: list[StreamAtom] = []
    ts = time.time()

    def _make_atom(text: str, atom_type: StreamAtomType, pattern_idx: int) -> StreamAtom:
        cleaned = text.strip()
        if not cleaned or len(cleaned) < 2:
            return None  # type: ignore
        return StreamAtom(
            content=cleaned,
            atom_type=atom_type,
            confidence=_CONFIDENCE_BY_TYPE.get(atom_type, 0.7),
            importance=_IMPORTANCE_BY_TYPE.get(atom_type, 0.5),
            entities=_extract_entities(cleaned),
            metadata={
                "source": "stream_extractor",
                "extraction_pattern": pattern_idx,
                "session_id": session_id,
                "extracted_at": ts,
            },
        )

    # Extract preferences
    for i, pat in enumerate(_PREFERENCE_PATTERNS):
        for m in pat.finditer(content):
            atom = _make_atom(m.group(1), StreamAtomType.PREFERENCE, i)
            if atom:
                atoms.append(atom)

    # Extract facts
    for i, pat in enumerate(_FACT_PATTERNS):
        for m in pat.finditer(content):
            atom = _make_atom(m.group(1), StreamAtomType.FACT, i)
            if atom:
                atoms.append(atom)

    # Extract plans
    for i, pat in enumerate(_PLAN_PATTERNS):
        for m in pat.finditer(content):
            atom = _make_atom(m.group(0).strip(), StreamAtomType.PLAN, i)
            if atom:
                atoms.append(atom)

    # Extract reminders
    for i, pat in enumerate(_REMINDER_PATTERNS):
        for m in pat.finditer(content):
            atom = _make_atom(m.group(1), StreamAtomType.REMINDER, i)
            if atom:
                atoms.append(atom)

    # Extract relationships
    for i, pat in enumerate(_RELATIONSHIP_PATTERNS):
        for m in pat.finditer(content):
            rel_text = f"{m.group(1).strip()} 和 {m.group(2).strip()}"
            atom = _make_atom(rel_text, StreamAtomType.RELATIONSHIP, i)
            if atom:
                atoms.append(atom)

    atoms = _deduplicate(atoms)

    if atoms:
        logger.debug(
            f"[StreamExtractor] Extracted {len(atoms)} atoms from "
            f"'{content[:50]}...' (role={role})"
        )

    return atoms


__all__ = [
    "StreamAtom",
    "StreamAtomType",
    "extract_from_message",
    "detect_explicit_memory",
]
