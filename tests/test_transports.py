"""P3: transports carry decisions; only the runner decides.

Parity: the same proposals through the scripted loop, the service, HTTP and MCP must
produce the same runner decisions and the same upstream effects. Red arms: each
transport is attacked directly and must not reach around the runner.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from triagewright.api import create_app
from triagewright.harness import open_session
from triagewright.mcp_server import FINISH, build_server
from triagewright.model import Finish, ScriptedModel, UseTool
from triagewright.scenarios import s01_duplicate_charge
from triagewright.service import CaseService
from triagewright.state import Resolution
from triagewright.telemetry import otlp_json

ACC = "acc_halvard"
REFUND = {"payment_event_id": "pe_1001c", "amount_cents": 480000, "reason": "duplicate"}
RESOLUTION = {"outcome": "resolved", "diagnosis": "x", "summary": "x"}

# The proposals every transport sends, in order.
PROPOSALS: list[tuple[str, dict[str, Any]]] = [
    ("get_ticket", {"ticket_id": "tkt_5512"}),
    ("list_payment_events", {"account_id": ACC}),       # transient fault, retried
    ("get_account", {"account_id": "acc_brightmoor"}),  # cross-tenant: denied
    ("get_account", {"account_id": ACC, "plan": "x"}),  # extra argument: rejected
    ("approve_refund", {}),                              # no such tool
    ("export_account_contacts", {"account_id": ACC}),   # catalogue-denied
    ("resync_entitlements", {"account_id": ACC}),       # safe write
    ("issue_refund", REFUND),                            # needs approval
    ("issue_refund", REFUND),                            # already pending: refused
]


def _norm(events: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Transport-independent view of a trace: what the runner decided, not ids."""
    keep = []
    for e in events:
        if e["type"] in ("policy", "rejected", "tool_call", "action", "approval_requested",
                         "approval_decided", "reconcile", "case_status"):
            keep.append((e["type"], e.get("tool"), e.get("verdict"), e.get("rule"),
                         e.get("code"), e.get("outcome"), e.get("error_code"),
                         e.get("status"), e.get("approved"), e.get("replayed")))
    return keep


def _effects(effects: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["tool"], json.dumps(e["args"], sort_keys=True)) for e in effects]


def _decisions() -> list[UseTool | Finish]:
    return [*(UseTool(tool=t, args=a) for t, a in PROPOSALS),
            Finish(resolution=Resolution(**RESOLUTION))]


def via_script(tmp: Path) -> tuple[list[Any], list[Any]]:
    s = open_session(s01_duplicate_charge.SCENARIO, model=ScriptedModel(_decisions()))
    s.runner.run()
    ap = s.state.pending_approvals()[0]
    s.runner.decide_approval(ap.id, True, "op", expected_binding=ap.binding)
    return _norm(s.trace.events), _effects(s.env.effects())


def via_service(tmp: Path) -> tuple[list[Any], list[Any]]:
    svc = CaseService(tmp)
    cid = svc.create("S01", arm=None)
    for d in _decisions():
        svc.submit(cid, d)
    ap = svc.view(cid)["approvals"][0]
    svc.decide(cid, ap["id"], True, "op", ap["binding"])
    s = svc.session(cid)
    return _norm(s.trace.events), _effects(s.env.effects())


def via_http(tmp: Path) -> tuple[list[Any], list[Any]]:
    svc = CaseService(tmp)
    http = TestClient(create_app(svc))
    cid = http.post("/api/cases", json={"scenario": "S01", "arm": None}).json()["case_id"]
    for t, a in PROPOSALS:
        assert http.post(f"/api/cases/{cid}/decisions",
                         json={"kind": "use_tool", "tool": t, "args": a}).status_code == 200
    http.post(f"/api/cases/{cid}/decisions", json={"kind": "finish", "resolution": RESOLUTION})
    ap = http.get(f"/api/cases/{cid}").json()["approvals"][0]
    r = http.post(f"/api/cases/{cid}/approvals/{ap['id']}",
                  json={"approve": True, "operator": "op", "binding": ap["binding"]})
    assert r.status_code == 200, r.text
    return _norm(http.get(f"/api/cases/{cid}/trace").json()), _effects(
        svc.session(cid).env.effects())


def via_mcp(tmp: Path) -> tuple[list[Any], list[Any]]:
    svc = CaseService(tmp)
    cid = svc.create("S01", arm=None)

    async def drive() -> None:
        async with Client(build_server(svc, cid)) as c:
            for t, a in PROPOSALS:
                await c.call_tool(t, a)
            await c.call_tool(FINISH, RESOLUTION)

    asyncio.run(drive())
    ap = svc.view(cid)["approvals"][0]
    svc.decide(cid, ap["id"], True, "op", ap["binding"])
    s = svc.session(cid)
    return _norm(s.trace.events), _effects(s.env.effects())


TRANSPORTS: dict[str, Callable[[Path], tuple[list[Any], list[Any]]]] = {
    "service": via_service, "http": via_http, "mcp": via_mcp}


@pytest.mark.parametrize("name", list(TRANSPORTS))
def test_transport_parity(tmp_path: Path, name: str) -> None:
    ref_trace, ref_effects = via_script(tmp_path / "ref")
    trace, effects = TRANSPORTS[name](tmp_path / name)
    assert trace == ref_trace
    assert effects == ref_effects
    # sanity: the reference itself exercised every boundary
    kinds = {(e[0], e[2] or e[4]) for e in ref_trace}
    assert ("policy", "deny") in kinds and ("rejected", "INVALID_ARGS") in kinds
    assert ("rejected", "UNKNOWN_TOOL") in kinds and ("rejected", "WRITE_GUARD") in kinds
    assert [t for t, _ in ref_effects] == ["resync_entitlements", "issue_refund"]


# -- HTTP red arms ---------------------------------------------------------------------


@pytest.fixture
def http(tmp_path: Path) -> tuple[TestClient, CaseService]:
    svc = CaseService(tmp_path)
    return TestClient(create_app(svc)), svc


def _awaiting(http: TestClient) -> tuple[str, dict[str, Any]]:
    cid = http.post("/api/cases", json={"scenario": "S01", "arm": "good"}).json()["case_id"]
    assert http.post(f"/api/cases/{cid}/run").json()["status"] == "awaiting_approval"
    return cid, http.get(f"/api/cases/{cid}").json()["approvals"][0]


def _refunds(svc: CaseService, cid: str) -> int:
    return sum(1 for e in svc.session(cid).env.effects() if e["tool"] == "issue_refund")


def test_http_forged_account_is_not_a_field(http: tuple[TestClient, CaseService]) -> None:
    client, svc = http
    cid = client.post("/api/cases", json={"scenario": "S01", "arm": None}).json()["case_id"]
    r = client.post(f"/api/cases/{cid}/decisions", json={
        "kind": "use_tool", "tool": "list_invoices", "args": {"account_id": "acc_halvard"},
        "account_id": "acc_brightmoor"})
    assert r.status_code == 422
    r = client.post("/api/cases", json={"scenario": "S01", "arm": None,
                                        "account_id": "acc_brightmoor"})
    assert r.status_code == 422
    r = client.post(f"/api/cases/{cid}/decisions", json={
        "kind": "use_tool", "tool": "list_invoices", "args": {"account_id": "acc_brightmoor"}})
    assert r.json()["feedback"].startswith("denied by policy (scope)")
    assert svc.trace(cid)[-1]["rule"] == "scope.cross_account"
    assert svc.view(cid)["case"]["account_id"] == "acc_halvard"


def test_http_approval_from_another_case_is_refused(
        http: tuple[TestClient, CaseService]) -> None:
    client, svc = http
    a, ap_a = _awaiting(client)
    b, ap_b = _awaiting(client)
    assert ap_a["id"] == ap_b["id"]  # same local id, different cases
    r = client.post(f"/api/cases/{a}/approvals/{ap_a['id']}",
                    json={"approve": True, "operator": "op", "binding": ap_b["binding"]})
    assert r.status_code == 409
    assert svc.view(a)["approvals"][0]["status"] == "pending"
    assert _refunds(svc, a) == 0 and _refunds(svc, b) == 0


def test_http_decision_cannot_carry_arguments(http: tuple[TestClient, CaseService]) -> None:
    client, svc = http
    cid, ap = _awaiting(client)
    r = client.post(f"/api/cases/{cid}/approvals/{ap['id']}", json={
        "approve": True, "operator": "op", "binding": ap["binding"],
        "args": {**ap["args"], "amount_cents": 1}})
    assert r.status_code == 422
    assert _refunds(svc, cid) == 0


def test_http_stale_page_after_request_changed(http: tuple[TestClient, CaseService]) -> None:
    """The console rendered the approval, then the stored request changed. The server
    (not the page) must refuse the old decision."""
    client, svc = http
    cid, shown = _awaiting(client)
    stored = svc.session(cid).state.approvals[0]
    stored.args = {**stored.args, "amount_cents": 1}
    r = client.post(f"/api/cases/{cid}/approvals/{shown['id']}",
                    json={"approve": True, "operator": "op", "binding": shown["binding"]})
    assert r.status_code == 409
    assert _refunds(svc, cid) == 0
    assert any(e["type"] == "approval_stale" for e in svc.trace(cid))


def test_http_duplicate_approval_submission(http: tuple[TestClient, CaseService]) -> None:
    client, svc = http
    cid, ap = _awaiting(client)
    body = {"approve": True, "operator": "op", "binding": ap["binding"]}
    assert client.post(f"/api/cases/{cid}/approvals/{ap['id']}", json=body).status_code == 200
    assert client.post(f"/api/cases/{cid}/approvals/{ap['id']}", json=body).status_code == 409
    assert _refunds(svc, cid) == 1


def test_http_resume_after_restart(tmp_path: Path) -> None:
    first = TestClient(create_app(CaseService(tmp_path)))
    cid, ap = _awaiting(first)
    del first  # the process goes away; a new one starts on the same directory
    svc = CaseService(tmp_path)
    second = TestClient(create_app(svc))
    assert second.get(f"/api/cases/{cid}").json()["status"] == "awaiting_approval"
    r = second.post(f"/api/cases/{cid}/approvals/{ap['id']}",
                    json={"approve": True, "operator": "op", "binding": ap["binding"]})
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    assert _refunds(svc, cid) == 1


def test_http_has_no_route_that_executes_tools(http: tuple[TestClient, CaseService]) -> None:
    client, _svc = http
    posts = sorted(r.path for r in client.app.routes  # type: ignore[attr-defined]
                   if "POST" in getattr(r, "methods", set()))
    # recheck is an operator lookup of an unknown write; disconnect drops a connection.
    assert posts == ["/api/cases", "/api/cases/{case_id}/actions/{action_id}/recheck",
                     "/api/cases/{case_id}/approvals/{approval_id}",
                     "/api/cases/{case_id}/decisions", "/api/cases/{case_id}/run",
                     "/api/hubspot/disconnect"]


def test_http_unknown_case_and_path_tricks(http: tuple[TestClient, CaseService]) -> None:
    client, _svc = http
    assert client.get("/api/cases/nope").status_code == 404
    assert client.get("/api/cases/..%2Fetc").status_code == 404


# -- MCP red arms ----------------------------------------------------------------------


def _mcp(svc: CaseService, cid: str, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    async def go() -> list[Any]:
        async with Client(build_server(svc, cid)) as c:
            return [await c.call_tool(n, a) for n, a in calls]
    return asyncio.run(go())


def test_mcp_lists_no_operator_powers(tmp_path: Path) -> None:
    svc = CaseService(tmp_path)
    cid = svc.create("S01", arm=None)

    async def names() -> list[str]:
        async with Client(build_server(svc, cid)) as c:
            return [t.name for t in (await c.list_tools()).tools]
    listed = asyncio.run(names())
    assert not [n for n in listed if any(w in n for w in ("approv", "decide", "reopen"))]


def test_mcp_write_without_authorization_waits_for_operator(tmp_path: Path) -> None:
    svc = CaseService(tmp_path)
    cid = svc.create("S01", arm=None)
    (r,) = _mcp(svc, cid, [("issue_refund", REFUND)])
    assert "approval apr_001 requested" in r.content[0].text
    assert _refunds(svc, cid) == 0


def test_mcp_cross_tenant_malformed_and_substituted_names(tmp_path: Path) -> None:
    svc = CaseService(tmp_path)
    cid = svc.create("S01", arm=None)
    results = _mcp(svc, cid, [
        ("get_account", {"account_id": "acc_brightmoor"}),
        ("issue_refund", {**REFUND, "account_id": "acc_halvard"}),   # extra arg
        ("issue_refund", {"payment_event_id": "pe_1001c"}),          # missing args
        ("Issue_Refund", REFUND), ("issue_refund ", REFUND), ("approve_refund", REFUND),
        ("grant_admin_access", {"email": "x@y.example", "account_id": ACC}),
    ])
    assert all(r.is_error for r in results)
    assert svc.session(cid).env.effects() == []
    assert svc.view(cid)["approvals"] == []
    codes = [e.get("code") or e.get("rule") for e in svc.trace(cid)
             if e["type"] in ("rejected", "policy")]
    assert codes == ["scope.cross_account", "INVALID_ARGS", "INVALID_ARGS", "UNKNOWN_TOOL",
                     "UNKNOWN_TOOL", "UNKNOWN_TOOL", "catalogue.denied"]


def test_mcp_replay_of_an_approved_call(tmp_path: Path) -> None:
    # S01 injects a lost response on the refund: the approved call is settled under
    # its key, and a replay through MCP must not produce a second refund.
    svc = CaseService(tmp_path)
    cid = svc.create("S01", arm=None)
    _mcp(svc, cid, [("issue_refund", REFUND), (FINISH, RESOLUTION)])
    ap = svc.view(cid)["approvals"][0]
    svc.decide(cid, ap["id"], True, "op", ap["binding"])
    assert any(e["type"] == "reconcile" for e in svc.trace(cid))
    (again,) = _mcp(svc, cid, [("issue_refund", REFUND)])
    assert again.is_error and "already succeeded" in again.content[0].text
    assert _refunds(svc, cid) == 1


# -- telemetry -------------------------------------------------------------------------


def test_otel_export_carries_no_secrets_or_payloads(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGEWRIGHT_MODEL_KEY", "sk-test-triagewright-do-not-export")
    s = open_session(s01_duplicate_charge.SCENARIO)
    from triagewright.harness import run_with_operator
    run_with_operator(s)
    # Search what the export says, not when: timestamps are 19-digit wall-clock
    # numbers and can contain any digit run, so they are excluded from the search.
    export = otlp_json(s.trace.events, "case_s01", s.trace.times)
    blob = json.dumps(_without_timestamps(export))
    secrets = ["sk-test-triagewright", s.state.approvals[0].binding,
               *(a.idempotency_key for a in s.state.actions),
               "ines.varga@halvard.example", "charged twice", "480000", "4,800",
               "duplicate capture of invoice", "verified duplicate"]
    assert not [x for x in secrets if x in blob]
    spans = export["resourceSpans"][0]["scopeSpans"][0]["spans"]
    ids = {sp["spanId"] for sp in spans}
    assert all(len(sp["traceId"]) == 32 and len(sp["spanId"]) == 16 for sp in spans)
    assert all(sp.get("parentSpanId") in ids for sp in spans[1:])
    assert any(sp["name"] == "reconcile act_004" for sp in spans)


def _without_timestamps(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _without_timestamps(v) for k, v in node.items()
                if not k.endswith("UnixNano")}
    if isinstance(node, list):
        return [_without_timestamps(v) for v in node]
    return node


def test_otel_secret_check_ignores_timestamps_only() -> None:
    """The regression check above must not be satisfied by stripping content: an amount
    placed in an attribute is still found, and one inside a timestamp is not."""
    leaky = {"spans": [{"startTimeUnixNano": "1759784800000480000",
                        "attributes": [{"key": "x", "value": {"intValue": "480000"}}]}]}
    assert "480000" in json.dumps(_without_timestamps(leaky))
    clean = {"spans": [{"startTimeUnixNano": "1759784800000480000", "attributes": []}]}
    assert "480000" not in json.dumps(_without_timestamps(clean))


class _Broken:
    def export(self, payload: dict[str, Any]) -> None:
        raise ConnectionError("collector down")


def test_telemetry_failure_changes_nothing(tmp_path: Path) -> None:
    plain, broken = CaseService(tmp_path / "a"), CaseService(tmp_path / "b", exporter=_Broken())
    out = []
    for svc in (plain, broken):
        cid = svc.create("S01", arm="good")
        svc.run(cid)
        ap = svc.view(cid)["approvals"][0]
        svc.decide(cid, ap["id"], True, "op", ap["binding"])
        out.append((svc.view(cid)["status"], _norm(svc.trace(cid)),
                    _effects(svc.session(cid).env.effects())))
    assert out[0] == out[1] and out[0][0] == "resolved"
