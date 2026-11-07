"""Tests for memory promotion, decay, and retention policy constants."""
import pytest

import omp_memory


def test_every_policy_constant_is_exported():
    """Every promotion, decay, and forget policy constant must be exported in __all__."""
    policy_constants = [
        "HEAT_WEIGHTS",
        "HEAT_DECAY_POWER_TAU_DAYS",
        "HEAT_DECAY_POWER_BETA",
        "DECAY_POWER_TAU_DAYS",
        "DECAY_POWER_BETA",
        "SCHEMA_PRIOR_WEIGHT",
        "SPACING_MIN_GAP_HOURS",
        "SPACING_FULL_GAP_HOURS",
        "RIF_PENALTY",
        "RIF_MIN_CONFIDENCE",
        "RETRIEVAL_WINDOW_DAYS",
        "RETRIEVALS_FOR_CONSOLIDATION",
        "CONSOLIDATE_GAIN",
        "ABSTAIN_SCORE",
        "PROMOTABLE_TYPES",
        "FORGET_MIN_AGE_DAYS",
        "FORGET_WINDOW_DAYS",
        "FORGET_FLOOR_SLACK",
        "DECAY_FLOOR",
    ]
    for name in policy_constants:
        assert name in omp_memory.__all__, f"{name} missing from __all__"
        assert hasattr(omp_memory, name), f"{name} not accessible on omp_memory"
        val = getattr(omp_memory, name)
        assert val is not None


def test_heat_weights_sum_to_one():
    """Heat component weights must sum to exactly 1.0."""
    assert sum(omp_memory.HEAT_WEIGHTS.values()) == pytest.approx(1.0)


def test_promote_heat_is_above_the_no_reinforcement_ceiling():
    """Promotion must be impossible without positive reinforcement signals.

    Even at maximum confidence (1.0) and instantaneous recency (1.0),
    the heat score without reinforcement cannot reach PROMOTE_HEAT.
    """
    no_reinforcement_ceiling = (
        omp_memory.HEAT_WEIGHTS["confidence"] * 1.0
        + omp_memory.HEAT_WEIGHTS["recency"] * 1.0
    )
    assert omp_memory.PROMOTE_HEAT > no_reinforcement_ceiling
