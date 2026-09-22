"""Reinforcement state and review task models.

Adapted from DeepTutor's learning/models.py (RepetitionState, ReviewTask).
Slimmed down for memory reinforcement — we track interval progression and
strength, not knowledge points or quiz attempts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ReinforcementState:
    """Per-memory reinforcement tracking.

    Stored as JSON in memory_atoms.reinforcement_state column.

    Attributes:
        interval_index: Current position in INTERVAL_SEQUENCES (0=first review).
        consecutive_correct: How many review confirmations in a row.
        consecutive_wrong: How many "forgotten" responses in a row.
        next_review_at: Unix timestamp when next review is due.
        last_reviewed_at: Unix timestamp of last review.
        review_strength: 0.0-1.0 recency-weighted memory strength.
    """

    interval_index: int = 0
    consecutive_correct: int = 0
    consecutive_wrong: int = 0
    next_review_at: float = 0.0
    last_reviewed_at: float = 0.0
    review_strength: float = 1.0

    def to_json(self) -> str:
        import json

        return json.dumps(
            {
                "interval_index": self.interval_index,
                "consecutive_correct": self.consecutive_correct,
                "consecutive_wrong": self.consecutive_wrong,
                "next_review_at": self.next_review_at,
                "last_reviewed_at": self.last_reviewed_at,
                "review_strength": self.review_strength,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | dict[str, Any] | None) -> ReinforcementState:
        if raw is None:
            return cls()
        if isinstance(raw, dict):
            d = raw
        elif isinstance(raw, str) and raw.strip():
            import json

            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return cls()
        else:
            return cls()
        return cls(
            interval_index=int(d.get("interval_index", 0)),
            consecutive_correct=int(d.get("consecutive_correct", 0)),
            consecutive_wrong=int(d.get("consecutive_wrong", 0)),
            next_review_at=float(d.get("next_review_at", 0.0)),
            last_reviewed_at=float(d.get("last_reviewed_at", 0.0)),
            review_strength=float(d.get("review_strength", 1.0)),
        )


@dataclass
class ReviewTask:
    """A memory atom due for review.

    Attributes:
        atom_id: memory_atoms.id
        content: Memory content snippet (for the review prompt).
        strength: Current review_strength (lower = more urgent).
        due_at: Unix timestamp when review is due.
        priority: 1-5 (1 = most urgent, from error-prone or high-importance).
        state: Current ReinforcementState snapshot.
    """

    atom_id: int
    content: str
    strength: float
    due_at: float
    priority: int
    state: ReinforcementState


__all__ = ["ReinforcementState", "ReviewTask"]
