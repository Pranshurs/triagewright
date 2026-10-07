"""A process dying mid-write must not let a restarted process mint a fresh key."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from triagewright.env.faults import FaultKind, FaultRule, SimulatedCrash
from triagewright.harness import open_session, resume_session, run_with_operator
from triagewright.scenarios import s01_duplicate_charge
from triagewright.state import ActionStatus, ApprovalStatus, CaseState, CaseStatus

S01 = s01_duplicate_charge.SCENARIO


@pytest.mark.parametrize("kind", [FaultKind.CRASH_AFTER_EFFECT, FaultKind.CRASH_BEFORE_EFFECT])
def test_crash_during_approved_refund_settles_on_restart(tmp_path: Path, kind: FaultKind) -> None:
    sc = dataclasses.replace(S01, faults=(FaultRule(tool="issue_refund", kind=kind),))
    s = open_session(sc, out_dir=tmp_path)
    assert s.runner.run() is CaseStatus.AWAITING_APPROVAL
    ap_id = s.state.pending_approvals()[0].id
    with pytest.raises(SimulatedCrash):
        s.runner.decide_approval(ap_id, True, "op")
    s.env.close()

    # What the dead process left on disk: the dispatched action, its key, and the
    # approval already consumed.
    disk = CaseState.model_validate_json((tmp_path / "state.json").read_text())
    rec = disk.actions[-1]
    assert rec.tool == "issue_refund" and rec.status is ActionStatus.UNKNOWN
    ap = next(a for a in disk.approvals if a.id == ap_id)
    assert ap.status is ApprovalStatus.EXECUTED and ap.action_id == rec.id

    r = resume_session(sc, tmp_path)
    assert r.runner.run() is CaseStatus.RESOLVED
    effects = [e for e in r.env.effects() if e["tool"] == "issue_refund"]
    assert len(effects) == 1
    assert effects[0]["idempotency_key"] == rec.idempotency_key  # the persisted key
    settled = next(a for a in r.state.actions if a.id == rec.id)
    assert settled.status is ActionStatus.SUCCEEDED
    assert settled.replayed is (kind is FaultKind.CRASH_AFTER_EFFECT)
    assert len(r.trace.of("resume_settlement")) == 1
    assert len([a for a in r.state.actions if a.tool == "issue_refund"]) == 1


def test_restart_does_not_reexecute_or_reapprove(tmp_path: Path) -> None:
    s = open_session(S01, out_dir=tmp_path)
    run_with_operator(s)
    s.env.close()
    r = resume_session(S01, tmp_path)
    assert r.runner.run() is CaseStatus.RESOLVED
    assert len([e for e in r.env.effects() if e["tool"] == "issue_refund"]) == 1


def test_case_opened_but_not_yet_driven_survives_a_restart(tmp_path: Path) -> None:
    """`open --external` in one process, the agent's transport in another."""
    from triagewright.service import CaseService

    cid = CaseService(tmp_path).create("S01", arm=None)
    later = CaseService(tmp_path)
    assert later.view(cid)["status"] == "investigating"
    assert [c["case_id"] for c in later.cases()] == [cid]
    assert [e["type"] for e in later.trace(cid)] == ["case_opened", "case_status"]
