"""Deterministic fault injection for tool calls."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class FaultKind(StrEnum):
    TRANSIENT_ERROR = "transient_error"          # upstream 503; nothing happened
    RATE_LIMITED = "rate_limited"                # 429; nothing happened
    TIMEOUT_BEFORE_EFFECT = "timeout_before_effect"
    TIMEOUT_AFTER_EFFECT = "timeout_after_effect"  # effect applied, response lost
    MALFORMED = "malformed"                      # upstream returned garbage


class FaultRule(BaseModel):
    tool: str
    on_call: int = 1     # 1-based: which invocation of this tool trips the fault
    kind: FaultKind
    repeat: int = 1      # how many consecutive invocations, starting at on_call


class FaultPlan:
    def __init__(self, rules: list[FaultRule] | None = None) -> None:
        self._rules = list(rules or [])
        self._calls: dict[str, int] = {}

    def next(self, tool: str) -> FaultKind | None:
        """Register one invocation of `tool` and return the fault it trips, if any."""
        n = self._calls.get(tool, 0) + 1
        self._calls[tool] = n
        for r in self._rules:
            if r.tool == tool and r.on_call <= n < r.on_call + r.repeat:
                return r.kind
        return None
