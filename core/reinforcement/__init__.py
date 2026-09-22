"""Memory reinforcement engine — adapted from DeepTutor learning module.

Brings active recall + spaced repetition to LivingMemory:
- Passive Ebbinghaus decay continues as normal
- When a memory's strength drops below threshold, trigger a review prompt
- User confirms "remember" → boost strength + extend interval
- User confirms "forgot"  → natural decay (no forced review)

Architecture:
    models.py         — ReinforcementState, ReviewTask dataclasses
    memory_strength.py — compute_memory_strength (recency-weighted accuracy)
    scheduler.py      — ReviewScheduler (interval sequences + review queue)
"""

from .models import ReinforcementState, ReviewTask
from .memory_strength import compute_memory_strength
from .scheduler import ReviewScheduler, INTERVAL_SEQUENCES

__all__ = [
    "ReinforcementState",
    "ReviewTask",
    "compute_memory_strength",
    "ReviewScheduler",
    "INTERVAL_SEQUENCES",
]
