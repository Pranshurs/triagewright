"""The runner: the model proposes, the runner disposes.

Facts the runner owns and the model cannot assert:
- the customer account a call touches (resolved from the referenced objects);
- whether a call is allowed, needs approval, or is denied (policy);
- whether an approval exists, what exactly it authorises, and that it is unused;
- the outcome of every write, including "unknown" after a lost response;
- the idempotency key of every write;
- the case's terminal status.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from triagewright.env.store import Environment
from triagewright.model import AskCustomer, CaseView, Finish, Model, UseTool
from triagewright.policy import Verdict, evaluate
from triagewright.state import (
    ActionRecord,
    ActionStatus,
    Approval,
    ApprovalStatus,
    CaseState,
    CaseStatus,
    Observation,
    Resolution,
    binding,
)
from triagewright.tools.base import Gateway, Outcome, ToolError, ToolResult, canonical_args
from triagewright.trace import Trace

READ_RETRIES = 2
RECONCILE_ATTEMPTS = 3
MAX_FINISH_REJECTIONS = 2
# Writes that may legitimately be repeated with the same arguments (policy bounds them).
REPEATABLE = {"retry_provisioning", "restart_service", "resync_entitlements",
              "set_ticket_status"}


@dataclass(frozen=True)
class Budget:
    max_steps: int = 40
    max_tool_calls: int = 60


class Runner:
    def __init__(self, state: CaseState, env: Environment, gateway: Gateway, model: Model,
                 trace: Trace, budget: Budget | None = None) -> None:
        self.state = state
        self.env = env
        self.gw = gateway
        self.model = model
        self.trace = trace
        self.budget = budget or Budget()
        self._feedback: str | None = None

    # -- public API ----------------------------------------------------------------

    def run(self) -> CaseStatus:
        """Drive the case until it is terminal or waits for an operator."""
        s = self.state
        if s.status is CaseStatus.OPEN:
            self.trace.emit("case_opened", case=s.case.model_dump())
            self._set_status(CaseStatus.INVESTIGATING, "intake")
        while not s.status.terminal and s.status is not CaseStatus.AWAITING_APPROVAL:
            if s.step >= self.budget.max_steps or s.tool_calls >= self.budget.max_tool_calls:
                self.trace.emit("budget_exhausted", step=s.step, tool_calls=s.tool_calls)
                self._set_status(CaseStatus.FAILED, "budget exhausted")
                break
            view = CaseView(s, self.gw.registry, self._feedback)
            self._feedback = None
            try:
                decision = self.model.decide(view)
            except Exception as e:  # a model failure ends the run, it does not crash it
                self.trace.emit("model_error", error=f"{type(e).__name__}: {e}")
                self._set_status(CaseStatus.FAILED, "model error")
                break
            s.step += 1
            self.trace.emit("decision", step=s.step, decision=decision.model_dump())
            if isinstance(decision, UseTool):
                self._feedback = self._use_tool(decision)
            elif isinstance(decision, AskCustomer):
                self._ask(decision)
            else:
                self._finish(decision)
        return s.status

    def decide_approval(self, approval_id: str, approve: bool, operator: str,
                        note: str | None = None) -> Approval:
        """Operator decision. Approved actions are executed by the runner, now."""
        ap = next((a for a in self.state.approvals if a.id == approval_id), None)
        if ap is None:
            raise KeyError(approval_id)
        if ap.status is not ApprovalStatus.PENDING:
            raise ValueError(f"approval {approval_id} is {ap.status.value}, not pending")
        ap.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
        ap.decided_by, ap.note = operator, note
        self.trace.emit("approval_decided", approval_id=ap.id, approved=approve,
                        operator=operator, note=note)
        if approve:
            self._execute_approved(ap)
        if self.state.status is CaseStatus.AWAITING_APPROVAL and not self.state.pending_approvals():
            self._set_status(CaseStatus.INVESTIGATING, "operator decided all approvals")
            self._feedback = "operator decisions recorded; review approvals and actions"
        return ap

    # -- tool use ----------------------------------------------------------------------

    def _use_tool(self, d: UseTool) -> str:
        s = self.state
        try:
            tool, parsed = self.gw.parse(d.tool, d.args)
        except ToolError as e:
            self.trace.emit("rejected", step=s.step, tool=d.tool, code=e.code, reason=e.message)
            return f"rejected: {e.code}: {e.message}"
        args = parsed.model_dump(mode="json")
        verdict = evaluate(self.env, s.case, tool, parsed)
        self.trace.emit("policy", step=s.step, tool=tool.name, args=args,
                        verdict=verdict.verdict.value, rule=verdict.rule, reason=verdict.reason)
        if verdict.verdict is Verdict.DENY:
            return f"denied by policy ({verdict.rule}): {verdict.reason}"

        if tool.effect.writes:
            blocked = self._write_guard(tool.name, args)
            if blocked:
                self.trace.emit("rejected", step=s.step, tool=tool.name, code="WRITE_GUARD",
                                reason=blocked)
                return f"rejected: {blocked}"

        if verdict.verdict is Verdict.NEEDS_APPROVAL:
            return self._request_approval(tool.name, args, d)
        if tool.effect.writes:
            rec = self._execute_write(tool.name, args, approval_id=None)
            return f"{rec.status.value}: {rec.detail or ''}".strip()
        obs = self._read(tool.name, args)
        return f"observed {obs.id}"

    def _write_guard(self, tool: str, args: dict[str, Any]) -> str | None:
        same = [a for a in self.state.actions if a.tool == tool and a.args == args]
        if any(a.status is ActionStatus.UNKNOWN for a in same):
            return "an identical write has an unknown outcome and is being reconciled"
        if tool not in REPEATABLE and any(a.status is ActionStatus.SUCCEEDED for a in same):
            return "an identical write already succeeded; not repeating it"
        b = binding(self.state.case.id, self.state.case.account_id, tool, args)
        for ap in self.state.approvals:
            if ap.binding == b and ap.status is ApprovalStatus.PENDING:
                return f"already awaiting operator approval {ap.id}"
            if ap.binding == b and ap.status is ApprovalStatus.REJECTED:
                return f"operator rejected this exact action ({ap.id})"
        return None

    def _read(self, tool: str, args: dict[str, Any]) -> Observation:
        attempts = 0
        result: ToolResult
        while True:
            attempts += 1
            self.state.tool_calls += 1
            result = self.gw.invoke(tool, args)
            self.trace.emit("tool_call", tool=tool, args=args, attempt=attempts,
                            outcome=result.outcome.value, error_code=result.error_code)
            if result.outcome is Outcome.OK or not result.retryable or attempts > READ_RETRIES:
                break
        return self._observe(tool, args, result, attempts)

    def _observe(self, tool: str, args: dict[str, Any], r: ToolResult,
                 attempts: int) -> Observation:
        s = self.state
        obs = Observation(id=f"obs_{len(s.observations) + 1:03d}", step=s.step, tool=tool,
                          args=args, ok=r.outcome is Outcome.OK, result=r.payload(),
                          attempts=attempts)
        s.observations.append(obs)
        self.trace.emit("observation", obs_id=obs.id, tool=tool, ok=obs.ok,
                        result=obs.result)
        return obs

    def _key(self, tool: str, args: dict[str, Any]) -> str:
        # Same intent -> same key, until that intent reaches a definite outcome; a new
        # attempt after a definite outcome gets a fresh key. UNKNOWN never advances it.
        settled = sum(1 for a in self.state.actions if a.tool == tool and a.args == args
                      and a.status is not ActionStatus.UNKNOWN)
        material = f"{self.state.case.id}\n{tool}\n{canonical_args(args)}\n{settled}"
        return "tw_" + hashlib.sha256(material.encode()).hexdigest()[:24]

    def _execute_write(self, tool: str, args: dict[str, Any],
                       approval_id: str | None) -> ActionRecord:
        s = self.state
        rec = ActionRecord(id=f"act_{len(s.actions) + 1:03d}", tool=tool, args=args,
                           idempotency_key=self._key(tool, args), status=ActionStatus.UNKNOWN,
                           approval_id=approval_id)
        s.actions.append(rec)
        s.tool_calls += 1
        r = self.gw.invoke(tool, args, rec.idempotency_key)
        self.trace.emit("tool_call", tool=tool, args=args, action_id=rec.id,
                        idempotency_key=rec.idempotency_key, approval_id=approval_id,
                        outcome=r.outcome.value, error_code=r.error_code)
        self._settle(rec, r)
        if rec.status is ActionStatus.UNKNOWN:
            self._reconcile(rec)
        return rec

    def _settle(self, rec: ActionRecord, r: ToolResult) -> None:
        if r.outcome is Outcome.OK:
            rec.status = ActionStatus.SUCCEEDED
            rec.replayed = r.replayed
            rec.detail = json.dumps(r.data, default=str)
        elif r.outcome is Outcome.ERROR:
            rec.status = ActionStatus.FAILED
            rec.detail = f"{r.error_code}: {r.error}"
        else:
            rec.status = ActionStatus.UNKNOWN
            rec.detail = f"{r.error_code}: outcome unknown"
        obs = self._observe(rec.tool, rec.args, r, attempts=1 + rec.reconcile_attempts)
        rec.observation_id = obs.id
        self.trace.emit("action", action_id=rec.id, status=rec.status.value,
                        replayed=rec.replayed, detail=rec.detail)

    def _reconcile(self, rec: ActionRecord) -> None:
        """Resolve an unknown outcome by re-asking upstream under the SAME key.

        The upstream idempotency store either replays the stored result (the effect
        had happened) or applies it now for the first time; it cannot apply it twice.
        """
        while rec.status is ActionStatus.UNKNOWN and rec.reconcile_attempts < RECONCILE_ATTEMPTS:
            rec.reconcile_attempts += 1
            self.state.tool_calls += 1
            r = self.gw.invoke(rec.tool, rec.args, rec.idempotency_key)
            self.trace.emit("reconcile", action_id=rec.id, attempt=rec.reconcile_attempts,
                            idempotency_key=rec.idempotency_key, outcome=r.outcome.value,
                            replayed=r.replayed, error_code=r.error_code)
            if r.outcome is Outcome.ERROR and r.retryable:
                continue  # transient; still unknown
            self._settle(rec, r)

    # -- approvals ---------------------------------------------------------------------

    def _request_approval(self, tool: str, args: dict[str, Any], d: UseTool) -> str:
        s = self.state
        missing = [e for e in d.evidence if s.obs(e) is None]
        ap = Approval(id=f"apr_{len(s.approvals) + 1:03d}", case_id=s.case.id,
                      account_id=s.case.account_id, tool=tool, args=args,
                      binding=binding(s.case.id, s.case.account_id, tool, args),
                      rationale=d.rationale,
                      evidence=[e for e in d.evidence if e not in missing],
                      requested_step=s.step)
        s.approvals.append(ap)
        self.trace.emit("approval_requested", approval_id=ap.id, tool=tool, args=args,
                        binding=ap.binding, rationale=d.rationale, evidence=ap.evidence,
                        unknown_evidence=missing)
        return f"approval {ap.id} requested; pending operator decision"

    def _execute_approved(self, ap: Approval) -> None:
        s = self.state
        # Re-derive what is being authorised from the approval's own stored request and
        # the case; any drift (args, account, case) voids it.
        tool, parsed = self.gw.parse(ap.tool, ap.args)
        args = parsed.model_dump(mode="json")
        expected = binding(s.case.id, s.case.account_id, tool.name, args)
        if ap.case_id != s.case.id or ap.account_id != s.case.account_id or \
                ap.binding != expected:
            self.trace.emit("approval_void", approval_id=ap.id, reason="binding mismatch")
            ap.status = ApprovalStatus.REJECTED
            ap.note = "voided: binding mismatch"
            return
        verdict = evaluate(self.env, s.case, tool, parsed)
        if verdict.verdict is Verdict.DENY:
            self.trace.emit("approval_void", approval_id=ap.id, reason=verdict.reason)
            ap.status = ApprovalStatus.REJECTED
            ap.note = f"voided: {verdict.reason}"
            return
        rec = self._execute_write(tool.name, args, approval_id=ap.id)
        ap.status = ApprovalStatus.EXECUTED
        ap.action_id = rec.id

    # -- ending ------------------------------------------------------------------------

    def _ask(self, d: AskCustomer) -> None:
        self.state.questions.append(d.question)
        self.trace.emit("clarification_requested", question=d.question)
        self._set_status(CaseStatus.NEEDS_INFO, "asked the customer a question")

    def _finish(self, d: Finish) -> None:
        s = self.state
        res = d.resolution
        problems = self._ground(res)
        if problems and s.finish_rejections < MAX_FINISH_REJECTIONS:
            s.finish_rejections += 1
            self.trace.emit("finish_rejected", problems=problems)
            self._feedback = "finish rejected, ungrounded findings: " + "; ".join(problems)
            return
        s.resolution = res
        self.trace.emit("resolution", resolution=res.model_dump(), ungrounded=problems)
        if s.pending_approvals():
            self._set_status(CaseStatus.AWAITING_APPROVAL,
                             f"{len(s.pending_approvals())} approval(s) pending")
        elif s.unknown_actions():
            self._set_status(CaseStatus.NEEDS_ATTENTION,
                             "write(s) with unknown outcome: "
                             + ", ".join(a.id for a in s.unknown_actions()))
        else:
            mapping = {"resolved": CaseStatus.RESOLVED, "escalated": CaseStatus.ESCALATED,
                       "needs_info": CaseStatus.NEEDS_INFO}
            self._set_status(mapping.get(res.outcome, CaseStatus.NEEDS_ATTENTION),
                             f"model requested {res.outcome!r}")

    def _ground(self, res: Resolution) -> list[str]:
        """Every finding must cite observations that exist and contain the values it names."""
        problems: list[str] = []
        for i, f in enumerate(res.findings, 1):
            mine: list[str] = []
            cited = [self.state.obs(e) for e in f.evidence]
            if not f.evidence:
                mine.append(f"finding {i} cites no evidence")
            for e, o in zip(f.evidence, cited, strict=True):
                if o is None:
                    mine.append(f"finding {i} cites {e}, which was never observed")
            blob = " ".join(json.dumps(o.result, default=str) for o in cited if o is not None)
            for v in f.values:
                if v not in blob:
                    mine.append(f"finding {i}: {v!r} not in cited observations")
            f.grounded = not mine
            problems.extend(mine)
        return problems

    def _set_status(self, status: CaseStatus, reason: str) -> None:
        self.state.status = status
        self.state.status_reason = reason
        self.trace.emit("case_status", status=status.value, reason=reason)
