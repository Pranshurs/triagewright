"""Typed tool model and the gateway that executes tools against the environment.

The gateway plays the part of the real upstream systems: it validates input,
injects configured faults, and honours idempotency keys on writes the way a
payment processor does. It does not decide whether a call is *allowed*; that is
the policy's job (see `fdeops.policy`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ValidationError

from fdeops.env.faults import FaultKind, FaultPlan
from fdeops.env.store import Environment


class Effect(StrEnum):
    READ = "read"
    SAFE_WRITE = "safe_write"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"

    @property
    def writes(self) -> bool:
        return self in (Effect.SAFE_WRITE, Effect.APPROVAL_REQUIRED)


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable


Handler = Callable[[Environment, Any], dict[str, Any]]
# Returns the account an invocation touches, or None for global resources
# (plans, runbooks, incidents). Raises ToolError if the referenced object is missing.
Scope = Callable[[Environment, Any], str | None]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    domain: str
    effect: Effect
    input_model: type[BaseModel]
    handler: Handler | None
    scope: Scope

    def json_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        if (tool.effect is Effect.DENIED) != (tool.handler is None):
            raise ValueError(f"{tool.name}: DENIED tools, and only they, have no handler")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)


def canonical_args(args: dict[str, Any]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)


def args_hash(tool: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(f"{tool}\n{canonical_args(args)}".encode()).hexdigest()


class Outcome(StrEnum):
    OK = "ok"
    ERROR = "error"      # definitely no effect
    UNKNOWN = "unknown"  # a write may or may not have happened


@dataclass(frozen=True)
class ToolResult:
    outcome: Outcome
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error: str | None = None
    retryable: bool = False
    replayed: bool = False  # served from the idempotency store

    def payload(self) -> dict[str, Any]:
        if self.outcome is Outcome.OK:
            return {"ok": True, "data": self.data, "replayed": self.replayed}
        return {
            "ok": False,
            "outcome": self.outcome.value,
            "error_code": self.error_code,
            "error": self.error,
            "retryable": self.retryable,
        }


class Gateway:
    def __init__(self, env: Environment, registry: Registry, faults: FaultPlan | None = None):
        self.env = env
        self.registry = registry
        self.faults = faults or FaultPlan()

    def parse(self, name: str, args: dict[str, Any]) -> tuple[Tool, BaseModel]:
        tool = self.registry.get(name)
        if tool is None:
            raise ToolError("UNKNOWN_TOOL", f"no tool named {name!r}")
        try:
            return tool, tool.input_model.model_validate(args)
        except ValidationError as e:
            raise ToolError("INVALID_ARGS", e.errors(include_url=False).__repr__()) from e

    def scope_of(self, name: str, args: dict[str, Any]) -> str | None:
        tool, parsed = self.parse(name, args)
        return tool.scope(self.env, parsed)

    def invoke(
        self, name: str, args: dict[str, Any], idempotency_key: str | None = None
    ) -> ToolResult:
        try:
            tool, parsed = self.parse(name, args)
        except ToolError as e:
            return ToolResult(Outcome.ERROR, error_code=e.code, error=e.message)
        if tool.handler is None:
            return ToolResult(Outcome.ERROR, error_code="NOT_PERMITTED", error="tool is disabled")
        if tool.effect.writes and not idempotency_key:
            return ToolResult(
                Outcome.ERROR, error_code="IDEMPOTENCY_KEY_REQUIRED", error="writes need a key"
            )

        fault = self.faults.next(name)
        if fault in (FaultKind.TRANSIENT_ERROR, FaultKind.RATE_LIMITED):
            return ToolResult(
                Outcome.ERROR, error_code=fault.value.upper(), error="upstream", retryable=True
            )
        if fault is FaultKind.TIMEOUT_BEFORE_EFFECT:
            return self._timeout(tool)
        if fault is FaultKind.MALFORMED:
            return ToolResult(
                Outcome.UNKNOWN if tool.effect.writes else Outcome.ERROR,
                error_code="MALFORMED_RESPONSE",
                error="upstream returned an unparseable body",
                retryable=True,
            )

        try:
            result = self._execute(tool, parsed, args, idempotency_key)
        except ToolError as e:
            return ToolResult(Outcome.ERROR, error_code=e.code, error=e.message,
                              retryable=e.retryable)
        if fault is FaultKind.TIMEOUT_AFTER_EFFECT:
            return self._timeout(tool)
        return result

    def _timeout(self, tool: Tool) -> ToolResult:
        return ToolResult(
            Outcome.UNKNOWN if tool.effect.writes else Outcome.ERROR,
            error_code="TIMEOUT",
            error="upstream did not respond in time",
            retryable=True,
        )

    def _execute(
        self, tool: Tool, parsed: BaseModel, raw: dict[str, Any], key: str | None
    ) -> ToolResult:
        assert tool.handler is not None
        if not tool.effect.writes:
            return ToolResult(Outcome.OK, data=tool.handler(self.env, parsed))
        assert key is not None
        h = args_hash(tool.name, parsed.model_dump(mode="json"))
        with self.env.transaction():
            prior = self.env.one("SELECT * FROM idempotency WHERE key = ?", [key])
            if prior is not None:
                if prior["tool"] != tool.name or prior["args_hash"] != h:
                    raise ToolError("IDEMPOTENCY_CONFLICT", "key reused with different request")
                return ToolResult(Outcome.OK, data=json.loads(prior["result"]), replayed=True)
            data = tool.handler(self.env, parsed)
            self.env.insert(
                "idempotency",
                {"key": key, "tool": tool.name, "args_hash": h, "result": json.dumps(data)},
            )
        self.env.tick()
        return ToolResult(Outcome.OK, data=data)
