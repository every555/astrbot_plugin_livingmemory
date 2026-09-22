"""Memory strength scoring — adapted from DeepTutor's mastery.py.

Maps a memory atom's reinforcement history to a 0..1 strength score.
Uses recency-weighted accuracy with a low-confidence cap.

Unlike DeepTutor which scores quiz answers, we score review confirmations:
- "remember" = correct (1.0)
- "forgot"    = wrong   (0.0)

The cap prevents a single confirmation from declaring a memory "strong."
"""

from __future__ import annotations

# Recency weights for the most recent review confirmations (oldest -> newest).
# Newer confirmations count more, so recovery after forgetting is rewarded.
_RECENCY_WEIGHTS: tuple[float, ...] = (0.5, 0.7, 0.85, 0.95, 1.0)

# Strength cannot exceed this until enough reviews accumulate.
_CONFIDENCE_CAP: dict[int, float] = {1: 0.5, 2: 0.8}

# Threshold: memories with strength below this are "endangered"
ENDANGERED_THRESHOLD: float = 0.3

# Threshold: memories with strength above this are "strong" (skip review)
STRONG_THRESHOLD: float = 0.7


def compute_memory_strength(confirmation_history: list[bool]) -> float:
    """Return a 0..1 strength score from a memory's review confirmations.

    Args:
        confirmation_history: per-review outcomes in chronological order.
            True = user confirmed "remember", False = user said "forgot".

    Returns:
        float: 0.0 (completely forgotten) to 1.0 (strongly remembered).
    """
    if not confirmation_history:
        return 1.0  # Never reviewed = assume strong (no evidence of weakness)

    recent = confirmation_history[-len(_RECENCY_WEIGHTS):]
    weights = _RECENCY_WEIGHTS[-len(recent):]

    score = sum(
        w * (1.0 if c else 0.0) for c, w in zip(recent, weights, strict=True)
    ) / sum(weights)

    return min(score, _CONFIDENCE_CAP.get(len(recent), 1.0))


def is_endangered(strength: float) -> bool:
    """Whether a memory needs review attention."""
    return strength <= ENDANGERED_THRESHOLD


def is_strong(strength: float) -> bool:
    """Whether a memory is safely above the review threshold."""
    return strength >= STRONG_THRESHOLD


__all__ = [
    "compute_memory_strength",
    "is_endangered",
    "is_strong",
    "ENDANGERED_THRESHOLD",
    "STRONG_THRESHOLD",
]
