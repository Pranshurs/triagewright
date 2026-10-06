"""End-to-end operational scenarios: a company's state, a case, faults, and scripts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from triagewright.env.faults import FaultRule
from triagewright.model import Step
from triagewright.state import Case


@dataclass(frozen=True)
class OperatorRule:
    """How the simulated operator answers an approval request for `tool`."""
    tool: str
    approve: bool
    note: str | None = None


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    now: str
    fixture: dict[str, list[dict[str, Any]]]
    case: Case
    faults: Sequence[FaultRule] = ()
    operator: Sequence[OperatorRule] = ()
    scripts: dict[str, Callable[[], Sequence[Step]]] = field(default_factory=dict)


def registry() -> dict[str, Scenario]:
    from triagewright.scenarios import s01_duplicate_charge

    out = {}
    for mod in (s01_duplicate_charge,):
        out[mod.SCENARIO.id] = mod.SCENARIO
    return out
