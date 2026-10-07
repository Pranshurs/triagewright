"""Metrics and the approval span, both derived from the canonical trace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from triagewright.service import CaseService
from triagewright.telemetry import Metrics, case_metrics, metrics_endpoint, otlp_json
from triagewright.tools.catalog import default_registry


class Capture:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def export(self, payload: dict[str, Any]) -> None:
        self.payloads.append(json.loads(json.dumps(payload)))


class Broken:
    def export(self, payload: dict[str, Any]) -> None:
        raise ConnectionError("collector down")


def run_s01(svc: CaseService) -> str:
    cid = svc.create("S01", arm="good")
    svc.run(cid)
    ap = svc.view(cid)["approvals"][0]
    svc.decide(cid, ap["id"], True, "op", ap["binding"])
    assert svc.view(cid)["status"] == "resolved"
    return cid


def points(payload: dict[str, Any], name: str) -> dict[tuple[tuple[str, Any], ...], Any]:
    (metric,) = [m for m in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
                 if m["name"] == name]
    body = metric.get("sum") or metric["histogram"]
    assert body["aggregationTemporality"] == 2
    out = {}
    for p in body["dataPoints"]:
        labels = tuple(sorted((a["key"].removeprefix("triagewright."),
                               next(iter(a["value"].values()))) for a in p["attributes"]))
        out[labels] = int(p["asInt"]) if "asInt" in p else p
    return out


def test_s01_metrics_tell_the_operational_story(tmp_path: Path) -> None:
    traces, metrics = Capture(), Capture()
    svc = CaseService(tmp_path, exporter=traces, metrics_exporter=metrics)
    run_s01(svc)
    last = metrics.payloads[-1]
    calls = points(last, "triagewright.tool.calls")
    assert calls[(("outcome", "unknown"), ("tool", "issue_refund"))] == 1
    assert calls[(("outcome", "ok"), ("tool", "get_ticket"))] == 1
    assert points(last, "triagewright.reconciliations") == {
        (("outcome", "ok"), ("tool", "issue_refund")): 1}
    assert points(last, "triagewright.cases") == {(("status", "resolved"),): 1}
    (wait,) = points(last, "triagewright.approval.wait").items()
    assert wait[0] == (("approved", "true"), ("tool", "issue_refund"))
    assert wait[1]["count"] == "1" and wait[1]["sum"] >= 0
    assert sum(map(int, wait[1]["bucketCounts"])) == 1
    assert len(wait[1]["bucketCounts"]) == len(wait[1]["explicitBounds"]) + 1
    (duration,) = points(last, "triagewright.case.duration").values()
    assert duration["count"] == "1" and duration["sum"] >= wait[1]["sum"]
    assert traces.payloads and len(traces.payloads) == len(metrics.payloads)


def test_a_case_is_counted_once_however_often_it_is_exported(tmp_path: Path) -> None:
    metrics = Capture()
    svc = CaseService(tmp_path, metrics_exporter=metrics)
    first = run_s01(svc)
    once = points(metrics.payloads[-1], "triagewright.tool.calls")
    for _ in range(3):
        svc._export(first)
    assert points(metrics.payloads[-1], "triagewright.tool.calls") == once
    run_s01(svc)
    twice = points(metrics.payloads[-1], "triagewright.tool.calls")
    assert twice == {k: 2 * v for k, v in once.items()}
    assert points(metrics.payloads[-1], "triagewright.cases") == {(("status", "resolved"),): 2}


def test_unfinished_case_reports_no_duration_or_final_status(tmp_path: Path) -> None:
    svc = CaseService(tmp_path)
    cid = svc.create("S01", arm="good")
    svc.run(cid)                               # stops at the approval
    m = case_metrics(svc.trace(cid), svc.session(cid).trace.times)
    assert m.cases == {} and m.case_duration == {} and m.approval_wait == {}
    assert sum(m.tool_calls.values()) > 5


def test_durations_need_wall_clock_times() -> None:
    events = [{"seq": 1, "type": "approval_requested", "approval_id": "apr_001", "tool": "t"},
              {"seq": 2, "type": "approval_decided", "approval_id": "apr_001",
               "approved": False},
              {"seq": 3, "type": "case_status", "status": "escalated"}]
    bare = case_metrics(events)                # a trace reloaded from disk has no times
    assert bare.approval_wait == {} and bare.case_duration == {}
    assert bare.cases == {(("status", "escalated"),): 1}
    timed = case_metrics(events, {1: 10**9, 2: 4 * 10**9, 3: 5 * 10**9})
    assert timed.approval_wait == {(("approved", "false"), ("tool", "t")): [3.0]}
    assert timed.case_duration == {(("status", "escalated"),): [4.0]}


def test_metrics_carry_only_tool_names_outcomes_and_statuses(tmp_path: Path) -> None:
    metrics = Capture()
    svc = CaseService(tmp_path, metrics_exporter=metrics)
    cid = run_s01(svc)
    scope = metrics.payloads[-1]["resourceMetrics"][0]["scopeMetrics"][0]
    seen: dict[str, set[str]] = {}
    for m in scope["metrics"]:
        for p in (m.get("sum") or m["histogram"])["dataPoints"]:
            for a in p["attributes"]:
                seen.setdefault(a["key"], set()).add(str(next(iter(a["value"].values()))))
    assert set(seen) == {"triagewright.tool", "triagewright.outcome", "triagewright.status",
                         "triagewright.approved"}
    assert seen["triagewright.tool"] <= set(default_registry().names())
    assert seen["triagewright.outcome"] <= {"ok", "error", "unknown"}
    assert seen["triagewright.status"] == {"resolved"}
    assert seen["triagewright.approved"] == {"true"}
    # Nothing identifying a case, an action or an approval, in labels or anywhere else.
    blob = json.dumps(metrics.payloads[-1])
    s = svc.session(cid)
    for leak in (cid, "act_0", "apr_0", "obs_0", s.state.approvals[0].binding,
                 s.state.actions[0].idempotency_key, "halvard", "duplicate capture"):
        assert leak not in blob


def test_failing_metrics_exporter_changes_nothing(tmp_path: Path) -> None:
    out = []
    for svc in (CaseService(tmp_path / "a"),
                CaseService(tmp_path / "b", metrics_exporter=Broken())):
        cid = run_s01(svc)
        out.append(([{k: v for k, v in e.items()} for e in svc.trace(cid)],
                    [(r["tool"], r["args"]) for r in svc.session(cid).env.effects()]))
    assert out[0] == out[1]


def test_metrics_endpoint_follows_the_traces_endpoint() -> None:
    assert metrics_endpoint("http://c:4318/v1/traces") == "http://c:4318/v1/metrics"
    assert metrics_endpoint("http://c:4318/v1/traces/") == "http://c:4318/v1/metrics"
    assert metrics_endpoint("http://c:4318/custom") is None
    assert metrics_endpoint(None) is None
    assert metrics_endpoint("http://c:4318/v1/traces", "http://m/x") == "http://m/x"


def test_approval_is_a_span_that_owns_the_write_and_its_reconciliation(
        tmp_path: Path) -> None:
    svc = CaseService(tmp_path)
    cid = run_s01(svc)
    s = svc.session(cid)
    spans = otlp_json(s.trace.events, cid, s.trace.times)[
        "resourceSpans"][0]["scopeSpans"][0]["spans"]
    by_name = {sp["name"]: sp for sp in spans}
    root, wait = by_name["case"], by_name["approval issue_refund"]
    assert wait["parentSpanId"] == root["spanId"]
    assert [e["name"] for e in wait["events"]] == ["approval_decided"]
    children = [sp for sp in spans if sp.get("parentSpanId") == wait["spanId"]]
    assert [sp["name"] for sp in children] == ["tool issue_refund", "reconcile act_004"]
    assert children[0]["status"]["code"] == 2 and children[1]["status"]["code"] == 1
    assert int(wait["startTimeUnixNano"]) <= int(wait["events"][0]["timeUnixNano"]) \
        <= int(children[1]["startTimeUnixNano"]) <= int(wait["endTimeUnixNano"])
    # Reads and safe writes still hang off the decision that asked for them.
    decide = by_name["decide get_ticket"]
    assert by_name["tool get_ticket"]["parentSpanId"] == decide["spanId"]
    assert Metrics().otlp_json()["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]


def _spans(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(payload["resourceSpans"][0]["scopeSpans"][0]["spans"])


def test_each_span_is_pushed_once_and_only_when_final(tmp_path: Path) -> None:
    traces = Capture()
    svc = CaseService(tmp_path, exporter=traces)
    cid = run_s01(svc)
    pushed = [sp for p in traces.payloads for sp in _spans(p)]
    ids = [sp["spanId"] for sp in pushed]
    assert len(ids) == len(set(ids)) and len(traces.payloads) >= 2
    s = svc.session(cid)
    full = _spans(otlp_json(s.trace.events, cid, s.trace.times))
    assert sorted(pushed, key=lambda sp: sp["spanId"]) == \
        sorted(full, key=lambda sp: sp["spanId"])
    # While the case waited for the operator, neither the case span nor the approval
    # span nor the decision still in progress had been sent.
    early = {sp["name"] for sp in _spans(traces.payloads[0])}
    assert "tool get_ticket" in early and "decide get_ticket" in early
    assert not early & {"case", "approval issue_refund", "decide finish"}
    assert {"case", "approval issue_refund"} <= {sp["name"] for sp in
                                                 _spans(traces.payloads[-1])}
    before = len(traces.payloads)
    svc._export(cid)                           # nothing new: nothing sent
    assert len(traces.payloads) == before


def test_a_failed_push_is_offered_again(tmp_path: Path) -> None:
    class Flaky(Capture):
        fail = True

        def export(self, payload: dict[str, Any]) -> None:
            if self.fail:
                raise ConnectionError("collector down")
            super().export(payload)

    traces = Flaky()
    svc = CaseService(tmp_path, exporter=traces)
    cid = svc.create("S01", arm="good")
    svc.run(cid)
    assert traces.payloads == []
    traces.fail = False
    ap = svc.view(cid)["approvals"][0]
    svc.decide(cid, ap["id"], True, "op", ap["binding"])
    s = svc.session(cid)
    assert {sp["spanId"] for p in traces.payloads for sp in _spans(p)} == \
        {sp["spanId"] for sp in _spans(otlp_json(s.trace.events, cid, s.trace.times))}


def test_a_restarted_process_does_not_resend_finished_spans(tmp_path: Path) -> None:
    first = Capture()
    svc = CaseService(tmp_path, exporter=first)
    cid = svc.create("S01", arm="good")
    svc.run(cid)
    later = Capture()
    svc2 = CaseService(tmp_path, exporter=later)
    ap = svc2.view(cid)["approvals"][0]
    svc2.decide(cid, ap["id"], True, "op", ap["binding"])
    a = [sp["spanId"] for p in first.payloads for sp in _spans(p)]
    b = [sp for p in later.payloads for sp in _spans(p)]
    assert not set(a) & {sp["spanId"] for sp in b}
    root = next(sp for sp in b if sp["name"] == "case")
    # Events from the earlier process have no wall-clock time here; the span must not
    # reach back to a placeholder date.
    assert int(root["startTimeUnixNano"]) > 1_750_000_000_000_000_000
