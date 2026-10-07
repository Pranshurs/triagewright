"""MCP transport: the tool catalogue served to an MCP client, bound to one case.

An MCP tool call is a *proposal*. It becomes a `UseTool` decision submitted to the
runner through CaseService, exactly like a scripted or HTTP decision: same policy,
scope, approval, idempotency and reconciliation. There is no MCP-only path to the
gateway, and no tool for deciding approvals (that is the operator's, not the agent's).

Reserved argument keys `_rationale` and `_evidence` carry the proposal's rationale and
cited observation ids; they are stripped before the tool's own arguments are checked.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from triagewright import __version__
from triagewright.model import Finish, UseTool
from triagewright.runner import NotAccepting
from triagewright.service import CaseService
from triagewright.state import Resolution
from triagewright.tools.base import Effect, Registry

FINISH = "triagewright_finish"
CASE = "triagewright_case"
RESERVED = ("_rationale", "_evidence")


def _tool_list(registry: Registry) -> list[types.Tool]:
    tools = []
    for t in registry:
        schema = t.input_model.model_json_schema()
        props = dict(schema.get("properties", {}))
        props["_rationale"] = {"type": "string", "description": "why this call"}
        props["_evidence"] = {"type": "array", "items": {"type": "string"},
                              "description": "observation ids supporting a write"}
        note = {Effect.APPROVAL_REQUIRED: " [an operator must approve this]",
                Effect.DENIED: " [not available to agents]"}.get(t.effect, "")
        tools.append(types.Tool(name=t.name, description=t.description + note,
                                input_schema={**schema, "properties": props}))
    tools.append(types.Tool(name=CASE, description="Current case state (read-only).",
                            input_schema={"type": "object", "properties": {}}))
    tools.append(types.Tool(name=FINISH, description="Finish the case with a resolution.",
                            input_schema=Resolution.model_json_schema()))
    return tools


def _text(payload: Any, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload, default=str))],
        is_error=error)


def handle(service: CaseService, case_id: str, name: str,
           arguments: dict[str, Any] | None) -> types.CallToolResult:
    args = dict(arguments or {})
    try:
        if name == CASE:
            return _text(service.view(case_id))
        if name == FINISH:
            decision: UseTool | Finish = Finish(resolution=Resolution.model_validate(args))
        else:
            rationale = str(args.pop("_rationale", "") or "")
            evidence = [str(e) for e in args.pop("_evidence", []) or []]
            # Unknown names and bad arguments go to the runner too, which rejects them
            # and records the attempt.
            decision = UseTool(tool=name, args=args, rationale=rationale, evidence=evidence)
        result = service.submit(case_id, decision)
    except NotAccepting as e:
        return _text({"error": str(e)}, error=True)
    except ValueError as e:  # pydantic validation of a finish payload
        return _text({"error": str(e)}, error=True)
    fb = result.get("feedback") or ""
    rejected = fb.startswith(("rejected", "denied"))
    return _text(result, error=rejected)


def build_server(service: CaseService, case_id: str) -> Server[Any]:
    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=_tool_list(service.registry()))

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return handle(service, case_id, params.name, params.arguments)

    return Server("triagewright", version=__version__,
                  instructions=f"Tools act on case {case_id}. Writes may need operator "
                               "approval; the runtime decides.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


def serve_stdio(service: CaseService, case_id: str) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(service, case_id)

    async def main() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(main)
