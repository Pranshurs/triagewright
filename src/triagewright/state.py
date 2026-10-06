"""Case state. Everything here is written by the runner, never by the model."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from triagewright.tools.base import canonical_args


class Case(BaseModel):
    id: str
    account_id: str          # resolved at intake from the ticket; the model cannot change it
    ticket_id: str
    contact_email: str
    request: str


class CaseStatus(StrEnum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    NEEDS_INFO = "needs_info"
    NEEDS_ATTENTION = "needs_attention"  # finished with an effect whose outcome is unknown
    FAILED = "failed"                    # budget exhausted or unrecoverable error

    @property
    def terminal(self) -> bool:
        return self not in (CaseStatus.OPEN, CaseStatus.INVESTIGATING,
                            CaseStatus.AWAITING_APPROVAL)


class Observation(BaseModel):
    id: str
    step: int
    tool: str
    args: dict[str, Any]
    ok: bool
    result: dict[str, Any]   # tool payload (data or error), as the runner received it
    attempts: int = 1


class ActionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"        # upstream definitively rejected it; no effect
    UNKNOWN = "unknown"      # may or may not have taken effect


class ActionRecord(BaseModel):
    id: str
    tool: str
    args: dict[str, Any]
    idempotency_key: str
    status: ActionStatus
    approval_id: str | None = None
    observation_id: str | None = None
    replayed: bool = False         # result came from the upstream idempotency store
    reconcile_attempts: int = 0
    detail: str | None = None


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


def binding(case_id: str, account_id: str, tool: str, args: dict[str, Any]) -> str:
    """What an approval authorises: this operation, these arguments, this account, this case."""
    material = "\n".join([case_id, account_id, tool, canonical_args(args)])
    return hashlib.sha256(material.encode()).hexdigest()


class Approval(BaseModel):
    id: str
    case_id: str
    account_id: str
    tool: str
    args: dict[str, Any]
    binding: str
    rationale: str
    evidence: list[str]
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_step: int
    decided_by: str | None = None
    note: str | None = None
    action_id: str | None = None


class Finding(BaseModel):
    claim: str
    evidence: list[str] = Field(default_factory=list)  # observation ids
    values: list[str] = Field(default_factory=list)    # ids/amounts the claim relies on
    grounded: bool | None = None                       # set by the runner


class Resolution(BaseModel):
    outcome: str            # "resolved" | "escalated" | "needs_info" (a request, not a fact)
    diagnosis: str          # short label, e.g. "duplicate_capture+stale_entitlement"
    summary: str
    findings: list[Finding] = Field(default_factory=list)
    uncertainty: str | None = None


class CaseState(BaseModel):
    case: Case
    status: CaseStatus = CaseStatus.OPEN
    step: int = 0
    tool_calls: int = 0
    observations: list[Observation] = Field(default_factory=list)
    actions: list[ActionRecord] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    resolution: Resolution | None = None
    finish_rejections: int = 0
    status_reason: str | None = None

    def obs(self, obs_id: str) -> Observation | None:
        return next((o for o in self.observations if o.id == obs_id), None)

    def latest(self, tool: str) -> Observation | None:
        return next((o for o in reversed(self.observations) if o.tool == tool), None)

    def pending_approvals(self) -> list[Approval]:
        return [a for a in self.approvals if a.status is ApprovalStatus.PENDING]

    def unknown_actions(self) -> list[ActionRecord]:
        return [a for a in self.actions if a.status is ActionStatus.UNKNOWN]

    def render(self, budget_chars: int = 12000) -> str:
        """Compact model context: newest observations in full, older ones as one-liners."""
        head = {
            "case": self.case.model_dump(),
            "status": self.status.value,
            "actions": [
                {"id": a.id, "tool": a.tool, "args": a.args, "status": a.status.value,
                 "replayed": a.replayed} for a in self.actions
            ],
            "approvals": [
                {"id": a.id, "tool": a.tool, "args": a.args, "status": a.status.value,
                 "note": a.note} for a in self.approvals
            ],
        }
        parts = [json.dumps(head, default=str)]
        used = len(parts[0])
        full: list[str] = []
        brief: list[str] = []
        for o in reversed(self.observations):
            body = json.dumps({"id": o.id, "tool": o.tool, "args": o.args, "ok": o.ok,
                               "result": o.result}, default=str)
            if used + len(body) <= budget_chars:
                full.append(body)
                used += len(body)
            else:
                brief.append(f"{o.id} {o.tool}({canonical_args(o.args)}) ok={o.ok} [elided]")
        parts.extend(reversed(brief))
        parts.extend(reversed(full))
        return "\n".join(parts)
