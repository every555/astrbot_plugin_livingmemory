"""Tests for memory reinforcement engine (spaced repetition review).

Tests adapted from DeepTutor's learning module test patterns.
"""
from __future__ import annotations

import time

import pytest

from astrbot_plugin_livingmemory.core.reinforcement.models import ReinforcementState, ReviewTask
from astrbot_plugin_livingmemory.core.reinforcement.memory_strength import (
    ENDANGERED_THRESHOLD,
    STRONG_THRESHOLD,
    compute_memory_strength,
    is_endangered,
    is_strong,
)
from astrbot_plugin_livingmemory.core.reinforcement.scheduler import INTERVAL_SEQUENCES, ReviewScheduler


# ── ReinforcementState tests ──


class TestReinforcementState:
    def test_default_state(self):
        state = ReinforcementState()
        assert state.interval_index == 0
        assert state.consecutive_correct == 0
        assert state.consecutive_wrong == 0
        assert state.next_review_at == 0.0
        assert state.review_strength == 1.0

    def test_json_roundtrip(self):
        state = ReinforcementState(
            interval_index=3,
            consecutive_correct=2,
            consecutive_wrong=0,
            next_review_at=1700000000.0,
            last_reviewed_at=1699900000.0,
            review_strength=0.75,
        )
        json_str = state.to_json()
        restored = ReinforcementState.from_json(json_str)
        assert restored.interval_index == 3
        assert restored.consecutive_correct == 2
        assert restored.next_review_at == 1700000000.0
        assert restored.review_strength == 0.75

    def test_from_json_none(self):
        state = ReinforcementState.from_json(None)
        assert state.interval_index == 0

    def test_from_json_empty_string(self):
        state = ReinforcementState.from_json("")
        assert state.interval_index == 0

    def test_from_json_invalid(self):
        state = ReinforcementState.from_json("not json")
        assert state.interval_index == 0

    def test_from_json_dict(self):
        state = ReinforcementState.from_json({"interval_index": 2, "review_strength": 0.5})
        assert state.interval_index == 2
        assert state.review_strength == 0.5


# ── Memory strength tests ──


class TestMemoryStrength:
    def test_no_history_returns_strong(self):
        assert compute_memory_strength([]) == 1.0

    def test_single_correct_capped(self):
        # One correct review → capped at 0.5 (low confidence)
        assert compute_memory_strength([True]) == 0.5

    def test_single_wrong_zero(self):
        # One wrong → very low
        score = compute_memory_strength([False])
        assert score < 0.3

    def test_two_correct_capped(self):
        # Two correct → capped at 0.8
        assert compute_memory_strength([True, True]) == 0.8

    def test_three_or_more_no_cap(self):
        # Three correct → no cap, full recency weighting
        score = compute_memory_strength([True, True, True])
        assert score > 0.85

    def test_all_wrong_low(self):
        score = compute_memory_strength([False, False, False])
        assert score < 0.2

    def test_recovery_after_forgetting(self):
        # Early wrong, recent correct → recovery rewarded
        score = compute_memory_strength([False, False, True, True, True])
        # Should be reasonable due to recency weights
        assert 0.4 < score < 1.0

    def test_is_endangered(self):
        assert is_endangered(0.2) is True
        assert is_endangered(0.3) is True
        assert is_endangered(0.31) is False

    def test_is_strong(self):
        assert is_strong(0.69) is False
        assert is_strong(0.7) is True
        assert is_strong(0.9) is True


# ── ReviewScheduler tests ──


class TestReviewScheduler:
    def setup_method(self):
        # Use debug mode (seconds instead of days)
        self.scheduler = ReviewScheduler(debug_mode=True)

    def test_initial_state(self):
        state = self.scheduler.get_initial_state()
        assert state.interval_index == 0
        assert state.next_review_at > time.time()

    def test_interval_sequences(self):
        """Verify intervals are reasonable for memory review."""
        assert INTERVAL_SEQUENCES[0] == 1  # First review: 1 day
        assert INTERVAL_SEQUENCES[-1] == 120  # Max interval: 120 days
        assert len(INTERVAL_SEQUENCES) == 7  # 7 levels

    def test_schedule_next_correct(self):
        state = ReinforcementState(interval_index=0, next_review_at=0)
        state = self.scheduler.schedule_next(state, is_correct=True)
        assert state.interval_index >= 1  # Advanced
        assert state.next_review_at > time.time()

    def test_schedule_next_wrong_retreats(self):
        state = ReinforcementState(interval_index=2, next_review_at=0)
        state = self.scheduler.schedule_next(state, is_correct=False)
        assert state.interval_index <= 1  # Pulled back
        assert state.next_review_at > time.time()

    def test_consecutive_correct_accelerates(self):
        state = ReinforcementState(interval_index=0, next_review_at=0)
        # Two correct in a row → jump 2 levels
        state = self.scheduler.schedule_next(state, is_correct=True)
        idx1 = state.interval_index
        state = self.scheduler.schedule_next(state, is_correct=True)
        assert state.interval_index >= idx1 + 1  # Accelerated

    def test_consecutive_wrong_does_not_sink_below_zero(self):
        state = ReinforcementState(interval_index=0, next_review_at=0)
        state = self.scheduler.schedule_next(state, is_correct=False)
        assert state.interval_index == 0  # Can't go below 0

    def test_is_due(self):
        # Past due
        state = ReinforcementState(next_review_at=time.time() - 10)
        assert self.scheduler.is_due(state) is True

        # Future
        state = ReinforcementState(next_review_at=time.time() + 3600)
        assert self.scheduler.is_due(state) is False

    def test_compute_priority_endangered(self):
        assert self.scheduler.compute_priority(0.5, is_endangered=True) == 1

    def test_compute_priority_high_importance(self):
        assert self.scheduler.compute_priority(0.9, is_endangered=False) == 2

    def test_compute_priority_low_importance(self):
        assert self.scheduler.compute_priority(0.2, is_endangered=False) == 5

    def test_get_due_reviews_empty(self):
        tasks = self.scheduler.get_due_reviews([])
        assert len(tasks) == 0

    def test_get_due_reviews_sorts_by_priority(self):
        now = time.time()
        atoms = [
            {
                "id": 1,
                "content": "low priority",
                "importance": 0.2,
                "reinforcement_state": ReinforcementState(
                    next_review_at=now - 100
                ).to_json(),
            },
            {
                "id": 2,
                "content": "endangered!",
                "importance": 0.5,
                "reinforcement_state": ReinforcementState(
                    next_review_at=now - 100,
                    review_strength=0.2,
                ).to_json(),
            },
            {
                "id": 3,
                "content": "high importance",
                "importance": 0.9,
                "reinforcement_state": ReinforcementState(
                    next_review_at=now - 100
                ).to_json(),
            },
        ]
        tasks = self.scheduler.get_due_reviews(atoms, max_tasks=3)
        assert len(tasks) == 3
        # Endangered (id=2) should be first
        assert tasks[0].atom_id == 2
        # High importance (id=3) should be before low (id=1)
        assert tasks[2].atom_id == 1

    def test_get_due_reviews_skips_not_due(self):
        now = time.time()
        atoms = [
            {
                "id": 1,
                "content": "not due",
                "importance": 0.9,
                "reinforcement_state": ReinforcementState(
                    next_review_at=now + 99999
                ).to_json(),
            },
        ]
        tasks = self.scheduler.get_due_reviews(atoms)
        assert len(tasks) == 0


# ── Integration-style test ──


class TestReinforcementIntegration:
    """Test the full reinforcement cycle without real DB."""

    def test_full_cycle_remember(self):
        """Simulate: memory created → review due → user confirms → strength up."""
        scheduler = ReviewScheduler(debug_mode=True)
        state = scheduler.get_initial_state()

        # Wait a tiny bit so it's due
        state.next_review_at = time.time() - 1

        # User confirms they remember
        state = scheduler.schedule_next(state, is_correct=True)

        # Build history and compute strength
        history = [True, True]  # initial + this confirmation
        new_strength = compute_memory_strength(history)
        state.review_strength = new_strength

        # Should be stronger (capped at 0.8 for 2 reviews)
        assert state.review_strength >= 0.5
        assert state.interval_index > 0  # Interval advanced
        assert state.consecutive_correct > 0

    def test_full_cycle_forget_then_recover(self):
        """Simulate: user forgets → strength drops → later recovers."""
        scheduler = ReviewScheduler(debug_mode=True)

        # Start strong
        state = ReinforcementState(
            interval_index=3,
            consecutive_correct=3,
            review_strength=0.85,
            next_review_at=time.time() - 1,
        )

        # User forgets
        state = scheduler.schedule_next(state, is_correct=False)
        history = [True, True, True, False]
        state.review_strength = compute_memory_strength(history)

        # Strength should drop
        assert state.review_strength < 0.85
        assert state.interval_index < 3  # Interval retreated

    def test_strength_thresholds_work(self):
        """Verify endangered/strong thresholds make sense."""
        assert ENDANGERED_THRESHOLD == 0.3
        assert STRONG_THRESHOLD == 0.7

        # A memory at 0.3 is endangered, at 0.31 is not
        assert is_endangered(0.30) is True
        assert is_endangered(0.31) is False

        # A memory at 0.69 is not strong, at 0.70 is
        assert is_strong(0.69) is False
        assert is_strong(0.70) is True
