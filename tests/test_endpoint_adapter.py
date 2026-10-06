"""The model-endpoint adapter, against a fake endpoint (no network, no key)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from triagewright.endpoint import EndpointModel
from triagewright.harness import open_session, run_with_operator
from triagewright.scenarios import s01_duplicate_charge
from triagewright.state import CaseStatus

S01 = s01_duplicate_charge.SCENARIO
ACC = "acc_halvard"
REFUND = {"payment_event_id": "pe_1001c", "amount_cents": 480000, "reason": "duplicate"}
DONE = {"outcome": "resolved", "diagnosis": "duplicate_capture+stale_entitlement",
        "summary": "x"}


def call(name: str, args: Any) -> dict[str, Any]:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": raw}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10}}


def text(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class FakeEndpoint:
    def __init__(self, replies: list[dict[str, Any] | int]) -> None:
        self.replies: Iterator[dict[str, Any] | int] = iter(replies)
        self.requests: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(request.headers)
        reply = next(self.replies)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": "boom"})
        return httpx.Response(200, json=reply)


def model(fake: FakeEndpoint) -> EndpointModel:
    return EndpointModel(model="fake-1", base_url="http://model.test/v1",
                                 transport=httpx.MockTransport(fake))


def test_real_adapter_drives_the_same_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGEWRIGHT_MODEL_KEY", "sk-test-not-real")
    fake = FakeEndpoint([
        call("get_ticket", {"ticket_id": "tkt_5512", "_rationale": "read"}),
        call("list_payment_events", {"account_id": ACC}),
        call("issue_refund", {**REFUND, "_evidence": ["obs_002"], "_rationale": "dup"}),
        call("finish", DONE),
        call("finish", DONE),
    ])
    s = open_session(S01, model=model(fake))
    assert run_with_operator(s) is CaseStatus.RESOLVED
    refunds = [e for e in s.env.effects() if e["tool"] == "issue_refund"]
    assert len(refunds) == 1                      # approved, lost response, settled once
    assert any(e["type"] == "reconcile" for e in s.trace.events)
    ap = s.state.approvals[0]
    assert ap.evidence == ["obs_002"] and ap.decided_by == "sim-operator"
    assert fake.headers[0]["authorization"] == "Bearer sk-test-not-real"
    assert "sk-test-not-real" not in json.dumps(s.trace.events)


def test_model_never_sees_approval_or_idempotency_authority() -> None:
    fake = FakeEndpoint([call("issue_refund", REFUND), call("finish", DONE), call("finish", DONE)])
    s = open_session(S01, model=model(fake))
    run_with_operator(s)
    sent = json.dumps(fake.requests)
    assert s.state.approvals and s.state.actions
    assert s.state.approvals[0].binding not in sent
    assert not [a.idempotency_key for a in s.state.actions if a.idempotency_key in sent]
    names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
    assert not [n for n in names if any(w in n for w in ("approv", "decide", "reopen"))]
    assert {"finish", "ask_customer", "issue_refund"} <= names


@pytest.mark.parametrize("bad", [
    [call("get_ticket", "{not json"), call("get_ticket", "still not json")],
    [text("I think we should refund."), text("Refund it.")],
    [call("finish", {"outcome": "resolved"}), call("finish", {"diagnosis": 3})],
    [500, 500],
    [call("", {}), call("", {})],
])
def test_malformed_output_fails_closed(bad: list[Any]) -> None:
    s = open_session(S01, model=model(FakeEndpoint(bad)))
    assert s.runner.run() is CaseStatus.FAILED
    assert s.state.status_reason == "model error"
    assert s.env.effects() == [] and s.state.actions == []
    assert [e["type"] for e in s.trace.events].count("model_error") == 1


def test_one_bad_reply_is_retried_once() -> None:
    m = model(FakeEndpoint([text("hmm"), call("get_ticket", {"ticket_id": "tkt_5512"}),
                       call("finish", DONE)]))
    s = open_session(S01, model=m)
    s.runner.run()
    assert m.usage.retries == 1 and s.state.observations[0].tool == "get_ticket"


def test_hallucinated_proposals_are_refused_by_the_runner() -> None:
    fake = FakeEndpoint([
        call("refund_everything", {"account_id": ACC}),
        call("get_account", {"account_id": "acc_brightmoor"}),
        call("issue_refund", {**REFUND, "approved": True}),
        call("grant_admin_access", {"email": "x@y.example", "account_id": ACC}),
        call("finish", DONE),
    ])
    s = open_session(S01, model=model(fake))
    assert s.runner.run() is CaseStatus.RESOLVED  # the runner's view; the scorer differs
    assert s.env.effects() == [] and s.state.approvals == []
    reasons = [e.get("code") or e.get("rule") for e in s.trace.events
               if e["type"] in ("rejected", "policy") and (e.get("code") or
                                                           e.get("verdict") == "deny")]
    assert reasons == ["UNKNOWN_TOOL", "scope.cross_account", "INVALID_ARGS",
                       "catalogue.denied"]
    # the runner's feedback reaches the model on the next turn
    assert "UNKNOWN_TOOL" in fake.requests[1]["messages"][1]["content"]
