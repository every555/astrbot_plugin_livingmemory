"""Review scheduler — adapted from DeepTutor's learning/scheduler.py.

Spaced repetition for memory atoms: schedules review prompts at
increasing intervals. Correct confirmations advance the interval;
"forgotten" responses pull it back.

The interval sequences are gentler than DeepTutor's learning intervals
because memories don't need high-frequency drilling like knowledge points.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ReinforcementState, ReviewTask

# Review intervals in days — gentler than DeepTutor's knowledge intervals.
# Memory review is about "don't silently forget" vs "master this concept."
INTERVAL_SEQUENCES: list[int] = [1, 3, 7, 14, 30, 60, 120]

# Priority weights — importance boosts priority (lower number = more urgent).
# Memories with higher importance get reviewed sooner.
_BASE_PRIORITY: int = 3


class ReviewScheduler:
    """Spaced repetition scheduler for memory reinforcement.

    Key difference from DeepTutor's SpacedRepetitionScheduler:
    - No knowledge-type-specific intervals (all memories use same sequence).
    - Importance-aware priority: high-importance memories get priority 1-2,
      low-importance get 4-5.
    - Optional debug mode (seconds instead of days) for testing.
    """

    def __init__(self, debug_mode: bool = False):
        self._debug_mode = debug_mode

    def _seconds_per_unit(self) -> float:
        return 1.0 if self._debug_mode else 86400.0

    def get_initial_state(self) -> "ReinforcementState":
        """Create initial reinforcement state for a new memory."""
        from .models import ReinforcementState

        intervals = INTERVAL_SEQUENCES
        return ReinforcementState(
            interval_index=0,
            consecutive_correct=0,
            consecutive_wrong=0,
            next_review_at=time.time() + intervals[0] * self._seconds_per_unit(),
            last_reviewed_at=0.0,
            review_strength=1.0,
        )

    def schedule_next(
        self, state: "ReinforcementState", is_correct: bool
    ) -> "ReinforcementState":
        """Schedule next review based on outcome.

        Args:
            state: Current reinforcement state.
            is_correct: True if user confirmed "remember".

        Returns:
            Updated state with new interval and next_review_at.
        """
        intervals = INTERVAL_SEQUENCES
        max_index = len(intervals) - 1

        if is_correct:
            state.consecutive_wrong = 0
            state.consecutive_correct += 1
            # Accelerate: 2 consecutive correct → jump 2 levels
            if state.consecutive_correct >= 2:
                state.interval_index += 2
                state.consecutive_correct = 0
            else:
                state.interval_index += 1
        else:
            state.consecutive_wrong += 1
            state.consecutive_correct = 0
            # Pull back: wrong answers reduce interval
            state.interval_index = max(0, state.interval_index - 1)
            # Two consecutive "forgot" → reset the counter
            if state.consecutive_wrong >= 2:
                state.consecutive_wrong = 0

        state.interval_index = max(0, min(state.interval_index, max_index))
        state.next_review_at = time.time() + intervals[state.interval_index] * self._seconds_per_unit()
        state.last_reviewed_at = time.time()

        return state

    def is_due(self, state: "ReinforcementState") -> bool:
        """Check if a memory's review is due."""
        if state.next_review_at <= 0:
            return True
        return time.time() >= state.next_review_at

    def compute_priority(self, importance: float, is_endangered: bool = False) -> int:
        """Compute review priority (1-5, 1 = most urgent).

        Endangered memories get top priority, high-importance memories are
        nudged up, low-importance memories are nudged down.
        """
        if is_endangered:
            return 1
        if importance >= 0.8:
            return 2
        if importance >= 0.5:
            return 3
        if importance >= 0.3:
            return 4
        return 5

    def get_due_reviews(
        self,
        atoms: list[dict],
        max_tasks: int = 5,
    ) -> list["ReviewTask"]:
        """Build a list of review tasks from atom records.

        Args:
            atoms: List of dicts with keys:
                id, content, importance, reinforcement_state (JSON string).
            max_tasks: Maximum number of review tasks to return.

        Returns:
            Sorted list of ReviewTask (most urgent first).
        """
        from .models import ReinforcementState, ReviewTask
        from .memory_strength import is_endangered

        tasks: list[ReviewTask] = []

        for atom in atoms:
            state = ReinforcementState.from_json(atom.get("reinforcement_state"))
            if not self.is_due(state):
                continue

            importance = float(atom.get("importance", 0.5))
            content = str(atom.get("content", ""))
            atom_id = int(atom.get("id", 0))

            endangered = is_endangered(state.review_strength)
            priority = self.compute_priority(importance, endangered)

            tasks.append(
                ReviewTask(
                    atom_id=atom_id,
                    content=content,
                    strength=state.review_strength,
                    due_at=state.next_review_at,
                    priority=priority,
                    state=state,
                )
            )

        # Sort by priority (lower = urgent), then by strength (lower = weaker)
        tasks.sort(key=lambda t: (t.priority, t.strength))
        return tasks[:max_tasks]


__all__ = ["ReviewScheduler", "INTERVAL_SEQUENCES"]
