"""OpenTelemetry export of the canonical trace (OTLP/JSON), observational only.

The canonical trace stays authoritative; this derives spans from it. By default only
an allow-list of operational attributes leaves the process: tool names, verdicts,
rules, outcomes, error codes and record ids. Arguments, results, rationales, notes,
approval bindings and idempotency keys are never exported unless payload export is
explicitly enabled, and bindings/keys not even then.

Metrics are derived from the same trace: a handful of counters and histograms whose
labels are tool names, outcomes and statuses, nothing else.

Exporting can never fail a case: `safe_export` swallows every error.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import logging
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from triagewright import __version__

log = logging.getLogger(__name__)

NEVER = {"binding", "idempotency_key", "args", "result", "rationale", "note", "decision",
         "resolution", "case", "summary", "problems", "question", "detail", "evidence",
         "unknown_evidence", "reason"}
ALLOWED = {"tool", "verdict", "rule", "outcome", "error_code", "attempt", "action_id",
           "approval_id", "obs_id", "ok", "status", "approved", "replayed", "step", "operator"}


TERMINAL = {"resolved", "escalated", "needs_info", "needs_attention", "failed"}


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

    Spans: the case (root), each decision with its tool calls, and each approval from
    request to the end of the write it authorised (the operator's decision is an event
    on it). Approval and status events are also span events on the root.
    """
    return _build(events, service_case, times, include_payloads)[0]


def otlp_increment(events: Sequence[Mapping[str, Any]], service_case: str,
                   times: Mapping[int, int] | None,
                   sent: set[str]) -> tuple[dict[str, Any] | None, set[str]]:
    """The spans that are final and not in `sent`, and their ids.

    A backend keeps every span it is given, so a span is pushed once, when nothing
    about it can still change: tool calls at once, a decision when the next one starts,
    the case and its approvals when the case ends.
    """
    payload, still_open = _build(events, service_case, times, False)
    scope = payload["resourceSpans"][0]["scopeSpans"][0]
    scope["spans"] = [sp for sp in scope["spans"]
                      if sp["spanId"] not in sent and sp["spanId"] not in still_open]
    ids = {sp["spanId"] for sp in scope["spans"]}
    return (payload if ids else None), ids


def _build(events: Sequence[Mapping[str, Any]], service_case: str,
           times: Mapping[int, int] | None,
           include_payloads: bool) -> tuple[dict[str, Any], set[str]]:
    times = times or {}
    base = 1_700_000_000_000_000_000
    # Wall-clock times exist only for events this process emitted. With none at all the
    # export is deterministic; with some, earlier events take the earliest known time.
    floor = min(times.values()) if times else None
    t = {int(e["seq"]): times.get(int(e["seq"]), base + int(e["seq"]) * 1_000_000
                                  if floor is None else floor)
         for e in events}
    trace_id = _id("case", service_case, n=32)
    root_id = _id(service_case, "root", n=16)
    last = max(t.values()) if t else base
    spans: list[dict[str, Any]] = []
    root_events: list[dict[str, Any]] = []
    status = ""
    current: dict[str, Any] | None = None
    approvals: dict[str, dict[str, Any]] = {}   # approval id -> its span
    parent_of: dict[str, str] = {}              # action id -> parent of its tool span

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
            # An approved write hangs off its approval, and a reconciliation off the
            # same parent as the write it settles; everything else off its decision.
            parent = current["spanId"] if current else root_id
            waited = approvals.get(str(e.get("approval_id")))
            if waited is not None:
                parent = waited["spanId"]
            action = str(e.get("action_id"))
            parent = parent_of.setdefault(action, parent) if e.get("action_id") else parent
            owner = next((a for a in approvals.values() if a["spanId"] == parent), None)
            if owner is not None:
                owner["endTimeUnixNano"] = str(ts)
            spans.append({
                "traceId": trace_id, "spanId": _id(service_case, seq, n=16),
                "parentSpanId": parent,
                "name": f"{'tool' if typ == 'tool_call' else 'reconcile'} "
                        f"{e.get('tool', e.get('action_id'))}",
                "kind": 3, "startTimeUnixNano": str(ts), "endTimeUnixNano": str(ts),
                "attributes": _attrs(e, include_payloads),
                "status": {"code": 1 if e.get("outcome") == "ok" else 2}})
        else:
            if typ == "case_status":
                status = str(e["status"])
            elif typ == "approval_requested":
                approvals[str(e["approval_id"])] = {
                    "traceId": trace_id, "parentSpanId": root_id, "kind": 1,
                    "spanId": _id(service_case, "approval", e["approval_id"], n=16),
                    "name": f"approval {e.get('tool')}", "startTimeUnixNano": str(ts),
                    "endTimeUnixNano": str(ts), "events": [],
                    "attributes": _attrs(e, include_payloads=False)}
            elif typ in ("approval_decided", "approval_expired", "approval_void") and \
                    str(e.get("approval_id")) in approvals:
                waited = approvals[str(e["approval_id"])]
                waited["endTimeUnixNano"] = str(ts)
                waited["events"].append({"timeUnixNano": str(ts), "name": typ,
                                         "attributes": _attrs(e, include_payloads=False)})
            root_events.append({"timeUnixNano": str(ts), "name": typ,
                                "attributes": _attrs(e, include_payloads=False)})
    close(current, last)
    spans.extend(approvals.values())
    first = min(t.values()) if t else base
    spans.insert(0, {"traceId": trace_id, "spanId": root_id, "name": "case",
                     "kind": 1, "startTimeUnixNano": str(first),
                     "endTimeUnixNano": str(last), "events": root_events,
                     "attributes": [_kv("triagewright.case.id", service_case),
                                    _kv("triagewright.case.status", status)]})
    still_open: set[str] = set()
    if status not in TERMINAL:
        still_open = {root_id, *(a["spanId"] for a in approvals.values())}
        if current is not None:
            still_open.add(current["spanId"])
    return {"resourceSpans": [{
        "resource": {"attributes": [_kv("service.name", "triagewright")]},
        "scopeSpans": [{"scope": {"name": "triagewright", "version": __version__},
                        "spans": spans}]}]}, still_open


# -- metrics ---------------------------------------------------------------------------

BOUNDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
          300.0, 1800.0, 3600.0)
Labels = tuple[tuple[str, str], ...]


@dataclass
class CaseMetrics:
    """What one case contributes. Labels are tool names, outcomes and statuses only:
    no ids, arguments, text or anything else from the case."""
    tool_calls: dict[Labels, int] = field(default_factory=dict)
    reconciliations: dict[Labels, int] = field(default_factory=dict)
    cases: dict[Labels, int] = field(default_factory=dict)
    case_duration: dict[Labels, list[float]] = field(default_factory=dict)
    approval_wait: dict[Labels, list[float]] = field(default_factory=dict)


def case_metrics(events: Sequence[Mapping[str, Any]],
                 times: Mapping[int, int] | None = None) -> CaseMetrics:
    """Derive a case's metrics from its canonical trace. Durations need wall-clock
    times, which exist only for events emitted by this process."""
    times = times or {}
    m = CaseMetrics()
    tool_of: dict[str, str] = {}
    asked: dict[str, tuple[str, int | None]] = {}
    status = ""

    def bump(d: dict[Labels, int], **labels: Any) -> None:
        key = tuple(sorted((k, str(v)) for k, v in labels.items()))
        d[key] = d.get(key, 0) + 1

    def seconds(d: dict[Labels, list[float]], start: int | None, end: int | None,
                **labels: Any) -> None:
        if start is not None and end is not None and end >= start:
            key = tuple(sorted((k, str(v)) for k, v in labels.items()))
            d.setdefault(key, []).append((end - start) / 1e9)

    for e in events:
        seq, typ = int(e["seq"]), e["type"]
        if typ == "tool_call":
            bump(m.tool_calls, tool=e["tool"], outcome=e["outcome"])
            if e.get("action_id"):
                tool_of[str(e["action_id"])] = str(e["tool"])
        elif typ == "reconcile":
            bump(m.reconciliations, tool=tool_of.get(str(e.get("action_id")), "unknown"),
                 outcome=e["outcome"])
        elif typ == "approval_requested":
            asked[str(e["approval_id"])] = (str(e["tool"]), times.get(seq))
        elif typ == "approval_decided" and str(e.get("approval_id")) in asked:
            tool, since = asked.pop(str(e["approval_id"]))
            seconds(m.approval_wait, since, times.get(seq), tool=tool,
                    approved=str(bool(e.get("approved"))).lower())
        elif typ == "case_status":
            status = str(e["status"])
    if status in TERMINAL and events:
        bump(m.cases, status=status)
        seconds(m.case_duration, times.get(int(events[0]["seq"])),
                times.get(int(events[-1]["seq"])), status=status)
    return m


class Metrics:
    """Cumulative metrics over the cases this process has handled.

    Each case's contribution is recomputed from its whole trace, so exporting the same
    case twice counts it once.
    """

    def __init__(self, clock: Any = time.time_ns) -> None:
        self._clock = clock
        self._start = clock()
        self._cases: dict[str, CaseMetrics] = {}

    def update(self, case_id: str, events: Sequence[Mapping[str, Any]],
               times: Mapping[int, int] | None = None) -> None:
        self._cases[case_id] = case_metrics(events, times)

    def otlp_json(self) -> dict[str, Any]:
        """OTLP/JSON ExportMetricsServiceRequest, cumulative temporality."""
        now = str(self._clock())
        stamp = {"startTimeUnixNano": str(self._start), "timeUnixNano": now}

        def attrs(labels: Labels) -> list[dict[str, Any]]:
            return [_kv(f"triagewright.{k}", v) for k, v in labels]

        def counter(name: str, unit: str, about: str, pick: str) -> dict[str, Any]:
            total: dict[Labels, int] = {}
            for c in self._cases.values():
                for labels, n in getattr(c, pick).items():
                    total[labels] = total.get(labels, 0) + n
            return {"name": name, "unit": unit, "description": about, "sum": {
                "aggregationTemporality": 2, "isMonotonic": True, "dataPoints": [
                    {"attributes": attrs(k), **stamp, "asInt": str(v)}
                    for k, v in sorted(total.items())]}}

        def histogram(name: str, about: str, pick: str) -> dict[str, Any]:
            values: dict[Labels, list[float]] = {}
            for c in self._cases.values():
                for labels, vs in getattr(c, pick).items():
                    values.setdefault(labels, []).extend(vs)
            points = []
            for labels, vs in sorted(values.items()):
                buckets = [0] * (len(BOUNDS) + 1)
                for v in vs:
                    buckets[bisect.bisect_left(BOUNDS, v)] += 1
                points.append({"attributes": attrs(labels), **stamp, "count": str(len(vs)),
                               "sum": sum(vs), "bucketCounts": [str(b) for b in buckets],
                               "explicitBounds": list(BOUNDS)})
            return {"name": name, "unit": "s", "description": about, "histogram": {
                "aggregationTemporality": 2, "dataPoints": points}}

        metrics = [
            counter("triagewright.tool.calls", "{call}", "Tool calls by tool and outcome.",
                    "tool_calls"),
            counter("triagewright.reconciliations", "{attempt}",
                    "Reconciliation attempts for writes with an unknown outcome.",
                    "reconciliations"),
            counter("triagewright.cases", "{case}", "Finished cases by final status.", "cases"),
            histogram("triagewright.case.duration", "Wall-clock time from intake to the "
                      "final status.", "case_duration"),
            histogram("triagewright.approval.wait", "Time an approval request waited for "
                      "an operator.", "approval_wait"),
        ]
        return {"resourceMetrics": [{
            "resource": {"attributes": [_kv("service.name", "triagewright")]},
            "scopeMetrics": [{"scope": {"name": "triagewright", "version": __version__},
                              "metrics": metrics}]}]}


def metrics_endpoint(traces_endpoint: str | None, explicit: str | None = None) -> str | None:
    """Where metrics go: an explicit URL, else the traces URL's sibling."""
    if explicit:
        return explicit
    if traces_endpoint and traces_endpoint.rstrip("/").endswith("/v1/traces"):
        return traces_endpoint.rstrip("/")[: -len("traces")] + "metrics"
    return None


class Exporter(Protocol):
    def export(self, payload: dict[str, Any]) -> None: ...


class OtlpHttpExporter:
    """POST OTLP/JSON to a collector, e.g. http://localhost:4318/v1/traces or
    .../v1/metrics."""

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
