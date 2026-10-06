"""Shared synthetic company used by most scenarios. Every name here is fictional."""

from __future__ import annotations

import copy
from typing import Any

from triagewright.model import CaseView

ACCOUNT = "acc_halvard"
OTHER = "acc_brightmoor"
CONTACT = "ines.varga@halvard.example"

_BASE: dict[str, list[dict[str, Any]]] = {
    "accounts": [
        {"id": ACCOUNT, "name": "Halvard Logistics", "plan": "enterprise", "status": "active",
         "region": "eu-north", "owner": "am_ostrova"},
        {"id": OTHER, "name": "Brightmoor Analytics", "plan": "team", "status": "active",
         "region": "us-east", "owner": "am_kade"},
    ],
    "contacts": [
        {"id": "con_ines", "account_id": ACCOUNT, "name": "Ines Varga", "email": CONTACT,
         "role": "admin"},
        {"id": "con_ravi", "account_id": OTHER, "name": "Ravi Shah",
         "email": "ravi@brightmoor.example", "role": "admin"},
    ],
    "plans": [
        {"code": "team", "features": ["audit_log"]},
        {"code": "enterprise", "features": ["audit_log", "scim", "sso"]},
    ],
    "subscriptions": [
        {"id": "sub_h2", "account_id": ACCOUNT, "plan": "enterprise", "status": "active",
         "changed_at": "2026-09-30T16:20:00"},
        {"id": "sub_b1", "account_id": OTHER, "plan": "team", "status": "active",
         "changed_at": "2026-01-10T09:00:00"},
    ],
    "entitlements": [
        {"account_id": ACCOUNT, "feature": f, "enabled": 1, "source_plan": "enterprise",
         "synced_at": "2026-09-30T16:25:00"} for f in ("audit_log", "scim", "sso")
    ],
    "invoices": [
        {"id": "inv_1001", "account_id": ACCOUNT, "amount_cents": 480000, "currency": "EUR",
         "status": "paid", "period": "2026-10", "issued_at": "2026-10-01T00:00:00"},
        {"id": "inv_b77", "account_id": OTHER, "amount_cents": 39000, "currency": "USD",
         "status": "paid", "period": "2026-10", "issued_at": "2026-10-01T00:00:00"},
    ],
    "payment_events": [
        {"id": "pe_1001b", "account_id": ACCOUNT, "invoice_id": "inv_1001", "kind": "capture",
         "amount_cents": 480000, "currency": "EUR", "processor_ref": "ch_7Ka2",
         "created_at": "2026-10-01T00:01:30", "expires_at": None},
        {"id": "pe_b77", "account_id": OTHER, "invoice_id": "inv_b77", "kind": "capture",
         "amount_cents": 39000, "currency": "USD", "processor_ref": "ch_Bm01",
         "created_at": "2026-10-01T00:02:00", "expires_at": None},
    ],
    "services": [
        {"id": "svc_eun_12", "name": "cell-eu-north-12", "tenancy": "dedicated",
         "health": "healthy"},
        {"id": "svc_shared_eu", "name": "shared-eu-3", "tenancy": "shared",
         "health": "healthy"},
    ],
    "workspaces": [
        {"id": "ws_halvard_prod", "account_id": ACCOUNT, "name": "halvard-prod",
         "status": "active", "tenancy": "dedicated", "service_id": "svc_eun_12"},
        {"id": "ws_brightmoor", "account_id": OTHER, "name": "brightmoor-main",
         "status": "active", "tenancy": "shared", "service_id": "svc_shared_eu"},
    ],
    "tickets": [
        {"id": "tkt_b31", "account_id": OTHER, "contact_id": "con_ravi",
         "subject": "Invoice question", "body": "Our October invoice looks high.",
         "status": "open", "incident_id": None, "created_at": "2026-10-05T10:00:00"},
    ],
}


def company() -> dict[str, list[dict[str, Any]]]:
    return copy.deepcopy(_BASE)


def ticket(tid: str, subject: str, body: str, created: str = "2026-10-06T07:40:00",
           contact: str = "con_ines") -> dict[str, Any]:
    return {"id": tid, "account_id": ACCOUNT, "contact_id": contact, "subject": subject,
            "body": body, "status": "open", "incident_id": None, "created_at": created}


def replace_rows(fx: dict[str, list[dict[str, Any]]], table: str,
                 new: list[dict[str, Any]], **match: Any) -> None:
    """Drop rows matching `match` from `table` and append `new`."""
    fx[table] = [r for r in fx.get(table, [])
                 if not all(r.get(k) == v for k, v in match.items())] + new


def obs(v: CaseView, tool: str) -> str:
    o = v.state.latest(tool)
    assert o is not None, f"script expected an observation of {tool}"
    return o.id
