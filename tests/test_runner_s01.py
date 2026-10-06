"""S01 vertical slice plus red arms against the runner's authority boundaries."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from triagewright.env.faults import FaultKind, FaultRule
from triagewright.harness import Session, open_session, run_with_operator
from triagewright.model import Finish, ScriptedModel, Step, UseTool
from triagewright.scenarios import OperatorRule, s01_duplicate_charge
from triagewright.state import ActionStatus, ApprovalStatus, CaseState, CaseStatus, Resolution

S01 = s01_duplicate_charge.SCENARIO
ACC = s01_duplicate_charge.ACCOUNT
REFUND = {"payment_event_id": "pe_1001c", "amount_cents": 480000, "reason": "duplicate"}


def refunds(s: Session) -> list[dict[str, object]]:
    return [e for e in s.env.snapshot()["payment_events"] if e["kind"] == "refund"]


def session(steps: Sequence[Step], **overrides: object) -> Session:
    sc = dataclasses.replace(S01, **overrides) if overrides else S01
    return open_session(sc, model=ScriptedModel(steps))


def done(outcome: str = "resolved") -> Finish:
    return Finish(resolution=Resolution(outcome=outcome, diagnosis="x", summary="x"))


# -- the vertical slice ------------------------------------------------------------------


def test_s01_good_path_end_state() -> None:
    s = open_session(S01)
    assert run_with_operator(s) is CaseStatus.RESOLVED
    snap = s.env.snapshot()
    assert len(refunds(s)) == 1 and refunds(s)[0]["processor_ref"] == "ch_9Rm4"
    assert {e["feature"] for e in snap["entitlements"]} == {"audit_log", "scim", "sso"}
    assert snap["workspaces"][0]["status"] == "active"
    assert next(t for t in snap["tickets"] if t["id"] == "tkt_5512")["status"] == "solved"
    refund = next(a for a in s.state.actions if a.tool == "issue_refund")
    assert refund.status is ActionStatus.SUCCEEDED and refund.replayed
    assert refund.reconcile_attempts == 1
    assert all(f.grounded for f in s.state.resolution.findings)  # type: ignore[union-attr]


def test_s01_trace_is_deterministic() -> None:
    a, b = open_session(S01), open_session(S01)
    run_with_operator(a)
    run_with_operator(b)
    assert a.trace.events == b.trace.events


def test_paused_case_resumes_from_persisted_state() -> None:
    s = open_session(S01)
    assert s.runner.run() is CaseStatus.AWAITING_APPROVAL
    restored = CaseState.model_validate_json(s.state.model_dump_json())
    s.runner.state = restored
    s.state = restored
    s.runner.decide_approval(restored.pending_approvals()[0].id, True, "op")
    assert s.runner.run() is CaseStatus.RESOLVED
    assert len(refunds(s)) == 1


# -- approval boundary -------------------------------------------------------------------


def test_consequential_action_waits_for_approval() -> None:
    s = session([UseTool(tool="issue_refund", args=REFUND), done()])
    assert s.runner.run() is CaseStatus.AWAITING_APPROVAL
    assert refunds(s) == []
    assert s.state.actions == []


def test_rejected_action_is_not_executed_or_retryable() -> None:
    s = session([UseTool(tool="issue_refund", args=REFUND), done(),
                 UseTool(tool="issue_refund", args=REFUND), done()],
                operator=(OperatorRule(tool="issue_refund", approve=False),))
    assert run_with_operator(s) is CaseStatus.RESOLVED
    assert refunds(s) == []
    assert any("operator rejected" in e.get("reason", "") for e in s.trace.of("rejected"))


def test_approval_cannot_authorize_changed_arguments() -> None:
    s = session([UseTool(tool="issue_refund", args={**REFUND, "amount_cents": 1000}), done()])
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    ap.args = {**ap.args, "amount_cents": 480000}  # tamper after the request
    s.runner.decide_approval(ap.id, True, "op")
    assert ap.status is ApprovalStatus.REJECTED and "binding" in (ap.note or "")
    assert refunds(s) == []


def test_approval_cannot_move_to_another_case() -> None:
    s = session([UseTool(tool="issue_refund", args=REFUND), done()])
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    s.state.case = s.state.case.model_copy(update={"id": "case_other"})
    s.runner.decide_approval(ap.id, True, "op")
    assert refunds(s) == []


def test_approval_is_single_use() -> None:
    s = open_session(S01)
    run_with_operator(s)
    ap = s.state.approvals[0]
    try:
        s.runner.decide_approval(ap.id, True, "op")
    except ValueError:
        pass
    else:
        raise AssertionError("an executed approval was decided again")
    assert len(refunds(s)) == 1


def test_duplicate_proposal_does_not_duplicate_effect() -> None:
    s = session([UseTool(tool="issue_refund", args={**REFUND, "amount_cents": 1000}), done(),
                 UseTool(tool="issue_refund", args={**REFUND, "amount_cents": 1000}), done()],
                faults=())
    assert run_with_operator(s) is CaseStatus.RESOLVED
    assert len(refunds(s)) == 1


# -- scope boundary -------------------------------------------------------------------------


def test_cross_account_reads_and_writes_are_denied() -> None:
    s = session([
        UseTool(tool="get_account", args={"account_id": "acc_brightmoor"}),
        UseTool(tool="find_contact", args={"email": "ravi@brightmoor.example"}),
        UseTool(tool="add_internal_note", args={"ticket_id": "tkt_4120", "body": "ok"}),
        done(),
    ])
    s.runner.run()
    verdicts = [e["verdict"] for e in s.trace.of("policy")]
    assert verdicts == ["deny", "deny", "allow"]
    assert not any(o.tool in ("get_account", "find_contact") for o in s.state.observations)


def test_denied_catalogue_tool_never_runs() -> None:
    s = session([UseTool(tool="export_account_contacts", args={"account_id": ACC}), done()])
    s.runner.run()
    assert s.trace.of("policy")[0]["rule"] == "catalogue.denied"
    assert s.trace.of("tool_call") == []


# -- unknown outcomes ---------------------------------------------------------------------


def test_lost_response_before_effect_is_applied_exactly_once_on_reconcile() -> None:
    s = session([UseTool(tool="issue_refund", args=REFUND), done()],
                faults=(FaultRule(tool="issue_refund", kind=FaultKind.TIMEOUT_BEFORE_EFFECT),))
    run_with_operator(s)
    rec = s.state.actions[0]
    assert rec.status is ActionStatus.SUCCEEDED and not rec.replayed
    assert len(refunds(s)) == 1


def test_unresolvable_unknown_blocks_retry_and_resolution() -> None:
    partial = {**REFUND, "amount_cents": 1000}  # partial: the over-refund check can't save us
    s = session([UseTool(tool="issue_refund", args=partial), done(),
                 UseTool(tool="issue_refund", args=partial), done()],
                faults=(FaultRule(tool="issue_refund", kind=FaultKind.TIMEOUT_AFTER_EFFECT),
                        FaultRule(tool="issue_refund", on_call=2, repeat=10,
                                  kind=FaultKind.TRANSIENT_ERROR)))
    status = run_with_operator(s)
    assert s.state.actions[0].status is ActionStatus.UNKNOWN
    assert len(s.state.actions) == 1           # the re-proposal was refused
    assert len(refunds(s)) == 1                 # the one real effect
    assert status is CaseStatus.NEEDS_ATTENTION # cannot be called resolved


# -- grounding ------------------------------------------------------------------------------


def test_ungrounded_findings_are_rejected_then_flagged() -> None:
    from triagewright.state import Finding

    bad = Finish(resolution=Resolution(
        outcome="resolved", diagnosis="x", summary="x",
        findings=[Finding(claim="refund pe_9999 exists", evidence=["obs_001"],
                          values=["pe_9999"]),
                  Finding(claim="made up", evidence=["obs_404"])]))
    s = session([UseTool(tool="list_payment_events", args={"account_id": ACC}), bad, bad, bad],
                faults=())
    s.runner.run()
    assert len(s.trace.of("finish_rejected")) == 2
    res = s.state.resolution
    assert res is not None and [f.grounded for f in res.findings] == [False, False]
