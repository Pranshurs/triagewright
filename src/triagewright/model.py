"""What a model may say, and the provider protocol.

A model proposes intent only. There is deliberately no way to express "this is
safe", "this was approved", "use account X" or "that write failed": the runner
derives those facts itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from triagewright.state import CaseState, Resolution
from triagewright.tools.base import Registry


class _D(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rationale: str = ""


class UseTool(_D):
    kind: Literal["use_tool"] = "use_tool"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)  # observation ids supporting a write


class AskCustomer(_D):
    kind: Literal["ask_customer"] = "ask_customer"
    question: str


class Finish(_D):
    kind: Literal["finish"] = "finish"
    resolution: Resolution


Decision = Annotated[UseTool | AskCustomer | Finish, Field(discriminator="kind")]


class CaseView:
    """Read-only window the model sees."""

    def __init__(self, state: CaseState, registry: Registry, feedback: str | None) -> None:
        self.state = state
        self.registry = registry
        self.feedback = feedback  # the runner's reply to the previous decision


class Model(Protocol):
    name: str

    def decide(self, view: CaseView) -> UseTool | AskCustomer | Finish: ...


Step = UseTool | AskCustomer | Finish | Callable[[CaseView], UseTool | AskCustomer | Finish]


class ScriptedModel:
    """Deterministic model: replays a fixed script.

    The script position is derived from the persisted state (number of decisions
    the runner has recorded), so a paused case resumes at the right step.
    Steps may be callables so scripts can refer to observation ids they saw.
    """

    name = "scripted"

    def __init__(self, steps: Sequence[Step]) -> None:
        self.steps = list(steps)

    def decide(self, view: CaseView) -> UseTool | AskCustomer | Finish:
        i = view.state.step
        if i >= len(self.steps):
            raise IndexError(f"script exhausted at step {i}")
        s = self.steps[i]
        return s(view) if callable(s) else s
