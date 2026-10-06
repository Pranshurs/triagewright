"""Run scenario arms and score them."""

from __future__ import annotations

from pathlib import Path

from triagewright.evals.scorer import RunRecord, ScoreCard, matches_expectation, score
from triagewright.harness import Session, open_session, run_with_operator
from triagewright.scenarios import Scenario, registry


def record_of(session: Session) -> RunRecord:
    st = session.state
    return RunRecord(
        scenario=session.scenario.id, arm=session.arm, account_id=st.case.account_id,
        fixture=session.scenario.fixture, snapshot=session.env.snapshot(),
        effects=session.env.effects(), events=session.trace.events, status=st.status.value,
        actions=[a.model_dump(mode="json") for a in st.actions],
        resolution=st.resolution.model_dump(mode="json") if st.resolution else None,
        steps=st.step, tool_calls=st.tool_calls)


def run_arm(scenario: Scenario, arm: str,
            out_dir: Path | None = None) -> tuple[Session, ScoreCard, list[str]]:
    s = open_session(scenario, arm=arm, out_dir=out_dir)
    run_with_operator(s)
    card = score(scenario.gold, record_of(s))
    return s, card, matches_expectation(card, scenario.arms[arm].expect)


def run_all(out_root: Path | None = None) -> list[tuple[ScoreCard, list[str]]]:
    results = []
    for sc in registry().values():
        for arm in sc.arms:
            out = out_root / sc.id / arm if out_root else None
            _s, card, misses = run_arm(sc, arm, out)
            results.append((card, misses))
    return results
