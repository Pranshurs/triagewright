"""A case on a ticket that lives in the connected HubSpot account.

Not part of the scenario suite or its evaluation: there is no simulated ground truth
to score against. It exists to drive the connector through the same runner, either
with the short scripted walkthrough below or by an external agent.
"""

from __future__ import annotations

from triagewright.evals.oracle import Gold
from triagewright.model import CaseView, Finish, Step, UseTool
from triagewright.scenarios import Arm, Scenario
from triagewright.scenarios.base import ACCOUNT, CONTACT, company, obs, ticket
from triagewright.state import Case, Fact, Finding, Resolution

ID = "HUBSPOT"
NOTE = "Triage: ticket reviewed against the company and contact on record."


def _first(v: CaseView, key: str) -> str:
    o = v.state.latest("hubspot_get_ticket")
    assert o is not None and o.ok, "script expected the ticket to be readable"
    ids = o.result["data"][key]
    assert ids, f"the walkthrough needs a ticket with at least one of {key}"
    return str(ids[0])


def _finish(hs_ticket: str, summary: str) -> Step:
    return lambda v: Finish(resolution=Resolution(
        outcome="resolved", diagnosis="hubspot_triage_note", summary=summary,
        findings=[Finding(claim="the ticket exists in the connected HubSpot account",
                          evidence=[o := obs(v, "hubspot_get_ticket")],
                          facts=[Fact(obs=o, record=hs_ticket, field="id",
                                      value=hs_ticket)])]))


def walkthrough(hs_ticket: str) -> list[Step]:
    return [
        UseTool(tool="hubspot_get_ticket", args={"ticket_id": hs_ticket},
                rationale="read the ticket"),
        lambda v: UseTool(tool="hubspot_get_company",
                          args={"company_id": _first(v, "company_ids")},
                          rationale="which customer this is"),
        lambda v: UseTool(tool="hubspot_get_contact",
                          args={"contact_id": _first(v, "contact_ids")},
                          rationale="who raised it"),
        lambda v: UseTool(tool="hubspot_add_ticket_note",
                          args={"ticket_id": hs_ticket, "body": NOTE},
                          evidence=[obs(v, "hubspot_get_ticket")],
                          rationale="record the triage on the ticket"),
        _finish(hs_ticket, "Triage note proposed; waiting for the operator."),
        _finish(hs_ticket, "Ticket reviewed and a triage note recorded in HubSpot."),
    ]


def scenario(hs_ticket: str, account_id: str = ACCOUNT) -> Scenario:
    fx = company()
    fx["tickets"].append(ticket("tkt_hubspot", "Ticket held in HubSpot",
                                f"See HubSpot ticket {hs_ticket}."))
    return Scenario(
        id=ID, title="Ticket in a connected HubSpot account",
        invariant="a real system of record sits behind the same runner",
        now="2026-10-06T08:00:00", fixture=fx,
        case=Case(id="case_hubspot", account_id=account_id, ticket_id="tkt_hubspot",
                  contact_email=CONTACT,
                  request=f"Review HubSpot ticket {hs_ticket} and leave a triage note."),
        gold=Gold(terminal=frozenset({"resolved"})),
        arms={"walkthrough": Arm(steps=lambda: walkthrough(hs_ticket), expect={},
                                 about="read ticket, company and contact; propose one note")})
