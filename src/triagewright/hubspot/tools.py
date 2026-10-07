"""HubSpot tools for the catalogue: three reads and one write.

They are ordinary catalogue entries. The policy resolves their tenant scope and
classifies them exactly as it does the simulated tools; the write needs an operator's
approval and goes through the runner's dispatch and reconciliation like any other.

Tenant scope: a HubSpot record belongs to the account its company is linked to
(`HubSpotAuth.link`, operator configuration). A record whose companies are not all
linked to one account has no scope and is denied.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from triagewright.env.store import Environment
from triagewright.hubspot.client import HubSpotClient
from triagewright.tools.base import Effect, External, Registry, Tool, ToolError

HubSpotId = Field(pattern=r"^[0-9]{1,20}$")

TICKET = ("subject", "content", "hs_pipeline_stage", "hs_ticket_priority", "createdate")
CONTACT = ("firstname", "lastname", "email")
COMPANY = ("name", "domain")


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HsTicketIn(_In):
    ticket_id: str = HubSpotId


class HsContactIn(_In):
    contact_id: str = HubSpotId


class HsCompanyIn(_In):
    company_id: str = HubSpotId


class HsNoteIn(_In):
    ticket_id: str = HubSpotId
    body: str = Field(min_length=1, max_length=4000)


def register(registry: Registry, client: HubSpotClient) -> Registry:
    auth = client.auth

    def account_of(companies: list[str]) -> str:
        accounts = {auth.account_for(c) for c in companies}
        if len(accounts) != 1 or None in accounts:
            raise ToolError("NOT_FOUND", "record is not linked to exactly one account")
        return str(accounts.pop())

    def companies_of(object_type: str, object_id: str) -> list[str]:
        ids, complete = client.associated(object_type, object_id, "companies")
        if not complete:
            raise ToolError("NOT_FOUND", "record has too many companies to resolve")
        return ids

    def ticket_scope(_env: Environment, a: Any) -> str:
        return account_of(companies_of("tickets", a.ticket_id))

    def contact_scope(_env: Environment, a: Any) -> str:
        return account_of(companies_of("contacts", a.contact_id))

    def company_scope(_env: Environment, a: Any) -> str:
        return account_of([a.company_id])

    def get_ticket(_env: Environment, a: HsTicketIn) -> dict[str, Any]:
        contacts, _ = client.associated("tickets", a.ticket_id, "contacts")
        return {"ticket": client.get("tickets", a.ticket_id, TICKET),
                "contact_ids": contacts,
                "company_ids": companies_of("tickets", a.ticket_id)}

    def get_contact(_env: Environment, a: HsContactIn) -> dict[str, Any]:
        return {"contact": client.get("contacts", a.contact_id, CONTACT),
                "company_ids": companies_of("contacts", a.contact_id)}

    def get_company(_env: Environment, a: HsCompanyIn) -> dict[str, Any]:
        return {"company": client.get("companies", a.company_id, COMPANY)}

    def write_note(a: HsNoteIn, key: str) -> dict[str, Any]:
        return client.create_note(a.ticket_id, a.body, auth.marker(key))

    def find_note(a: HsNoteIn, key: str) -> dict[str, Any]:
        return client.find_note(a.ticket_id, auth.marker(key))

    R = Effect.READ
    registry.add(Tool("hubspot_get_ticket", "A HubSpot ticket and the ids of its contacts "
                      "and companies.", "hubspot", R, HsTicketIn, get_ticket, ticket_scope))
    registry.add(Tool("hubspot_get_contact", "A HubSpot contact and the ids of its companies.",
                      "hubspot", R, HsContactIn, get_contact, contact_scope))
    registry.add(Tool("hubspot_get_company", "A HubSpot company.", "hubspot", R, HsCompanyIn,
                      get_company, company_scope))
    registry.add(Tool("hubspot_add_ticket_note", "Add a note to a HubSpot ticket.", "hubspot",
                      Effect.APPROVAL_REQUIRED, HsNoteIn, None, ticket_scope,
                      external=External(write=write_note, lookup=find_note)))
    return registry


def connector_from_env() -> HubSpotClient | None:
    """The connector, if TRIAGEWRIGHT_HUBSPOT_* is configured."""
    from triagewright.hubspot.oauth import HubSpotAuth, Settings

    settings = Settings.from_env()
    return HubSpotClient(HubSpotAuth(settings)) if settings else None
