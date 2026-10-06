"""Who may do what. Evaluated by the runner against live system state.

The model's view of the world (what it observed, what it claims) is never an input
here: predicates read the environment directly, and scope comes from the case.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from triagewright.env.store import Environment
from triagewright.state import Case
from triagewright.tools.base import Effect, Tool, ToolError


class Verdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str
    reason: str


Predicate = Callable[[Environment, Case, Any], Decision]

MAX_PROVISIONING_ATTEMPTS = 3
MAX_RESTARTS = 2


def _allow(rule: str, reason: str = "") -> Decision:
    return Decision(Verdict.ALLOW, rule, reason)


def _deny(rule: str, reason: str) -> Decision:
    return Decision(Verdict.DENY, rule, reason)


def _retry_provisioning(env: Environment, case: Case, a: Any) -> Decision:
    job = env.one("SELECT status, attempts FROM provisioning_jobs WHERE workspace_id=? "
                  "ORDER BY updated_at DESC, id DESC", [a.workspace_id])
    if job is None or job["status"] != "failed":
        return _deny("provisioning.only_failed", "latest provisioning job is not failed")
    if job["attempts"] >= MAX_PROVISIONING_ATTEMPTS:
        return _deny("provisioning.attempt_limit",
                     f"{job['attempts']} attempts already; escalate to engineering")
    return _allow("provisioning.retry", f"attempt {job['attempts'] + 1}")


def _restart_service(env: Environment, case: Case, a: Any) -> Decision:
    svc = env.one("SELECT s.* FROM services s JOIN workspaces w ON w.service_id = s.id "
                  "WHERE w.id=?", [a.workspace_id])
    if svc is None:
        return _deny("restart.no_service", "no service behind workspace")
    if svc["tenancy"] != "dedicated":
        return _deny("restart.shared_service", "shared service: restart affects other "
                     "customers; escalate to engineering")
    for inc in env.query("SELECT id, service_ids FROM incidents WHERE status != 'resolved'"):
        if svc["id"] in inc["service_ids"]:
            return _deny("restart.open_incident", f"service is covered by open incident "
                         f"{inc['id']}; link the ticket instead")
    if svc["restarts"] >= MAX_RESTARTS:
        return _deny("restart.limit", "restart limit reached; escalate to engineering")
    return _allow("restart.dedicated_no_incident")


def _redeliver_webhook(env: Environment, case: Case, a: Any) -> Decision:
    d = env.one("SELECT * FROM webhook_deliveries WHERE id=?", [a.delivery_id])
    if d is None:
        return _deny("webhook.unknown_delivery", "delivery not found")
    if 0 < d["status_code"] < 500:
        return _deny("webhook.client_error",
                     f"receiver answered {d['status_code']}; redelivery will not help")
    later = env.one("SELECT id FROM webhook_deliveries WHERE endpoint_id=? AND event=? "
                    "AND attempt > ?", [d["endpoint_id"], d["event"], d["attempt"]])
    if later is not None:
        return _deny("webhook.already_redelivered", f"already redelivered as {later['id']}")
    return _allow("webhook.redeliver_transient")


SAFE_WRITE_RULES: dict[str, Predicate] = {
    "retry_provisioning": _retry_provisioning,
    "restart_service": _restart_service,
    "redeliver_webhook": _redeliver_webhook,
}

# Safe writes with no precondition beyond tenant scope.
UNCONDITIONAL_SAFE = {
    "add_internal_note", "send_customer_reply", "set_ticket_status",
    "link_ticket_to_incident", "escalate_case", "resync_entitlements",
}


def evaluate(env: Environment, case: Case, tool: Tool, args: BaseModel) -> Decision:
    if tool.effect is Effect.DENIED:
        return _deny("catalogue.denied", f"{tool.name} is never available to the agent")
    try:
        scope = tool.scope(env, args)
    except ToolError as e:
        return _deny("scope.unresolved", e.message)
    if scope is not None and scope != case.account_id:
        return _deny("scope.cross_account",
                     f"{tool.name} targets a different account than this case")
    if tool.effect is Effect.READ:
        return _allow("read")
    if tool.effect is Effect.APPROVAL_REQUIRED:
        return Decision(Verdict.NEEDS_APPROVAL, "catalogue.approval_required",
                        f"{tool.name} is consequential")
    rule = SAFE_WRITE_RULES.get(tool.name)
    if rule is not None:
        return rule(env, case, args)
    if tool.name in UNCONDITIONAL_SAFE:
        return _allow("safe_write")
    # A safe write nobody wrote a rule for is not safe.
    return Decision(Verdict.NEEDS_APPROVAL, "policy.no_rule", f"no policy rule for {tool.name}")
