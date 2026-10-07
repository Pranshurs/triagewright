"""OpenTelemetry export of the canonical trace (OTLP/JSON), observational only.

The canonical trace stays authoritative; this derives spans from it. By default only
an allow-list of operational attributes leaves the process: tool names, verdicts,
rules, outcomes, error codes and record ids. Arguments, results, rationales, notes,
approval bindings and idempotency keys are never exported unless payload export is
explicitly enabled, and bindings/keys not even then.

Exporting can never fail a case: `safe_export` swallows every error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from triagewright import __version__

log = logging.getLogger(__name__)

NEVER = {"binding", "idempotency_key", "args", "result", "rationale", "note", "decision",
         "resolution", "case", "summary", "problems", "question", "detail", "evidence",
         "unknown_evidence", "reason"}
ALLOWED = {"tool", "verdict", "rule", "outcome", "error_code", "attempt", "action_id",
           "approval_id", "obs_id", "ok", "status", "approved", "replayed", "step", "operator"}


def _attrs(event: Mapping[str, Any], include_payloads: bool) -> list[dict[str, Any]]:
    out = []
    for k, v in event.items():
        if k in ("seq", "type"):
            continue
        if k in ALLOWED and not isinstance(v, dict | list):
            out.append(_kv(f"triagewright.{k}", v))
        elif include_payloads and k == "args":
            out.append(_kv("triagewright.args", json.dumps(v, sort_keys=True)))
    return out


def _kv(key: str, v: Any) -> dict[str, Any]:
    if isinstance(v, bool):
        val: dict[str, Any] = {"boolValue": v}
    elif isinstance(v, int):
        val = {"intValue": str(v)}
    elif v is None:
        val = {"stringValue": ""}
    else:
        val = {"stringValue": str(v)}
    return {"key": key, "value": val}


def _id(*parts: Any, n: int) -> str:
    return hashlib.sha256("/".join(map(str, parts)).encode()).hexdigest()[:n]


def otlp_json(events: Sequence[Mapping[str, Any]], service_case: str,
              times: Mapping[int, int] | None = None,
              include_payloads: bool = False) -> dict[str, Any]:
    """Build an OTLP/JSON ExportTraceServiceRequest: one trace per case.

    Spans: the case (root), each decision, and each tool call / reconciliation inside
    it. Approval and status events become span events on the root.
    """
    times = times or {}
    base = 1_700_000_000_000_000_000
    t = {int(e["seq"]): times.get(int(e["seq"]), base + int(e["seq"]) * 1_000_000)
         for e in events}
    trace_id = _id("case", service_case, n=32)
    root_id = _id(service_case, "root", n=16)
    last = max(t.values()) if t else base
    spans: list[dict[str, Any]] = []
    root_events: list[dict[str, Any]] = []
    status = ""
    current: dict[str, Any] | None = None

    def close(span: dict[str, Any] | None, end: int) -> None:
        if span is not None:
            span["endTimeUnixNano"] = str(max(end, int(span["startTimeUnixNano"])))
            spans.append(span)

    for e in events:
        seq, typ = int(e["seq"]), e["type"]
        ts = t[seq]
        if typ == "decision":
            close(current, ts)
            d = e.get("decision", {})
            name = f"decide {d.get('tool') or d.get('kind')}"
            current = {"traceId": trace_id, "spanId": _id(service_case, seq, n=16),
                       "parentSpanId": root_id, "name": name, "kind": 1,
                       "startTimeUnixNano": str(ts),
                       "attributes": [_kv("triagewright.decision.kind", d.get("kind")),
                                      _kv("triagewright.step", e.get("step"))]
                       + ([_kv("triagewright.tool", d["tool"])] if d.get("tool") else [])}
        elif typ == "policy" and current is not None:
            current["attributes"] += [_kv("triagewright.policy.verdict", e["verdict"]),
                                      _kv("triagewright.policy.rule", e["rule"])]
        elif typ in ("tool_call", "reconcile"):
            spans.append({
                "traceId": trace_id, "spanId": _id(service_case, seq, n=16),
                "parentSpanId": current["spanId"] if current else root_id,
                "name": f"{'tool' if typ == 'tool_call' else 'reconcile'} "
                        f"{e.get('tool', e.get('action_id'))}",
                "kind": 3, "startTimeUnixNano": str(ts), "endTimeUnixNano": str(ts),
                "attributes": _attrs(e, include_payloads),
                "status": {"code": 1 if e.get("outcome") == "ok" else 2}})
        else:
            if typ == "case_status":
                status = str(e["status"])
            root_events.append({"timeUnixNano": str(ts), "name": typ,
                                "attributes": _attrs(e, include_payloads=False)})
    close(current, last)
    first = min(t.values()) if t else base
    spans.insert(0, {"traceId": trace_id, "spanId": root_id, "name": "case",
                     "kind": 1, "startTimeUnixNano": str(first),
                     "endTimeUnixNano": str(last), "events": root_events,
                     "attributes": [_kv("triagewright.case.id", service_case),
                                    _kv("triagewright.case.status", status)]})
    return {"resourceSpans": [{
        "resource": {"attributes": [_kv("service.name", "triagewright")]},
        "scopeSpans": [{"scope": {"name": "triagewright", "version": __version__},
                        "spans": spans}]}]}


class Exporter(Protocol):
    def export(self, payload: dict[str, Any]) -> None: ...


class OtlpHttpExporter:
    """POST OTLP/JSON to a collector, e.g. http://localhost:4318/v1/traces."""

    def __init__(self, endpoint: str, timeout: float = 2.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout

    def export(self, payload: dict[str, Any]) -> None:
        req = urllib.request.Request(self.endpoint, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout):  # noqa: S310 - configured URL
            pass


def safe_export(exporter: Exporter | None, payload_fn: Any) -> bool:
    """Export, never raise. Returns whether it succeeded."""
    if exporter is None:
        return False
    try:
        exporter.export(payload_fn())
        return True
    except Exception as e:  # telemetry must not affect execution
        log.warning("telemetry export failed: %s", e)
        return False
