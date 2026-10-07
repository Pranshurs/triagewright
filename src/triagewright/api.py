"""HTTP transport. Every handler forwards to CaseService; none of them decides
anything about scope, safety, approval or idempotency."""

from __future__ import annotations

import os
from importlib import resources
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict

from triagewright import __version__
from triagewright.model import Decision
from triagewright.runner import NotAccepting, StaleApproval
from triagewright.scenarios import registry
from triagewright.service import CaseService, UnknownCase
from triagewright.telemetry import otlp_json


class NewCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str
    arm: str | None = "good"   # null: an external agent drives the case


class OperatorDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool
    operator: str
    binding: str               # the binding the operator was shown
    note: str | None = None


def create_app(service: CaseService | None = None) -> FastAPI:
    svc = service or CaseService(os.environ.get("TRIAGEWRIGHT_RUNS", "runs"))
    app = FastAPI(title="Triagewright", version=__version__)

    def guard(fn: Any, *a: Any, **kw: Any) -> Any:
        try:
            return fn(*a, **kw)
        except UnknownCase as e:
            raise HTTPException(404, f"unknown case {e.args[0]}") from e
        except (StaleApproval, NotAccepting) as e:
            raise HTTPException(409, str(e)) from e
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/", response_class=HTMLResponse)
    def ui() -> str:
        return resources.files("triagewright.web").joinpath("index.html").read_text("utf-8")

    @app.get("/api/scenarios")
    def scenarios() -> list[dict[str, Any]]:
        return [{"id": s.id, "title": s.title, "invariant": s.invariant,
                 "arms": {k: v.about for k, v in s.arms.items()}}
                for s in registry().values()]

    @app.get("/api/tools")
    def tools() -> list[dict[str, Any]]:
        return svc.tools()

    @app.get("/api/cases")
    def cases() -> list[dict[str, Any]]:
        return svc.cases()

    @app.post("/api/cases", status_code=201)
    def create(body: NewCase) -> dict[str, Any]:
        return {"case_id": guard(svc.create, body.scenario, body.arm)}

    @app.get("/api/cases/{case_id}")
    def view(case_id: str) -> dict[str, Any]:
        return guard(svc.view, case_id)  # type: ignore[no-any-return]

    @app.post("/api/cases/{case_id}/run")
    def run(case_id: str) -> dict[str, Any]:
        return {"status": guard(svc.run, case_id)}

    @app.post("/api/cases/{case_id}/decisions")
    def submit(case_id: str, decision: Annotated[Decision, Body()]) -> dict[str, Any]:
        return guard(svc.submit, case_id, decision)  # type: ignore[no-any-return]

    @app.post("/api/cases/{case_id}/approvals/{approval_id}")
    def decide(case_id: str, approval_id: str, body: OperatorDecision) -> dict[str, Any]:
        return guard(svc.decide, case_id, approval_id, body.approve,  # type: ignore[no-any-return]
                     body.operator, body.binding, body.note)

    @app.get("/api/cases/{case_id}/trace")
    def trace(case_id: str) -> list[dict[str, Any]]:
        return guard(svc.trace, case_id)  # type: ignore[no-any-return]

    @app.get("/api/cases/{case_id}/record", response_class=PlainTextResponse)
    def record(case_id: str) -> str:
        return guard(svc.record, case_id)  # type: ignore[no-any-return]

    @app.get("/api/cases/{case_id}/score")
    def score(case_id: str) -> dict[str, Any]:
        return guard(svc.score, case_id)  # type: ignore[no-any-return]

    @app.get("/api/cases/{case_id}/otel")
    def otel(case_id: str) -> dict[str, Any]:
        return otlp_json(guard(svc.trace, case_id), service_case=case_id)

    return app
