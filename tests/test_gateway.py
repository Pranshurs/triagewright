from __future__ import annotations

from fdeops.env.faults import FaultKind, FaultPlan, FaultRule
from fdeops.env.store import Environment
from fdeops.tools.base import Gateway, Outcome
from fdeops.tools.catalog import default_registry

REFUND = {"payment_event_id": "pe1", "amount_cents": 480000, "reason": "duplicate"}


def refunds(env: Environment) -> int:
    return sum(1 for e in env.snapshot()["payment_events"] if e["kind"] == "refund")


def test_timeout_after_effect_is_unknown_and_same_key_replays(env: Environment) -> None:
    g = Gateway(env, default_registry(),
                FaultPlan([FaultRule(tool="issue_refund", kind=FaultKind.TIMEOUT_AFTER_EFFECT)]))
    first = g.invoke("issue_refund", REFUND, "k")
    assert first.outcome is Outcome.UNKNOWN
    assert refunds(env) == 1  # the effect happened despite the timeout
    again = g.invoke("issue_refund", REFUND, "k")
    assert again.outcome is Outcome.OK and again.replayed
    assert refunds(env) == 1


def test_timeout_before_effect_changes_nothing(env: Environment) -> None:
    g = Gateway(env, default_registry(),
                FaultPlan([FaultRule(tool="issue_refund", kind=FaultKind.TIMEOUT_BEFORE_EFFECT)]))
    assert g.invoke("issue_refund", REFUND, "k").outcome is Outcome.UNKNOWN
    assert refunds(env) == 0


def test_key_reuse_with_different_args_conflicts(env: Environment) -> None:
    g = Gateway(env, default_registry())
    assert g.invoke("issue_refund", {**REFUND, "amount_cents": 100}, "k").outcome is Outcome.OK
    r = g.invoke("issue_refund", {**REFUND, "amount_cents": 200}, "k")
    assert r.error_code == "IDEMPOTENCY_CONFLICT"
    assert refunds(env) == 1


def test_over_refund_rejected(env: Environment) -> None:
    g = Gateway(env, default_registry())
    assert g.invoke("issue_refund", REFUND, "a").outcome is Outcome.OK
    assert g.invoke("issue_refund", REFUND, "b").error_code == "PRECONDITION"


def test_writes_require_a_key(env: Environment) -> None:
    g = Gateway(env, default_registry())
    assert g.invoke("issue_refund", REFUND).error_code == "IDEMPOTENCY_KEY_REQUIRED"


def test_denied_tools_never_execute(env: Environment) -> None:
    g = Gateway(env, default_registry())
    r = g.invoke("export_account_contacts", {"account_id": "acc_1"})
    assert r.error_code == "NOT_PERMITTED"


def test_invalid_args_rejected(env: Environment) -> None:
    g = Gateway(env, default_registry())
    assert g.invoke("get_account", {"account_id": "acc_1", "x": 1}).error_code == "INVALID_ARGS"


def test_provisioning_needs_entitlement_then_succeeds(env: Environment) -> None:
    g = Gateway(env, default_registry())
    first = g.invoke("retry_provisioning", {"workspace_id": "ws1"}, "p1")
    assert first.data and first.data["status"] == "failed"
    g.invoke("resync_entitlements", {"account_id": "acc_1"}, "e1")
    second = g.invoke("retry_provisioning", {"workspace_id": "ws1"}, "p2")
    assert second.data and second.data["status"] == "succeeded"


def test_scope_resolves_owning_account(env: Environment) -> None:
    g = Gateway(env, default_registry())
    assert g.scope_of("get_ticket", {"ticket_id": "t1"}) == "acc_1"
    assert g.scope_of("issue_refund", REFUND) == "acc_1"
    assert g.scope_of("search_runbooks", {"query": "refund"}) is None


def test_transient_fault_then_success(env: Environment) -> None:
    g = Gateway(env, default_registry(),
                FaultPlan([FaultRule(tool="get_account", kind=FaultKind.TRANSIENT_ERROR)]))
    assert g.invoke("get_account", {"account_id": "acc_1"}).retryable
    assert g.invoke("get_account", {"account_id": "acc_1"}).outcome is Outcome.OK


def test_runbook_search_finds_billing() -> None:
    from fdeops import runbooks
    assert runbooks.search("duplicate charge")[0]["runbook_id"] == "billing-duplicate-charges"
