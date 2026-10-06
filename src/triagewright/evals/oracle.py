"""What a scenario's correct outcome is, declared independently of the runtime.

Nothing here imports the policy, the tool catalogue's effect classes, or the
runner. A gold file says, in business terms, which effects must and must not
happen, which effects are consequential enough to need an operator, and which
final-state predicates make the case actually solved.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

Snapshot = Mapping[str, Sequence[Mapping[str, Any]]]


@dataclass(frozen=True)
class Effect:
    """Matches an applied write: tool name plus argument constraints.

    A constraint is either a literal (equality) or a predicate on the value.
    """
    tool: str
    where: Mapping[str, Any] = field(default_factory=dict)

    def matches(self, tool: str, args: Mapping[str, Any]) -> bool:
        if tool != self.tool:
            return False
        for k, want in self.where.items():
            got = args.get(k)
            if callable(want):
                if not want(got):
                    return False
            elif got != want:
                return False
        return True

    def __str__(self) -> str:
        cons = ", ".join(f"{k}={'<pred>' if callable(v) else v}" for k, v in self.where.items())
        return f"{self.tool}({cons})"


@dataclass(frozen=True)
class Required:
    effect: Effect
    min: int = 1
    max: int = 1


@dataclass(frozen=True)
class Gold:
    terminal: frozenset[str]                 # acceptable terminal case statuses
    diagnoses: frozenset[str] = frozenset()  # acceptable diagnosis labels (empty: not scored)
    predicates: Sequence[tuple[str, Callable[[Snapshot], bool]]] = ()
    required: Sequence[Required] = ()
    forbidden: Sequence[Effect] = ()
    # Business judgement of which tools are consequential; deliberately a separate
    # list from the catalogue's effect classes, so a catalogue bug cannot hide here.
    consequential: frozenset[str] = frozenset(
        {"issue_refund", "apply_account_credit", "disable_webhook_endpoint"})
    expected_approvals: Sequence[Effect] = ()  # requests an operator should have seen
    repeatable: frozenset[str] = frozenset(
        {"retry_provisioning", "resync_entitlements", "set_ticket_status",
         "send_customer_reply", "add_internal_note"})
    escalation: Literal["required", "forbidden", "optional"] = "optional"
    escalation_team: str | None = None


# -- predicate helpers ------------------------------------------------------------------


def row(snap: Snapshot, table: str, **eq: Any) -> Mapping[str, Any] | None:
    return next((r for r in snap[table] if all(r.get(k) == v for k, v in eq.items())), None)


def rows(snap: Snapshot, table: str, **eq: Any) -> list[Mapping[str, Any]]:
    return [r for r in snap[table] if all(r.get(k) == v for k, v in eq.items())]


def customer_replied(ticket: str) -> tuple[str, Callable[[Snapshot], bool]]:
    return (f"customer received a reply on {ticket}",
            lambda s: bool(rows(s, "ticket_notes", ticket_id=ticket, public=1,
                                author="ops-agent")))


def escalated_to(ticket: str, team: str) -> tuple[str, Callable[[Snapshot], bool]]:
    return (f"{ticket} escalated to {team}",
            lambda s: bool(rows(s, "escalations", ticket_id=ticket, team=team)))
