"""Case service: the one place every transport (CLI, HTTP, MCP) goes through.

It stores cases on disk and forwards decisions to the runner. It holds no policy,
scope, approval or idempotency logic of its own; those live in the runner.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from triagewright.evals.run import record_of
from triagewright.evals.scorer import score
from triagewright.harness import Session, open_session, resume_session, run_with_operator
from triagewright.model import AskCustomer, CaseView, Finish, UseTool
from triagewright.record import case_record
from triagewright.scenarios import Scenario, registry
from triagewright.state import ApprovalStatus
from triagewright.telemetry import Exporter, OtlpHttpExporter, otlp_json, safe_export


class ExternalModel:
    """Placeholder for cases driven from outside: decisions arrive via `submit`."""

    name = "external"

    def decide(self, view: CaseView) -> UseTool | AskCustomer | Finish:
        raise RuntimeError("externally driven case: submit decisions instead of run()")


class UnknownCase(KeyError):
    pass


class CaseService:
    def __init__(self, root: str | Path, exporter: Exporter | None = None) -> None:
        endpoint = os.environ.get("TRIAGEWRIGHT_OTLP_ENDPOINT")
        self.exporter = exporter or (OtlpHttpExporter(endpoint) if endpoint else None)
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()

    # -- lifecycle -------------------------------------------------------------------

    def create(self, scenario_id: str, arm: str | None = "good") -> str:
        """Open a case on a scenario. `arm=None` means an external agent drives it."""
        sc = self._scenario(scenario_id)
        if arm is not None and arm not in sc.arms:
            raise KeyError(f"unknown arm {arm!r}")
        with self._lock:
            n = 1 + sum(1 for p in self.root.glob(f"{sc.case.id}-*") if p.is_dir())
            case_id = f"{sc.case.id}-{n:03d}"
            out = self.root / case_id
            case = sc.case.model_copy(update={"id": case_id})
            model = ExternalModel() if arm is None else None
            s = open_session(sc, model=model, arm=arm or "good", out_dir=out, case=case)
            (out / "meta.json").write_text(json.dumps(
                {"scenario": sc.id, "arm": arm, "driver": "external" if arm is None
                 else "scripted"}), encoding="utf-8")
            s.runner.start()
            self._sessions[case_id] = s
            return case_id

    def cases(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.iterdir()):
            if (p / "meta.json").exists():
                meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
                out.append({"case_id": p.name, **meta,
                            "status": self.session(p.name).state.status.value})
        return out

    def session(self, case_id: str) -> Session:
        with self._lock:
            if case_id in self._sessions:
                return self._sessions[case_id]
            out = self.root / case_id
            if "/" in case_id or ".." in case_id or not (out / "meta.json").exists():
                raise UnknownCase(case_id)
            meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
            sc = self._scenario(meta["scenario"])
            model = ExternalModel() if meta["arm"] is None else None
            s = resume_session(sc, out, model=model, arm=meta["arm"] or "good")
            s.runner.start()  # settles anything a dead process left in flight
            self._sessions[case_id] = s
            return s

    def evict(self, case_id: str) -> None:
        """Drop the in-memory session (tests use this to simulate a restart)."""
        with self._lock:
            s = self._sessions.pop(case_id, None)
            if s:
                s.env.close()

    # -- driving ---------------------------------------------------------------------

    def run(self, case_id: str) -> str:
        with self._lock:
            s = self.session(case_id)
            if isinstance(s.runner.model, ExternalModel):
                raise ValueError("externally driven case; submit decisions instead")
            status = s.runner.run().value
            self._export(case_id)
            return status

    def run_with_simulated_operator(self, case_id: str) -> str:
        with self._lock:
            status = run_with_operator(self.session(case_id)).value
            self._export(case_id)
            return status

    def submit(self, case_id: str, decision: UseTool | AskCustomer | Finish) -> dict[str, Any]:
        with self._lock:
            s = self.session(case_id)
            before = len(s.state.observations)
            feedback = s.runner.submit(decision)
            new = s.state.observations[before:]
            self._export(case_id)
            return {"feedback": feedback, "status": s.state.status.value,
                    "observations": [o.model_dump(mode="json") for o in new]}

    def decide(self, case_id: str, approval_id: str, approve: bool, operator: str,
               binding: str, note: str | None = None) -> dict[str, Any]:
        with self._lock:
            s = self.session(case_id)
            ap = s.runner.decide_approval(approval_id, approve, operator, note,
                                          expected_binding=binding)
            if not isinstance(s.runner.model, ExternalModel) and \
                    not s.state.pending_approvals():
                s.runner.run()  # scripted cases continue on their own
            self._export(case_id)
            return {"approval": ap.model_dump(mode="json"), "status": s.state.status.value}

    def _export(self, case_id: str) -> None:
        s = self._sessions[case_id]
        safe_export(self.exporter, lambda: otlp_json(s.trace.events, case_id, s.trace.times))

    # -- reading ---------------------------------------------------------------------

    def view(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            return self._view(case_id)

    def _view(self, case_id: str) -> dict[str, Any]:
        s = self.session(case_id)
        st = s.state
        return {
            "case": st.case.model_dump(), "status": st.status.value,
            "status_reason": st.status_reason, "steps": st.step, "tool_calls": st.tool_calls,
            "observations": [o.model_dump(mode="json") for o in st.observations],
            "actions": [a.model_dump(mode="json", exclude={"idempotency_key"})
                        for a in st.actions],
            "approvals": [a.model_dump(mode="json") for a in st.approvals],
            "pending_approvals": [a.id for a in st.approvals
                                  if a.status is ApprovalStatus.PENDING],
            "questions": st.questions,
            "resolution": st.resolution.model_dump(mode="json") if st.resolution else None,
        }

    def trace(self, case_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._trace(case_id)

    def _trace(self, case_id: str) -> list[dict[str, Any]]:
        return list(self.session(case_id).trace.events)

    def record(self, case_id: str) -> str:
        with self._lock:
            return self._record(case_id)

    def _record(self, case_id: str) -> str:
        s = self.session(case_id)
        return case_record(s.state, s.trace.events)

    def score(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            return self._score(case_id)

    def _score(self, case_id: str) -> dict[str, Any]:
        s = self.session(case_id)
        return score(s.scenario.gold, record_of(s)).as_dict()

    def tools(self) -> list[dict[str, Any]]:
        from triagewright.tools.catalog import default_registry

        return [{**t.json_schema(), "effect": t.effect.value, "domain": t.domain}
                for t in default_registry()]

    @staticmethod
    def _scenario(scenario_id: str) -> Scenario:
        sc = registry().get(scenario_id.upper())
        if sc is None:
            raise KeyError(f"unknown scenario {scenario_id!r}")
        return sc

