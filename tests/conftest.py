from __future__ import annotations

from typing import Any

import pytest

from triagewright.env.store import Environment

NOW = "2026-10-06T09:00:00"


def base_fixture() -> dict[str, list[dict[str, Any]]]:
    return {
        "accounts": [
            {"id": "acc_1", "name": "Halvard Logistics", "plan": "enterprise",
             "status": "active", "region": "eu", "owner": "am_1"},
            {"id": "acc_2", "name": "Other Tenant", "plan": "team", "status": "active",
             "region": "us", "owner": "am_2"},
        ],
        "contacts": [{"id": "c1", "account_id": "acc_1", "name": "Ines",
                      "email": "ines@halvard.example", "role": "admin"}],
        "plans": [{"code": "enterprise", "features": ["sso", "audit_log"]}],
        "subscriptions": [{"id": "sub1", "account_id": "acc_1", "plan": "enterprise",
                           "status": "active", "changed_at": "2026-09-30T10:00:00"}],
        "invoices": [{"id": "inv1", "account_id": "acc_1", "amount_cents": 480000,
                      "currency": "USD", "status": "paid", "period": "2026-10",
                      "issued_at": "2026-10-01T00:00:00"}],
        "payment_events": [{"id": "pe1", "account_id": "acc_1", "invoice_id": "inv1",
                            "kind": "capture", "amount_cents": 480000, "currency": "USD",
                            "processor_ref": "ch_a", "created_at": "2026-10-01T00:01:00",
                            "expires_at": None}],
        "services": [{"id": "svc1", "name": "ws-eu-7", "tenancy": "dedicated",
                      "health": "healthy"}],
        "workspaces": [{"id": "ws1", "account_id": "acc_1", "name": "prod", "status": "failed",
                        "tenancy": "dedicated", "service_id": "svc1"}],
        "provisioning_jobs": [{"id": "job1", "workspace_id": "ws1", "status": "failed",
                               "error": "requires:sso", "attempts": 1,
                               "updated_at": "2026-10-01T00:05:00"}],
        "tickets": [{"id": "t1", "account_id": "acc_1", "contact_id": "c1", "subject": "x",
                     "body": "y", "status": "open", "incident_id": None,
                     "created_at": "2026-10-02T00:00:00"}],
    }


@pytest.fixture
def env() -> Environment:
    return Environment(base_fixture(), now=NOW)
