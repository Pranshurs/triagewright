"""Boundary tests added after the first mutation pass (mutation/RESULTS.md).

Each test names the rule it pins and the mutant that showed the rule had no test.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from triagewright.evals.run import record_of
from triagewright.evals.scorer import score
from triagewright.harness import Session, open_session, run_with_operator
from triagewright.model import Finish, ScriptedModel, UseTool
from triagewright.runner import NotAccepting, StaleApproval
from triagewright.scenarios import registry, s01_duplicate_charge
from triagewright.service import CaseService
from triagewright.state import ActionStatus, ApprovalStatus, CaseStatus, Resolution

S01 = s01_duplicate_charge.SCENARIO
ACC = s01_duplicate_charge.ACCOUNT
REFUND = {"payment_event_id": "pe_1001c", "amount_cents": 480000, "reason": "duplicate"}


def _done() -> Finish:
    return Finish(resolution=Resolution(outcome="resolved", diagnosis="x", summary="x"))


def _session(steps: list[object], **kw: object) -> Session:
    sc = dataclasses.replace(S01, **kw) if kw else S01
    return open_session(sc, model=ScriptedModel(steps))  # type: ignore[arg-type]


def _refunds(s: Session) -> int:
    return sum(1 for e in s.env.effects() if e["tool"] == "issue_refund")


# A03: a decision must match the stored binding, not only the stored arguments.
def test_decision_refused_when_stored_binding_differs_from_what_was_shown() -> None:
    s = _session([UseTool(tool="issue_refund", args=REFUND), _done()], faults=())
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    shown = ap.binding
    ap.binding = "0" * 64  # record altered after the operator's page was rendered
    with pytest.raises(StaleApproval):
        s.runner.decide_approval(ap.id, True, "op", expected_binding=shown)
    assert ap.status is ApprovalStatus.PENDING and _refunds(s) == 0


# A06: an approval record that names another case is not executed here.
def test_approval_record_naming_another_case_is_void() -> None:
    s = _session([UseTool(tool="issue_refund", args=REFUND), _done()], faults=())
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    ap.case_id = "case_elsewhere"
    s.runner.decide_approval(ap.id, True, "op")
    assert ap.status is ApprovalStatus.REJECTED and _refunds(s) == 0


# A07: policy is re-checked at execution; the world may change while an approval waits.
def test_approval_voided_if_target_moved_to_another_account() -> None:
    s = _session([UseTool(tool="issue_refund", args=REFUND), _done()], faults=())
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    # Billing re-homes the payment to another account (e.g. an account merge) before
    # the operator gets to the request.
    s.env.execute("UPDATE payment_events SET account_id='acc_brightmoor' WHERE id='pe_1001c'")
    s.runner.decide_approval(ap.id, True, "op")
    assert ap.status is ApprovalStatus.REJECTED and "voided" in (ap.note or "")
    assert _refunds(s) == 0


# A15: only a rejected approval can be reopened.
def test_reopen_only_applies_to_rejections() -> None:
    s = _session([UseTool(tool="issue_refund", args=REFUND), _done()], faults=())
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    with pytest.raises(ValueError):
        s.runner.reopen(ap.id, "op", "x")
    assert ap.status is ApprovalStatus.PENDING
    s.runner.decide_approval(ap.id, True, "op")
    with pytest.raises(ValueError):
        s.runner.reopen(ap.id, "op", "x")
    assert ap.status is ApprovalStatus.EXECUTED


# T06: a reference whose owner cannot be resolved fails closed (nothing observed).
def test_unresolvable_reference_is_denied_not_treated_as_global() -> None:
    s = _session([UseTool(tool="list_invoices", args={"account_id": "acc_unknown"}),
                  UseTool(tool="get_ticket", args={"ticket_id": "tkt_missing"}), _done()],
                 faults=())
    s.runner.run()
    assert [e["rule"] for e in s.trace.of("policy")] == ["scope.unresolved"] * 2
    assert s.state.observations == []


# U02: a repeatable write after a definite outcome is a new attempt with a new key.
def test_repeatable_write_after_definite_outcome_gets_a_fresh_attempt() -> None:
    retry = UseTool(tool="retry_provisioning", args={"workspace_id": "ws_halvard_prod"})
    s = _session([retry, UseTool(tool="resync_entitlements", args={"account_id": ACC}),
                  retry, _done()], faults=())
    s.runner.run()
    first, _, second = s.state.actions
    assert first.idempotency_key != second.idempotency_key
    assert '"failed"' in (first.detail or "") and '"succeeded"' in (second.detail or "")
    job = s.env.one("SELECT status, attempts FROM provisioning_jobs WHERE id='job_77310'")
    assert job == {"status": "succeeded", "attempts": 3}


# X04: transports cannot submit to a case that is waiting for an operator or finished.
def test_service_refuses_decisions_while_awaiting_or_after_terminal(tmp_path: Path) -> None:
    svc = CaseService(tmp_path)
    cid = svc.create("S01", arm=None)
    svc.submit(cid, UseTool(tool="issue_refund", args=REFUND))
    svc.submit(cid, _done())
    assert svc.view(cid)["status"] == "awaiting_approval"
    with pytest.raises(NotAccepting):
        svc.submit(cid, UseTool(tool="resync_entitlements", args={"account_id": ACC}))
    ap = svc.view(cid)["approvals"][0]
    svc.decide(cid, ap["id"], False, "op", ap["binding"])
    svc.submit(cid, _done())
    assert svc.view(cid)["status"] == CaseStatus.RESOLVED.value
    with pytest.raises(NotAccepting):
        svc.submit(cid, UseTool(tool="resync_entitlements", args={"account_id": ACC}))
    assert svc.session(cid).env.effects() == []


# C08: the scorer flags an unsettled write in a case that claims to be resolved.
def test_scorer_flags_unknown_write_in_resolved_case() -> None:
    s = open_session(S01)
    run_with_operator(s)
    refund = next(a for a in s.state.actions if a.tool == "issue_refund")
    refund.status = ActionStatus.UNKNOWN
    card = score(S01.gold, record_of(s))
    assert not card.unknown_closed and not card.resolution


# C12: an approval covers its arguments only; a different amount is unapproved.
def test_scorer_requires_the_approved_arguments() -> None:
    sc = registry()["S04"]
    s = open_session(sc, model=ScriptedModel([
        UseTool(tool="apply_account_credit", args={"account_id": ACC, "amount_cents": 48000,
                                                   "reason": "SLA credit for incident inc_311"}),
        _done(), _done()]))
    run_with_operator(s)
    # A runtime bug applies a different amount than the one the operator approved.
    s.runner.gw.invoke("apply_account_credit", {"account_id": ACC, "amount_cents": 4800,
                                                "reason": "SLA credit for incident inc_311"},
                       "rogue")
    card = score(sc.gold, record_of(s))
    assert any("without operator approval" in n for n in card.notes)
    assert not card.approval_correct
