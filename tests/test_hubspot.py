"""The HubSpot connector against a local fake of HubSpot (no network, no account).

Every case here goes through CaseService and the runner, so the connector is held to
the same scope, approval and unknown-outcome rules as the simulated tools.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fake_hubspot import CLIENT_ID, CLIENT_SECRET, FakeHubSpot
from fastapi.testclient import TestClient
from mcp.client import Client

from triagewright.api import create_app
from triagewright.env.faults import FaultKind, FaultPlan, FaultRule, SimulatedCrash
from triagewright.hubspot import DEFAULT_SCOPES
from triagewright.hubspot.client import HubSpotClient
from triagewright.hubspot.oauth import AuthError, HubSpotAuth, NotConnected, Settings
from triagewright.mcp_server import build_server
from triagewright.model import Finish, UseTool
from triagewright.service import CaseService
from triagewright.state import Resolution
from triagewright.telemetry import otlp_json
from triagewright.tools.base import ToolError

ACC, OTHER = "acc_halvard", "acc_brightmoor"
TICKET, FOREIGN_TICKET, ORPHAN_TICKET = "501", "502", "503"
NOTE = {"ticket_id": TICKET, "body": "Checked <billing> & plan.\nNothing to refund."}
REDIRECT = "http://localhost:8000/api/hubspot/callback"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> Iterator[FakeHubSpot]:
    f = FakeHubSpot()
    f.add("companies", "301", name="Halvard Logistics", domain="halvard.example")
    f.add("companies", "302", name="Brightmoor Analytics", domain="brightmoor.example")
    f.add("companies", "303", name="Unlinked Co", domain="unlinked.example")
    f.add("contacts", "201", firstname="Ines", lastname="Varga",
          email="ines.varga@halvard.example")
    f.add("contacts", "202", firstname="Ravi", lastname="Shah",
          email="ravi@brightmoor.example")
    f.add("tickets", TICKET, subject="Invoice query", content="Please check October.",
          hs_pipeline_stage="1", hs_ticket_priority="HIGH", createdate="2026-10-06")
    f.add("tickets", FOREIGN_TICKET, subject="Brightmoor secret", content="confidential-b")
    f.add("tickets", ORPHAN_TICKET, subject="No company", content="orphan")
    f.associate("tickets", TICKET, "companies", "301")
    f.associate("tickets", TICKET, "contacts", "201")
    f.associate("contacts", "201", "companies", "301")
    f.associate("tickets", FOREIGN_TICKET, "companies", "302")
    f.associate("contacts", "202", "companies", "302")
    f.associate("tickets", ORPHAN_TICKET, "companies", "303")
    yield f
    f.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def auth(fake: FakeHubSpot, tmp_path: Path, clock: Clock) -> HubSpotAuth:
    settings = Settings(client_id=CLIENT_ID, client_secret=CLIENT_SECRET,
                        store=tmp_path / "private" / "hubspot.json", redirect_uri=REDIRECT,
                        authorize_url=fake.base + "/oauth/authorize", oauth_base=fake.base,
                        api_base=fake.base)
    return HubSpotAuth(settings, clock=clock)


def consent(fake: FakeHubSpot, url: str) -> dict[str, str]:
    """Play the account owner: visit the authorize URL, return the callback's query."""
    r = httpx.get(url, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(REDIRECT)
    return {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}


def connect(fake: FakeHubSpot, auth: HubSpotAuth) -> None:
    cb = consent(fake, auth.begin())
    auth.complete(cb.get("state"), cb.get("code"), cb.get("error"))
    auth.link(ACC, "301")
    auth.link(OTHER, "302")


@pytest.fixture
def hub(fake: FakeHubSpot, auth: HubSpotAuth) -> HubSpotClient:
    connect(fake, auth)
    return HubSpotClient(auth, lookup_delay=0, max_backoff=0)


@pytest.fixture
def svc(tmp_path: Path, hub: HubSpotClient) -> CaseService:
    return CaseService(tmp_path / "runs", hubspot=hub)


def stored(auth: HubSpotAuth) -> dict[str, Any]:
    return dict(json.loads(auth.settings.store.read_text()))


# -- OAuth ------------------------------------------------------------------------------


def test_authorize_url_carries_state_and_scopes_but_no_secret(auth: HubSpotAuth) -> None:
    q = {k: v[0] for k, v in parse_qs(urlsplit(auth.begin()).query).items()}
    assert q["client_id"] == CLIENT_ID and q["redirect_uri"] == REDIRECT
    assert q["scope"].split() == list(DEFAULT_SCOPES)
    assert len(q["state"]) >= 43 and set(q) == {"client_id", "redirect_uri", "scope", "state"}
    assert auth.begin() != auth.begin()


def test_code_exchange_stores_tokens_owner_only(fake: FakeHubSpot, auth: HubSpotAuth) -> None:
    connect(fake, auth)
    status = auth.status()
    assert status["connected"] and status["hub_id"] == 4242
    assert set(status["scopes"]) == set(DEFAULT_SCOPES)
    assert "hs-" not in json.dumps(status)
    path = auth.settings.store
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stored(auth)["refresh_token"] in fake.refresh_tokens
    # Credentials travel in the body of the token request, never in a URL.
    assert all(CLIENT_SECRET not in p for _m, p in fake.requests)
    assert fake.token_requests[0]["grant_type"] == "authorization_code"


@pytest.mark.parametrize("tamper", ["wrong", "missing", "replayed", "expired"])
def test_callback_state_is_required_single_use_and_short_lived(
        fake: FakeHubSpot, auth: HubSpotAuth, clock: Clock, tamper: str) -> None:
    cb = consent(fake, auth.begin())
    state: str | None = cb["state"]
    if tamper == "wrong":
        state = cb["state"][:-1] + ("A" if cb["state"][-1] != "A" else "B")
    elif tamper == "missing":
        state = None
    elif tamper == "replayed":
        auth.complete(cb["state"], cb["code"])
        fake.token_requests.clear()
        cb = {**consent(fake, auth.begin()), "state": cb["state"]}
        state = cb["state"]
        auth.disconnect()
    else:
        clock.now += 601
    with pytest.raises(AuthError) as e:
        auth.complete(state, cb["code"])
    assert e.value.code == "INVALID_STATE"
    assert fake.token_requests == []          # the code was never sent anywhere
    assert not auth.status()["connected"]


def test_denied_consent_and_short_scopes_store_nothing(fake: FakeHubSpot,
                                                       auth: HubSpotAuth) -> None:
    fake.deny = True
    cb = consent(fake, auth.begin())
    with pytest.raises(AuthError) as e:
        auth.complete(cb.get("state"), cb.get("code"), cb.get("error"))
    assert e.value.code == "AUTHORIZATION_DENIED"
    fake.deny, fake.grant_scopes = False, ["crm.objects.contacts.read"]
    cb = consent(fake, auth.begin())
    with pytest.raises(AuthError) as e:
        auth.complete(cb["state"], cb["code"])
    assert e.value.code == "MISSING_SCOPES" and "crm.objects.contacts.write" in e.value.message
    assert not auth.status()["connected"] and stored(auth)["refresh_token"] is None


def test_access_token_is_refreshed_before_it_expires(fake: FakeHubSpot, auth: HubSpotAuth,
                                                     clock: Clock) -> None:
    connect(fake, auth)
    first = auth.access_token()
    clock.now += 1700
    assert auth.access_token() == first       # still inside its lifetime
    clock.now += 60                           # within the skew before expiry
    second = auth.access_token()
    assert second != first and second in fake.access_tokens
    assert [r["grant_type"] for r in fake.token_requests] == ["authorization_code",
                                                              "refresh_token"]


def test_rejected_token_is_refreshed_once_and_the_read_succeeds(
        fake: FakeHubSpot, hub: HubSpotClient) -> None:
    fake.expire_access_tokens()               # HubSpot stops honouring it early
    assert hub.get("companies", "301", ("name",))["name"] == "Halvard Logistics"
    assert fake.token_requests[-1]["grant_type"] == "refresh_token"


def test_revoked_grant_needs_reauthorization_and_forgets_tokens(
        fake: FakeHubSpot, hub: HubSpotClient) -> None:
    fake.revoke_grant()
    with pytest.raises(NotConnected) as e:
        hub.auth.access_token(force_refresh=True)
    assert e.value.code == "REAUTHORIZATION_REQUIRED"
    st = hub.auth.status()
    assert not st["connected"] and st["needs_reauthorization"]
    assert stored(hub.auth)["access_token"] is None
    connect(fake, hub.auth)                   # a fresh consent recovers
    assert hub.auth.status()["connected"] and not hub.auth.status()["needs_reauthorization"]


def test_token_endpoint_trouble_is_transient_and_keeps_the_grant(
        fake: FakeHubSpot, hub: HubSpotClient, clock: Clock) -> None:
    clock.now += 4000
    fake.fail("POST", "/token", "status:503")
    with pytest.raises(AuthError) as e:
        hub.auth.access_token()
    assert e.value.transient and hub.auth.status()["connected"]
    fake.fail("POST", "/token", "malformed")
    with pytest.raises(AuthError) as e:
        hub.auth.access_token()
    assert e.value.code == "MALFORMED_TOKEN_RESPONSE" and hub.auth.status()["connected"]
    assert hub.auth.access_token() in fake.access_tokens


def test_disconnect_revokes_upstream_and_keeps_links_and_marker(
        fake: FakeHubSpot, hub: HubSpotClient) -> None:
    marker = hub.auth.marker("tw_key")
    assert hub.auth.disconnect() == {"connected": False, "revoked_upstream": True}
    assert fake.refresh_tokens == {} and not hub.auth.status()["connected"]
    assert hub.auth.status()["links"] == {"301": ACC, "302": OTHER}
    assert hub.auth.marker("tw_key") == marker


def test_oauth_routes_complete_a_connection_without_showing_tokens(
        fake: FakeHubSpot, auth: HubSpotAuth, tmp_path: Path) -> None:
    client = TestClient(create_app(CaseService(tmp_path / "runs", hubspot=HubSpotClient(auth))))
    assert client.get("/api/hubspot/status").json()["connected"] is False
    go = client.get("/api/hubspot/connect", follow_redirects=False)
    assert go.status_code == 302
    cb = consent(fake, go.headers["location"])
    assert client.get("/api/hubspot/callback", params={**cb, "state": "x"}).status_code == 400
    done = client.get("/api/hubspot/callback", params=cb)
    assert done.status_code == 200 and done.json()["connected"] is True
    assert client.get("/api/hubspot/callback", params=cb).status_code == 400   # replay
    assert "hs-" not in done.text and CLIENT_SECRET not in done.text
    assert client.post("/api/hubspot/disconnect").json()["revoked_upstream"] is True


def test_without_a_connector_nothing_changes(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRIAGEWRIGHT_HUBSPOT_CLIENT_ID", raising=False)
    plain = CaseService(tmp_path / "runs")
    assert plain.hubspot is None
    assert not [t for t in plain.tools() if t["name"].startswith("hubspot_")]
    client = TestClient(create_app(plain))
    assert client.get("/api/hubspot/status").status_code == 404
    assert client.post("/api/cases", json={"scenario": "HUBSPOT",
                                           "hubspot_ticket": TICKET}).status_code == 400


def test_settings_come_from_the_environment_only() -> None:
    assert Settings.from_env({}) is None
    s = Settings.from_env({"TRIAGEWRIGHT_HUBSPOT_CLIENT_ID": "id",
                           "TRIAGEWRIGHT_HUBSPOT_CLIENT_SECRET": "s3cret",
                           "TRIAGEWRIGHT_HUBSPOT_STORE": "/tmp/x.json",
                           "TRIAGEWRIGHT_HUBSPOT_SCOPES": "a b"})
    assert s is not None and s.scopes == ("a", "b") and "s3cret" not in repr(s)


# -- the connector behind the runner ---------------------------------------------------


def open_case(svc: CaseService, external: bool = False) -> str:
    return svc.create("HUBSPOT", None if external else "walkthrough", TICKET)


def approve(svc: CaseService, cid: str, yes: bool = True) -> dict[str, Any]:
    (ap,) = [a for a in svc.view(cid)["approvals"] if a["status"] == "pending"]
    return svc.decide(cid, ap["id"], yes, "op", ap["binding"])


def action(svc: CaseService, cid: str) -> dict[str, Any]:
    (a,) = svc.view(cid)["actions"]
    return dict(a)


def test_walkthrough_reads_then_writes_only_after_approval(fake: FakeHubSpot,
                                                           svc: CaseService) -> None:
    cid = open_case(svc)
    assert svc.run(cid) == "awaiting_approval"
    tools = [o["tool"] for o in svc.view(cid)["observations"]]
    assert tools == ["hubspot_get_ticket", "hubspot_get_company", "hubspot_get_contact"]
    ticket = svc.view(cid)["observations"][0]["result"]["data"]
    assert ticket["ticket"]["subject"] == "Invoice query" and ticket["company_ids"] == ["301"]
    (policy,) = [e for e in svc.trace(cid) if e["type"] == "policy"
                 and e["tool"] == "hubspot_add_ticket_note"]
    assert policy["verdict"] == "needs_approval"
    assert fake.journal == [] and fake.posts("/notes") == 0

    assert approve(svc, cid)["status"] == "resolved"
    (note,) = fake.journal
    assert note["associations"][0]["to"]["id"] == TICKET
    assert action(svc, cid)["status"] == "succeeded"
    key = svc.session(cid).state.actions[0].idempotency_key
    body = note["properties"]["hs_note_body"]
    assert svc.hubspot is not None
    assert body.endswith("ref: " + svc.hubspot.auth.marker(key)) and key not in body


def test_rejected_note_is_never_sent(fake: FakeHubSpot, svc: CaseService) -> None:
    cid = open_case(svc)
    svc.run(cid)
    approve(svc, cid, yes=False)
    assert fake.journal == [] and fake.posts("/notes") == 0 and svc.view(cid)["actions"] == []


def test_note_body_is_escaped(fake: FakeHubSpot, svc: CaseService) -> None:
    cid = open_case(svc, external=True)
    svc.submit(cid, UseTool(tool="hubspot_add_ticket_note", args=NOTE))
    svc.submit(cid, Finish(resolution=Resolution(outcome="resolved", diagnosis="d",
                                                 summary="s")))
    approve(svc, cid)
    body = fake.journal[0]["properties"]["hs_note_body"]
    assert body.startswith("Checked &lt;billing&gt; &amp; plan.<br>Nothing to refund.<br><br>")


@pytest.mark.parametrize("tool,args", [
    ("hubspot_get_ticket", {"ticket_id": FOREIGN_TICKET}),
    ("hubspot_get_ticket", {"ticket_id": ORPHAN_TICKET}),
    ("hubspot_get_ticket", {"ticket_id": "999"}),
    ("hubspot_get_contact", {"contact_id": "202"}),
    ("hubspot_get_company", {"company_id": "302"}),
    ("hubspot_get_company", {"company_id": "303"}),
    ("hubspot_add_ticket_note", {"ticket_id": FOREIGN_TICKET, "body": "hello"}),
])
def test_records_of_other_or_unlinked_companies_are_out_of_scope(
        fake: FakeHubSpot, svc: CaseService, tool: str, args: dict[str, Any]) -> None:
    cid = open_case(svc, external=True)
    out = svc.submit(cid, UseTool(tool=tool, args=args))
    assert out["feedback"] == f"denied by policy (scope): {tool} is not available for this case"
    assert out["observations"] == [] and svc.view(cid)["approvals"] == []
    blob = json.dumps(svc.trace(cid)) + json.dumps(svc.view(cid))
    assert "Brightmoor secret" not in blob and "confidential-b" not in blob
    assert fake.posts("/notes") == 0


def test_ticket_shared_between_two_accounts_has_no_scope(fake: FakeHubSpot,
                                                         svc: CaseService) -> None:
    fake.associate("tickets", TICKET, "companies", "302")
    cid = open_case(svc, external=True)
    out = svc.submit(cid, UseTool(tool="hubspot_get_ticket", args={"ticket_id": TICKET}))
    assert "denied by policy (scope)" in out["feedback"] and out["observations"] == []


def test_ids_cannot_carry_a_path(svc: CaseService) -> None:
    cid = open_case(svc, external=True)
    out = svc.submit(cid, UseTool(tool="hubspot_get_ticket",
                                  args={"ticket_id": "501/associations/notes"}))
    assert out["feedback"].startswith("rejected: INVALID_ARGS")


# -- unknown outcomes ------------------------------------------------------------------


def dispatch(svc: CaseService, fake: FakeHubSpot, *faults: tuple[str, str, str]) -> str:
    cid = open_case(svc)
    svc.run(cid)
    for method, fragment, kind in faults:
        fake.fail(method, fragment, kind)
    approve(svc, cid)
    return cid


@pytest.mark.parametrize("kind", ["drop_after", "status_after:500", "status_after:502",
                                  "malformed"])
def test_lost_response_after_effect_is_found_not_resent(fake: FakeHubSpot, svc: CaseService,
                                                        kind: str) -> None:
    cid = dispatch(svc, fake, ("POST", "/notes", kind))
    a = action(svc, cid)
    assert a["status"] == "succeeded" and a["replayed"] and a["reconcile_attempts"] == 1
    assert len(fake.journal) == 1 and fake.posts("objects/2026-09/notes") - \
        fake.posts("batch/read") == 1          # one create; the rest were lookups
    assert json.loads(a["detail"])["id"] == fake.journal[0]["id"]
    assert svc.view(cid)["status"] == "resolved"
    kinds = [e["type"] for e in svc.trace(cid)]
    assert kinds.count("reconcile") == 1


def test_lost_request_before_effect_stays_unknown_and_is_not_resent(
        fake: FakeHubSpot, svc: CaseService) -> None:
    cid = dispatch(svc, fake, ("POST", "/notes", "drop_before"))
    a = action(svc, cid)
    assert a["status"] == "unknown" and a["reconcile_attempts"] == 3
    assert "NOT_VISIBLE" in a["detail"]
    assert fake.journal == [] and fake.posts("/notes") - fake.posts("batch/read") == 1
    assert svc.view(cid)["status"] == "needs_attention"     # never reported as failed
    # Asking again is still only a lookup.
    again = svc.recheck(cid, a["id"], "op")
    assert again["action"]["status"] == "unknown" and fake.journal == []
    assert fake.posts("/notes") - fake.posts("batch/read") == 1


def test_identical_write_is_refused_while_the_first_is_unknown(fake: FakeHubSpot,
                                                               svc: CaseService) -> None:
    cid = open_case(svc, external=True)
    svc.submit(cid, UseTool(tool="hubspot_add_ticket_note", args=NOTE))
    fake.fail("POST", "/notes", "drop_before")
    svc.submit(cid, Finish(resolution=Resolution(outcome="resolved", diagnosis="d",
                                                 summary="s")))
    approve(svc, cid)
    assert action(svc, cid)["status"] == "unknown"
    out = svc.submit(cid, UseTool(tool="hubspot_add_ticket_note", args=NOTE))
    assert "unknown outcome" in out["feedback"]
    assert fake.journal == [] and len(svc.view(cid)["approvals"]) == 1


def test_operator_recheck_settles_once_the_note_is_visible(fake: FakeHubSpot,
                                                           svc: CaseService) -> None:
    # Applied, the answer was lost, and every lookup during the run failed too.
    cid = dispatch(svc, fake, ("POST", "/notes", "drop_after"),
                   *[("GET", "/associations/notes", "status:503")] * 3)
    a = action(svc, cid)
    assert a["status"] == "unknown" and len(fake.journal) == 1
    assert svc.view(cid)["status"] == "needs_attention"
    done = svc.recheck(cid, a["id"], "op")
    assert done["action"]["status"] == "succeeded" and len(fake.journal) == 1
    assert [e["operator"] for e in svc.trace(cid) if e["type"] == "operator_recheck"] == ["op"]
    with pytest.raises(ValueError):
        svc.recheck(cid, a["id"], "op")       # nothing left to settle


def test_two_notes_with_the_marker_are_ambiguous(fake: FakeHubSpot, svc: CaseService) -> None:
    cid = dispatch(svc, fake, ("POST", "/notes", "drop_after"),
                   *[("GET", "/associations/notes", "status:503")] * 3)
    fake._create_note(fake.journal[0] | {})   # an operator duplicated it by hand
    out = svc.recheck(cid, action(svc, cid)["id"], "op")
    assert out["action"]["status"] == "unknown" and "AMBIGUOUS" in out["action"]["detail"]


def test_marker_cannot_be_forged_from_what_the_agent_knows(fake: FakeHubSpot,
                                                           svc: CaseService) -> None:
    cid = dispatch(svc, fake, ("POST", "/notes", "drop_before"))
    key = svc.session(cid).state.actions[0].idempotency_key
    unkeyed = "tw-" + hmac.new(b"", key.encode(), hashlib.sha256).hexdigest()[:32]
    fake._create_note({"properties": {"hs_note_body": f"ref: {key} ref: {unkeyed}"},
                       "associations": [{"to": {"id": TICKET}, "types": [
                           {"associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 228}]}]})
    assert svc.recheck(cid, "act_001", "op")["action"]["status"] == "unknown"


def test_lookup_that_cannot_see_every_note_does_not_conclude(fake: FakeHubSpot,
                                                             svc: CaseService) -> None:
    assert svc.hubspot is not None
    svc.hubspot.max_scan = 150
    for i in range(260):
        fake.add("notes", str(7000 + i), hs_note_body="older note")
        fake.associate("tickets", TICKET, "notes", str(7000 + i))
    cid = dispatch(svc, fake, ("POST", "/notes", "drop_after"))
    a = action(svc, cid)
    assert a["status"] == "unknown" and "LOOKUP_INCOMPLETE" in a["detail"]
    svc.hubspot.max_scan = 500
    assert svc.recheck(cid, a["id"], "op")["action"]["status"] == "succeeded"


@pytest.mark.parametrize("kind,code", [("status:429", "RATE_LIMITED"),
                                       ("status:423", "RATE_LIMITED"),
                                       ("status:403", "HUBSPOT_FORBIDDEN"),
                                       ("status:400", "HUBSPOT_REJECTED")])
def test_refused_write_is_a_definite_failure(fake: FakeHubSpot, svc: CaseService, kind: str,
                                             code: str) -> None:
    cid = dispatch(svc, fake, ("POST", "/notes", kind))
    a = action(svc, cid)
    assert a["status"] == "failed" and a["detail"].startswith(code)
    assert a["reconcile_attempts"] == 0 and fake.journal == []


def test_unreachable_host_is_a_definite_failure_not_an_unknown(fake: FakeHubSpot,
                                                              hub: HubSpotClient) -> None:
    hub.auth.access_token()
    fake.close()                              # connection refused: nothing can be sent
    with pytest.raises(ToolError) as e:
        hub.create_note(TICKET, "x", "tw-marker")
    assert e.value.code == "UPSTREAM_UNREACHABLE" and e.value.retryable


def test_unreachable_host_voids_an_approved_write(fake: FakeHubSpot, svc: CaseService) -> None:
    cid = open_case(svc)
    svc.run(cid)
    fake.close()
    approve(svc, cid)                         # scope cannot be re-derived: not dispatched
    assert svc.view(cid)["actions"] == []
    assert [e["type"] for e in svc.trace(cid)].count("approval_void") == 1


def test_write_with_a_stale_token_refreshes_and_applies_once(fake: FakeHubSpot,
                                                             svc: CaseService) -> None:
    cid = open_case(svc)
    svc.run(cid)
    fake.expire_access_tokens()
    approve(svc, cid)
    assert action(svc, cid)["status"] == "succeeded" and len(fake.journal) == 1


def test_crash_after_the_note_is_sent_is_settled_by_lookup_on_restart(
        fake: FakeHubSpot, svc: CaseService) -> None:
    cid = open_case(svc)
    svc.run(cid)
    svc.session(cid).runner.gw.faults = FaultPlan([FaultRule(
        tool="hubspot_add_ticket_note", kind=FaultKind.CRASH_AFTER_EFFECT)])
    with pytest.raises(SimulatedCrash):
        approve(svc, cid)
    assert len(fake.journal) == 1
    svc.evict(cid)                            # a new process
    a = action(svc, cid)
    assert a["status"] == "succeeded" and a["replayed"] and len(fake.journal) == 1
    assert fake.posts("/notes") - fake.posts("batch/read") == 1


def test_revoked_grant_mid_case_denies_and_writes_nothing(fake: FakeHubSpot,
                                                          svc: CaseService) -> None:
    cid = open_case(svc)
    svc.run(cid)
    fake.revoke_grant()
    approve(svc, cid)
    # Scope can no longer be resolved, so the approved write is voided, not dispatched.
    assert svc.view(cid)["actions"] == [] and fake.journal == []
    assert svc.hubspot is not None and svc.hubspot.auth.status()["needs_reauthorization"]


# -- reads -----------------------------------------------------------------------------


def test_transient_read_errors_are_retried(fake: FakeHubSpot, svc: CaseService) -> None:
    cid = open_case(svc, external=True)
    fake.fail("GET", "/companies/301", "status:503", "status:429")
    out = svc.submit(cid, UseTool(tool="hubspot_get_company", args={"company_id": "301"}))
    (o,) = out["observations"]
    assert o["ok"] and o["attempts"] == 3 and o["result"]["data"]["company"]["id"] == "301"


def test_persistent_read_failure_is_an_error_observation(fake: FakeHubSpot,
                                                         svc: CaseService) -> None:
    cid = open_case(svc, external=True)
    fake.fail("GET", "/companies/301", "malformed", "drop_after", "status:500")
    out = svc.submit(cid, UseTool(tool="hubspot_get_company", args={"company_id": "301"}))
    (o,) = out["observations"]
    assert not o["ok"] and o["attempts"] == 3 and o["result"]["retryable"]
    assert o["result"]["error_code"] == "UPSTREAM_ERROR"
    assert "correlationId=" in o["result"]["error"]


def test_rate_limit_wait_is_bounded(fake: FakeHubSpot, hub: HubSpotClient) -> None:
    waits: list[float] = []
    hub._sleep, hub.max_backoff = waits.append, 2.0
    fake.fail("GET", "/companies/301", "status:429")
    with pytest.raises(Exception, match="RATE_LIMITED"):
        hub.get("companies", "301", ("name",))
    assert waits == [2.0]                     # HubSpot asked for 7 s


# -- nothing secret leaves -------------------------------------------------------------


def test_no_token_secret_or_key_reaches_any_output(
        fake: FakeHubSpot, svc: CaseService, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    assert svc.hubspot is not None
    auth = svc.hubspot.auth
    cid = open_case(svc)
    svc.run(cid)
    fake.expire_access_tokens()               # forces a 401 whose body quotes the token
    fake.fail("POST", "/notes", "drop_after")
    approve(svc, cid)
    cid2 = open_case(svc, external=True)
    fake.expire_access_tokens()
    fake.revoke_grant()
    svc.submit(cid2, UseTool(tool="hubspot_get_ticket", args={"ticket_id": TICKET}))

    s = svc.session(cid)
    key = s.state.actions[0].idempotency_key
    root = Path(svc.root)
    surfaces = {
        "files": "".join(p.read_text(errors="replace") for p in root.rglob("*")
                         if p.is_file() and p.suffix != ".sqlite3"),
        "view": json.dumps([svc.view(c) for c in (cid, cid2)]),
        "trace": json.dumps([svc.trace(c) for c in (cid, cid2)]),
        "record": svc.record(cid) + svc.record(cid2),
        "score": json.dumps(svc.score(cid)),
        "otel": json.dumps(otlp_json(s.trace.events, cid, s.trace.times)),
        "status": json.dumps(auth.status()),
        "logs": caplog.text,
        "repr": repr(auth.settings) + repr(auth._load()),
    }
    secrets = [CLIENT_SECRET, "hs-access-", "hs-refresh-", auth._load().marker_secret]
    for name, text in surfaces.items():
        assert not [x for x in secrets if x in text], name
    # The runner's key and the upstream marker stay out of telemetry and out of HubSpot.
    assert key not in surfaces["otel"] and auth.marker(key) not in surfaces["otel"]
    assert all(key not in json.dumps(n) for n in fake.journal)
    assert not list(root.rglob("hubspot.json"))   # the store is not under the runs


# -- transports ------------------------------------------------------------------------


def test_http_and_mcp_expose_the_tools_but_no_new_authority(fake: FakeHubSpot,
                                                            svc: CaseService) -> None:
    client = TestClient(create_app(svc))
    names = {t["name"]: t["effect"] for t in client.get("/api/tools").json()}
    assert names["hubspot_add_ticket_note"] == "approval_required"
    assert names["hubspot_get_ticket"] == "read"
    cid = client.post("/api/cases", json={"scenario": "HUBSPOT", "arm": None,
                                          "hubspot_ticket": TICKET}).json()["case_id"]

    async def go() -> tuple[list[str], Any]:
        async with Client(build_server(svc, cid)) as c:
            listed = [t.name for t in (await c.list_tools()).tools]
            res = await c.call_tool("hubspot_add_ticket_note", NOTE)
            return listed, res
    listed, res = asyncio.run(go())
    assert "hubspot_add_ticket_note" in listed
    assert not [n for n in listed if any(w in n for w in ("approv", "recheck", "connect"))]
    assert "pending operator decision" in res.content[0].text and fake.journal == []
    assert client.post(f"/api/cases/{cid}/actions/act_001/recheck",
                       json={"operator": "op"}).status_code == 404
