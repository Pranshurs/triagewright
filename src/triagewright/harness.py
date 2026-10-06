"""Wire a scenario into a runnable session, and drive it with a simulated operator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from triagewright.env.faults import FaultPlan
from triagewright.env.store import Environment
from triagewright.model import Model, ScriptedModel
from triagewright.runner import Budget, Runner
from triagewright.scenarios import Scenario
from triagewright.state import CaseState, CaseStatus
from triagewright.tools.base import Gateway
from triagewright.tools.catalog import default_registry
from triagewright.trace import Trace


@dataclass
class Session:
    scenario: Scenario
    env: Environment
    state: CaseState
    trace: Trace
    runner: Runner


def open_session(scenario: Scenario, model: Model | None = None, script: str = "good",
                 out_dir: str | Path | None = None, budget: Budget | None = None) -> Session:
    env = Environment(scenario.fixture, now=scenario.now)
    gw = Gateway(env, default_registry(), FaultPlan(list(scenario.faults)))
    state = CaseState(case=scenario.case)
    trace = Trace(Path(out_dir) / "trace.jsonl" if out_dir else None)
    if model is None:
        model = ScriptedModel(scenario.scripts[script]())
    return Session(scenario, env, state, trace, Runner(state, env, gw, model, trace, budget))


def run_with_operator(session: Session, max_rounds: int = 5) -> CaseStatus:
    """Run to a terminal status, answering approvals per the scenario's operator rules.

    Approvals for tools the scenario has no rule for are rejected: an unplanned
    consequential action is the operator's to refuse.
    """
    rules = {r.tool: r for r in session.scenario.operator}
    status = session.runner.run()
    for _ in range(max_rounds):
        if status is not CaseStatus.AWAITING_APPROVAL:
            break
        for ap in session.state.pending_approvals():
            rule = rules.get(ap.tool)
            session.runner.decide_approval(
                ap.id, approve=bool(rule and rule.approve), operator="sim-operator",
                note=rule.note if rule else "no operator rule: rejected")
        status = session.runner.run()
    return status
