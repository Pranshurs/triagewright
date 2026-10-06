"""The default tool catalogue over the simulated business systems.

Each tool is a thin adapter: swap the handler for a real API client and the rest
of the agent (policy, approvals, tracing, evals) is unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from triagewright import runbooks
from triagewright.env.store import Environment
from triagewright.tools.base import Effect, Registry, Tool, ToolError


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AccountIn(_In):
    account_id: str


class TicketIn(_In):
    ticket_id: str


class WorkspaceIn(_In):
    workspace_id: str


class EndpointIn(_In):
    endpoint_id: str


class DeliveryIn(_In):
    delivery_id: str


class PlanIn(_In):
    plan: str


class IncidentIn(_In):
    incident_id: str


class ListIncidentsIn(_In):
    status: Literal["open", "all"] = "open"


class FindContactIn(_In):
    email: str


class RunbookSearchIn(_In):
    query: str = Field(min_length=2)


class RunbookReadIn(_In):
    runbook_id: str


class NoteIn(_In):
    ticket_id: str
    body: str = Field(min_length=1, max_length=4000)


class TicketStatusIn(_In):
    ticket_id: str
    status: Literal["open", "pending", "solved"]


class LinkIncidentIn(_In):
    ticket_id: str
    incident_id: str


class EscalateIn(_In):
    ticket_id: str
    team: Literal["billing", "engineering", "security", "account_management"]
    summary: str = Field(min_length=1, max_length=4000)


class RefundIn(_In):
    payment_event_id: str
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class CreditIn(_In):
    account_id: str
    amount_cents: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class ExportIn(_In):
    account_id: str


class GrantAdminIn(_In):
    email: str
    account_id: str


# -- scope resolvers -------------------------------------------------------------


def _need(row: dict[str, Any] | None, what: str) -> dict[str, Any]:
    if row is None:
        raise ToolError("NOT_FOUND", f"{what} not found")
    return row


def _account(env: Environment, a: Any) -> str:
    return str(_need(env.one("SELECT id FROM accounts WHERE id=?", [a.account_id]),
                     "account")["id"])


def _ticket(env: Environment, a: Any) -> str:
    return str(_need(env.one("SELECT account_id FROM tickets WHERE id=?", [a.ticket_id]),
                     "ticket")["account_id"])


def _workspace(env: Environment, a: Any) -> str:
    row = env.one("SELECT account_id FROM workspaces WHERE id=?", [a.workspace_id])
    return str(_need(row, "workspace")["account_id"])


def _endpoint(env: Environment, a: Any) -> str:
    row = env.one("SELECT account_id FROM webhook_endpoints WHERE id=?", [a.endpoint_id])
    return str(_need(row, "webhook endpoint")["account_id"])


def _delivery(env: Environment, a: Any) -> str:
    row = env.one(
        "SELECT e.account_id FROM webhook_deliveries d JOIN webhook_endpoints e "
        "ON e.id = d.endpoint_id WHERE d.id=?",
        [a.delivery_id],
    )
    return str(_need(row, "webhook delivery")["account_id"])


def _payment(env: Environment, a: Any) -> str:
    row = env.one("SELECT account_id FROM payment_events WHERE id=?", [a.payment_event_id])
    return str(_need(row, "payment event")["account_id"])


def _contact(env: Environment, a: Any) -> str:
    row = env.one("SELECT account_id FROM contacts WHERE email=?", [a.email])
    return str(_need(row, "contact")["account_id"])


def _global(env: Environment, a: Any) -> None:
    return None


# -- handlers: reads ---------------------------------------------------------------


def get_account(env: Environment, a: AccountIn) -> dict[str, Any]:
    acct = _need(env.one("SELECT * FROM accounts WHERE id=?", [a.account_id]), "account")
    sub = env.one("SELECT * FROM subscriptions WHERE account_id=? ORDER BY changed_at DESC",
                  [a.account_id])
    contacts = env.query("SELECT id, name, email, role FROM contacts WHERE account_id=?",
                         [a.account_id])
    return {"account": acct, "subscription": sub, "contacts": contacts}


def find_contact(env: Environment, a: FindContactIn) -> dict[str, Any]:
    c = _need(env.one("SELECT * FROM contacts WHERE email=?", [a.email]), "contact")
    return {"contact": c}


def get_plan(env: Environment, a: PlanIn) -> dict[str, Any]:
    return {"plan": _need(env.one("SELECT * FROM plans WHERE code=?", [a.plan]), "plan")}


def get_entitlements(env: Environment, a: AccountIn) -> dict[str, Any]:
    return {"entitlements": env.query(
        "SELECT feature, enabled, source_plan, synced_at FROM entitlements "
        "WHERE account_id=? ORDER BY feature", [a.account_id])}


def list_invoices(env: Environment, a: AccountIn) -> dict[str, Any]:
    return {"invoices": env.query(
        "SELECT * FROM invoices WHERE account_id=? ORDER BY issued_at", [a.account_id])}


def list_payment_events(env: Environment, a: AccountIn) -> dict[str, Any]:
    return {"payment_events": env.query(
        "SELECT * FROM payment_events WHERE account_id=? ORDER BY created_at, id",
        [a.account_id])}


def list_workspaces(env: Environment, a: AccountIn) -> dict[str, Any]:
    return {"workspaces": env.query(
        "SELECT * FROM workspaces WHERE account_id=? ORDER BY id", [a.account_id])}


def get_provisioning_jobs(env: Environment, a: WorkspaceIn) -> dict[str, Any]:
    return {"jobs": env.query(
        "SELECT * FROM provisioning_jobs WHERE workspace_id=? ORDER BY updated_at, id",
        [a.workspace_id])}


def get_workspace_health(env: Environment, a: WorkspaceIn) -> dict[str, Any]:
    ws = _need(env.one("SELECT * FROM workspaces WHERE id=?", [a.workspace_id]), "workspace")
    svc = env.one("SELECT * FROM services WHERE id=?", [ws["service_id"]])
    return {"workspace": ws, "service": svc}


def list_incidents(env: Environment, a: ListIncidentsIn) -> dict[str, Any]:
    sql = "SELECT * FROM incidents"
    if a.status == "open":
        sql += " WHERE status != 'resolved'"
    return {"incidents": env.query(sql + " ORDER BY started_at")}


def get_incident(env: Environment, a: IncidentIn) -> dict[str, Any]:
    # Global tool: it must not return account-owned data. Which tickets are linked to
    # an incident spans customers, so it is not exposed here; a case sees its own link
    # on its ticket.
    inc = _need(env.one("SELECT * FROM incidents WHERE id=?", [a.incident_id]), "incident")
    return {"incident": inc}


def get_ticket(env: Environment, a: TicketIn) -> dict[str, Any]:
    t = _need(env.one("SELECT * FROM tickets WHERE id=?", [a.ticket_id]), "ticket")
    notes = env.query(
        "SELECT * FROM ticket_notes WHERE ticket_id=? ORDER BY created_at, id", [a.ticket_id])
    return {"ticket": t, "notes": notes}


def list_tickets(env: Environment, a: AccountIn) -> dict[str, Any]:
    return {"tickets": env.query(
        "SELECT id, subject, status, incident_id, created_at FROM tickets "
        "WHERE account_id=? ORDER BY created_at", [a.account_id])}


def list_webhook_endpoints(env: Environment, a: AccountIn) -> dict[str, Any]:
    return {"endpoints": env.query(
        "SELECT * FROM webhook_endpoints WHERE account_id=? ORDER BY id", [a.account_id])}


def list_webhook_deliveries(env: Environment, a: EndpointIn) -> dict[str, Any]:
    return {"deliveries": env.query(
        "SELECT * FROM webhook_deliveries WHERE endpoint_id=? ORDER BY created_at, id",
        [a.endpoint_id])}


def search_runbooks(env: Environment, a: RunbookSearchIn) -> dict[str, Any]:
    return {"results": runbooks.search(a.query)}


def read_runbook(env: Environment, a: RunbookReadIn) -> dict[str, Any]:
    rb = runbooks.get(a.runbook_id)
    if rb is None:
        raise ToolError("NOT_FOUND", "runbook not found")
    return {"runbook": rb}


# -- handlers: writes --------------------------------------------------------------


def add_internal_note(env: Environment, a: NoteIn) -> dict[str, Any]:
    nid = env.new_id("note")
    env.insert("ticket_notes", {"id": nid, "ticket_id": a.ticket_id, "author": "ops-agent",
                                "body": a.body, "public": 0, "created_at": env.now()})
    return {"note_id": nid}


def send_customer_reply(env: Environment, a: NoteIn) -> dict[str, Any]:
    nid = env.new_id("reply")
    env.insert("ticket_notes", {"id": nid, "ticket_id": a.ticket_id, "author": "ops-agent",
                                "body": a.body, "public": 1, "created_at": env.now()})
    return {"reply_id": nid}


def set_ticket_status(env: Environment, a: TicketStatusIn) -> dict[str, Any]:
    env.execute("UPDATE tickets SET status=? WHERE id=?", [a.status, a.ticket_id])
    return {"ticket_id": a.ticket_id, "status": a.status}


def link_ticket_to_incident(env: Environment, a: LinkIncidentIn) -> dict[str, Any]:
    _need(env.one("SELECT id FROM incidents WHERE id=?", [a.incident_id]), "incident")
    env.execute("UPDATE tickets SET incident_id=? WHERE id=?", [a.incident_id, a.ticket_id])
    return {"ticket_id": a.ticket_id, "incident_id": a.incident_id}


def escalate_case(env: Environment, a: EscalateIn) -> dict[str, Any]:
    eid = env.new_id("esc")
    env.insert("escalations", {"id": eid, "ticket_id": a.ticket_id, "team": a.team,
                               "summary": a.summary, "created_at": env.now()})
    return {"escalation_id": eid, "team": a.team}


def resync_entitlements(env: Environment, a: AccountIn) -> dict[str, Any]:
    sub = _need(env.one("SELECT plan FROM subscriptions WHERE account_id=? AND status='active' "
                        "ORDER BY changed_at DESC", [a.account_id]), "active subscription")
    plan = _need(env.one("SELECT features FROM plans WHERE code=?", [sub["plan"]]), "plan")
    features: list[str] = plan["features"]
    env.execute("DELETE FROM entitlements WHERE account_id=?", [a.account_id])
    for f in features:
        env.insert("entitlements", {"account_id": a.account_id, "feature": f, "enabled": 1,
                                    "source_plan": sub["plan"], "synced_at": env.now()})
    return {"account_id": a.account_id, "plan": sub["plan"], "features": features}


REQUIRED_FEATURE_PREFIX = "requires:"


def retry_provisioning(env: Environment, a: WorkspaceIn) -> dict[str, Any]:
    ws = _need(env.one("SELECT * FROM workspaces WHERE id=?", [a.workspace_id]), "workspace")
    job = _need(env.one("SELECT * FROM provisioning_jobs WHERE workspace_id=? "
                        "ORDER BY updated_at DESC, id DESC", [a.workspace_id]), "job")
    if job["status"] != "failed":
        raise ToolError("PRECONDITION", f"latest job is {job['status']}, not failed")
    # A provisioning job fails while the account lacks an entitlement it needs; the
    # failed job's error names the feature ("requires:<feature>").
    missing = None
    err = job["error"] or ""
    if err.startswith(REQUIRED_FEATURE_PREFIX):
        feature = err[len(REQUIRED_FEATURE_PREFIX):].split()[0]
        ok = env.one("SELECT enabled FROM entitlements WHERE account_id=? AND feature=?",
                     [ws["account_id"], feature])
        if not ok or not ok["enabled"]:
            missing = feature
    status = "failed" if missing else "succeeded"
    env.execute(
        "UPDATE provisioning_jobs SET status=?, error=?, attempts=attempts+1, updated_at=? "
        "WHERE id=?",
        [status, f"{REQUIRED_FEATURE_PREFIX}{missing}" if missing else None, env.now(), job["id"]],
    )
    env.execute("UPDATE workspaces SET status=? WHERE id=?",
                ["failed" if missing else "active", a.workspace_id])
    return {"job_id": job["id"], "status": status, "attempts": job["attempts"] + 1,
            "error": f"{REQUIRED_FEATURE_PREFIX}{missing}" if missing else None}


def restart_service(env: Environment, a: WorkspaceIn) -> dict[str, Any]:
    ws = _need(env.one("SELECT * FROM workspaces WHERE id=?", [a.workspace_id]), "workspace")
    env.execute("UPDATE services SET health='healthy', restarts=restarts+1 WHERE id=?",
                [ws["service_id"]])
    env.execute("UPDATE workspaces SET status='active' WHERE service_id=? AND status='degraded'",
                [ws["service_id"]])
    return {"service_id": ws["service_id"], "health": "healthy"}


def redeliver_webhook(env: Environment, a: DeliveryIn) -> dict[str, Any]:
    d = _need(env.one("SELECT * FROM webhook_deliveries WHERE id=?", [a.delivery_id]),
              "delivery")
    ep = _need(env.one("SELECT * FROM webhook_endpoints WHERE id=?", [d["endpoint_id"]]),
               "endpoint")
    # The simulated receiver: a 5xx outage has since recovered; a 4xx stays a 4xx.
    code = 200 if d["status_code"] >= 500 or d["status_code"] == 0 else d["status_code"]
    did = env.new_id("dlv")
    env.insert("webhook_deliveries", {"id": did, "endpoint_id": ep["id"], "event": d["event"],
                                      "status_code": code, "attempt": d["attempt"] + 1,
                                      "created_at": env.now()})
    failures = 0 if code < 300 else ep["consecutive_failures"] + 1
    env.execute("UPDATE webhook_endpoints SET consecutive_failures=? WHERE id=?",
                [failures, ep["id"]])
    return {"delivery_id": did, "status_code": code}


def disable_webhook_endpoint(env: Environment, a: EndpointIn) -> dict[str, Any]:
    env.execute("UPDATE webhook_endpoints SET status='disabled' WHERE id=?", [a.endpoint_id])
    return {"endpoint_id": a.endpoint_id, "status": "disabled"}


def issue_refund(env: Environment, a: RefundIn) -> dict[str, Any]:
    pe = _need(env.one("SELECT * FROM payment_events WHERE id=?", [a.payment_event_id]),
               "payment event")
    if pe["kind"] != "capture":
        raise ToolError("PRECONDITION", f"can only refund a capture, not {pe['kind']}")
    refunded = env.one(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM payment_events "
        "WHERE kind='refund' AND processor_ref=?", [pe["processor_ref"]])
    already = int(refunded["s"]) if refunded else 0
    if already + a.amount_cents > pe["amount_cents"]:
        raise ToolError("PRECONDITION", "refund exceeds captured amount")
    rid = env.new_id("pe")
    env.insert("payment_events", {
        "id": rid, "account_id": pe["account_id"], "invoice_id": pe["invoice_id"],
        "kind": "refund", "amount_cents": a.amount_cents, "currency": pe["currency"],
        "processor_ref": pe["processor_ref"], "created_at": env.now(), "expires_at": None})
    return {"refund_event_id": rid, "amount_cents": a.amount_cents, "currency": pe["currency"]}


def apply_account_credit(env: Environment, a: CreditIn) -> dict[str, Any]:
    iid = env.new_id("inv")
    env.insert("invoices", {"id": iid, "account_id": a.account_id, "amount_cents": -a.amount_cents,
                            "currency": "USD", "status": "paid", "period": "credit",
                            "issued_at": env.now()})
    return {"credit_invoice_id": iid, "amount_cents": a.amount_cents}


# -- registry -------------------------------------------------------------------------


def default_registry() -> Registry:
    r = Registry()
    R, S, A, D = Effect.READ, Effect.SAFE_WRITE, Effect.APPROVAL_REQUIRED, Effect.DENIED

    def t(name: str, desc: str, domain: str, eff: Effect, model: type[BaseModel],
          handler: Any, scope: Any) -> None:
        r.add(Tool(name, desc, domain, eff, model, handler, scope))

    t("get_account", "Account record, current subscription and contacts.", "crm", R,
      AccountIn, get_account, _account)
    t("find_contact", "Look up a contact (and their account) by email.", "crm", R,
      FindContactIn, find_contact, _contact)
    t("get_plan", "Features included in a plan.", "billing", R, PlanIn, get_plan, _global)
    t("get_entitlements", "Feature entitlements currently synced to the account.",
      "entitlements", R, AccountIn, get_entitlements, _account)
    t("list_invoices", "Invoices for an account.", "billing", R, AccountIn, list_invoices,
      _account)
    t("list_payment_events", "Payment processor events (authorizations, captures, refunds).",
      "billing", R, AccountIn, list_payment_events, _account)
    t("list_workspaces", "Workspaces belonging to an account.", "provisioning", R, AccountIn,
      list_workspaces, _account)
    t("get_provisioning_jobs", "Provisioning job history for a workspace.", "provisioning", R,
      WorkspaceIn, get_provisioning_jobs, _workspace)
    t("get_workspace_health", "Workspace status and the health of the service it runs on.",
      "operations", R, WorkspaceIn, get_workspace_health, _workspace)
    t("list_incidents", "Platform incidents (open by default).", "operations", R,
      ListIncidentsIn, list_incidents, _global)
    t("get_incident", "One platform incident.", "operations", R,
      IncidentIn, get_incident, _global)
    t("get_ticket", "A support ticket with its notes and replies.", "support", R, TicketIn,
      get_ticket, _ticket)
    t("list_tickets", "Support history for an account.", "support", R, AccountIn, list_tickets,
      _account)
    t("list_webhook_endpoints", "Webhook endpoints configured by an account.", "integrations",
      R, AccountIn, list_webhook_endpoints, _account)
    t("list_webhook_deliveries", "Delivery attempts for a webhook endpoint.", "integrations", R,
      EndpointIn, list_webhook_deliveries, _endpoint)
    t("search_runbooks", "Keyword search over internal runbooks.", "knowledge", R,
      RunbookSearchIn, search_runbooks, _global)
    t("read_runbook", "Full text of one runbook.", "knowledge", R, RunbookReadIn, read_runbook,
      _global)

    t("add_internal_note", "Add an internal (not customer-visible) note to a ticket.",
      "support", S, NoteIn, add_internal_note, _ticket)
    t("send_customer_reply", "Send a reply to the customer on a ticket.", "support", S, NoteIn,
      send_customer_reply, _ticket)
    t("set_ticket_status", "Set a ticket's status.", "support", S, TicketStatusIn,
      set_ticket_status, _ticket)
    t("link_ticket_to_incident", "Attach a ticket to an existing incident.", "operations", S,
      LinkIncidentIn, link_ticket_to_incident, _ticket)
    t("escalate_case", "Hand the case to a human team with a summary.", "support", S,
      EscalateIn, escalate_case, _ticket)
    t("resync_entitlements", "Re-derive entitlements from the active subscription's plan.",
      "entitlements", S, AccountIn, resync_entitlements, _account)
    t("retry_provisioning", "Re-run the latest failed provisioning job for a workspace.",
      "provisioning", S, WorkspaceIn, retry_provisioning, _workspace)
    t("restart_service", "Restart the service backing a workspace.", "operations", S,
      WorkspaceIn, restart_service, _workspace)
    t("redeliver_webhook", "Re-send one failed webhook delivery.", "integrations", S,
      DeliveryIn, redeliver_webhook, _delivery)

    t("issue_refund", "Refund (part of) a captured payment.", "billing", A, RefundIn,
      issue_refund, _payment)
    t("apply_account_credit", "Apply a service credit to an account.", "billing", A, CreditIn,
      apply_account_credit, _account)
    t("disable_webhook_endpoint", "Stop deliveries to a webhook endpoint.", "integrations", A,
      EndpointIn, disable_webhook_endpoint, _endpoint)

    t("export_account_contacts", "Bulk export of an account's contact data.", "crm", D,
      ExportIn, None, _account)
    t("grant_admin_access", "Make a user an admin of an account.", "crm", D, GrantAdminIn,
      None, _account)
    return r
