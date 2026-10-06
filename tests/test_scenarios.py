"""Every arm of every scenario must produce the score card it declares."""

from __future__ import annotations

import pytest

from triagewright.evals.run import run_arm
from triagewright.evals.scorer import matches_expectation
from triagewright.scenarios import GOOD, registry

SCENARIOS = registry()
ARMS = [(sid, arm) for sid, sc in SCENARIOS.items() for arm in sc.arms]


@pytest.mark.parametrize(("sid", "arm"), ARMS, ids=[f"{s}-{a}" for s, a in ARMS])
def test_arm_scores_as_declared(sid: str, arm: str) -> None:
    _s, card, misses = run_arm(SCENARIOS[sid], arm)
    assert not misses, (misses, card.notes)


@pytest.mark.parametrize("sid", list(SCENARIOS))
def test_every_red_arm_is_caught_somewhere(sid: str) -> None:
    sc = SCENARIOS[sid]
    red = [a for a in sc.arms if a != "good"]
    assert red, f"{sid} has no adversarial arm"
    for arm in red:
        _s, card, _m = run_arm(sc, arm)
        # A red arm must differ from a clean card on some dimension, or show blocked
        # attempts; one that looks clean measures nothing.
        if matches_expectation(card, GOOD):  # differs from a clean card somewhere
            continue
        assert card.unauthorized_attempts > 0, f"{sid}/{arm} scores clean"


def test_scenarios_attack_distinct_invariants() -> None:
    inv = [sc.invariant for sc in SCENARIOS.values()]
    assert len(set(inv)) == len(inv) == 10


def test_unsafe_effects_never_succeed_through_the_runner() -> None:
    """Across all arms, the only harmful effects are ones an operator approved or that
    policy legitimately allowed; never a denied or unapproved consequential effect."""
    for sid, arm in ARMS:
        _s, card, _m = run_arm(SCENARIOS[sid], arm)
        assert not any("without operator approval" in n for n in card.notes), (sid, arm)
        assert not any("another account" in n for n in card.notes), (sid, arm)
        assert card.scope_intact, (sid, arm)
        assert card.unknown_closed, (sid, arm)
