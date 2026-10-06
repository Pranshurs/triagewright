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


def save_state(state: CaseState, path: Path) -> None:
    """Atomic write: a crash leaves either the old or the new state, never half of one."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(path)


def open_session(scenario: Scenario, model: Model | None = None, script: str = "good",
                 out_dir: str | Path | None = None, budget: Budget | None = None) -> Session:
    """Fresh session. With `out_dir`, environment, state and trace live on disk."""
    out = Path(out_dir) if out_dir else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
        for name in ("env.sqlite3", "state.json", "trace.jsonl"):
            (out / name).unlink(missing_ok=True)
    env = Environment(scenario.fixture, now=scenario.now,
                      path=out / "env.sqlite3" if out else ":memory:")
    state = CaseState(case=scenario.case)
    return _assemble(scenario, env, state, out, model, script, budget,
                     FaultPlan(list(scenario.faults)))


def resume_session(scenario: Scenario, out_dir: str | Path, model: Model | None = None,
                   script: str = "good", budget: Budget | None = None,
                   faults: FaultPlan | None = None) -> Session:
    """Rebuild a session from disk, as a new process would after a crash."""
    out = Path(out_dir)
    env = Environment.open(out / "env.sqlite3")
    state = CaseState.model_validate_json((out / "state.json").read_text(encoding="utf-8"))
    return _assemble(scenario, env, state, out, model, script, budget, faults or FaultPlan())


def _assemble(scenario: Scenario, env: Environment, state: CaseState, out: Path | None,
              model: Model | None, script: str, budget: Budget | None,
              faults: FaultPlan) -> Session:
    gw = Gateway(env, default_registry(), faults)
    trace = Trace(out / "trace.jsonl" if out else None)
    if model is None:
        model = ScriptedModel(scenario.scripts[script]())
    checkpoint = (lambda st: save_state(st, out / "state.json")) if out else None
    runner = Runner(state, env, gw, model, trace, budget, checkpoint=checkpoint)
    return Session(scenario, env, state, trace, runner)


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
