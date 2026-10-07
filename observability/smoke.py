"""Smoke test of the observability profile, against the running compose stack.

    TRIAGEWRIGHT_OTLP_ENDPOINT=http://collector:4318/v1/traces \
      docker compose --profile observability up --build -d
    python observability/smoke.py

Runs scenario S01 through the HTTP API, then asks Jaeger for the trace and Prometheus
for the metrics. It proves the OTLP export is accepted by a real Collector and arrives
in both backends; it is not part of the unit suite.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

API = "http://127.0.0.1:8000"
JAEGER = "http://127.0.0.1:16686"
PROMETHEUS = "http://127.0.0.1:9090"


def call(url: str, body: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - fixed local URLs
        return json.loads(r.read())


def wait_for(what: str, probe: Any, seconds: float = 60.0) -> Any:
    deadline = time.time() + seconds
    while True:
        try:
            got = probe()
            if got:
                return got
        except OSError:
            pass
        if time.time() > deadline:
            sys.exit(f"FAIL: {what} did not appear within {seconds:.0f}s")
        time.sleep(2)


def prom(query: str) -> dict[str, float]:
    q = urllib.parse.urlencode({"query": query})
    out = {}
    for r in call(f"{PROMETHEUS}/api/v1/query?{q}")["data"]["result"]:
        labels = ",".join(f"{k}={v}" for k, v in sorted(r["metric"].items())
                          if k.startswith("triagewright_") or k == "le")
        out[labels] = float(r["value"][1])
    return out


def main() -> None:
    started = time.time()
    case = call(f"{API}/api/cases", {"scenario": "S01", "arm": "good"})["case_id"]
    call(f"{API}/api/cases/{case}/run", {})
    (ap,) = call(f"{API}/api/cases/{case}")["approvals"]
    time.sleep(1.5)  # a visible approval wait
    done = call(f"{API}/api/cases/{case}/approvals/{ap['id']}",
                {"approve": True, "operator": "smoke", "binding": ap["binding"]})
    assert done["status"] == "resolved", done
    print(f"case {case}: {done['status']}")

    def spans() -> Any:
        window = urllib.parse.urlencode({
            "query.service_name": "triagewright",
            "query.start_time_min": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(started - 60)),
            "query.start_time_max": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime(time.time() + 60))})
        got = call(f"{JAEGER}/api/v3/traces?{window}").get("result", {})
        found = [sp for rs in got.get("resourceSpans", []) for ss in rs["scopeSpans"]
                 for sp in ss["spans"]]
        root = next((sp for sp in found if sp["name"] == "case" and any(
            a["key"] == "triagewright.case.id" and a["value"]["stringValue"] == case
            for a in sp["attributes"])), None)
        return root and [sp for sp in found if sp["traceId"] == root["traceId"]]

    got = wait_for("the case's trace in Jaeger", spans)
    ids = [sp["spanId"] for sp in got]
    assert len(ids) == len(set(ids)), "a span was stored more than once"
    by_id = {sp["spanId"]: sp for sp in got}
    parent = {sp["name"]: by_id[sp["parentSpanId"]]["name"] for sp in got
              if sp.get("parentSpanId")}
    assert parent["approval issue_refund"] == "case", parent
    assert parent["tool issue_refund"] == "approval issue_refund", parent
    assert parent["reconcile act_004"] == "approval issue_refund", parent
    assert parent["tool retry_provisioning"] == "decide retry_provisioning", parent
    wait = next(sp for sp in got if sp["name"] == "approval issue_refund")
    waited = (int(wait["endTimeUnixNano"]) - int(wait["startTimeUnixNano"])) / 1e9
    assert waited >= 1.0, waited
    root = next(sp for sp in got if sp["name"] == "case")
    status = {a["key"]: a["value"] for a in root["attributes"]}["triagewright.case.status"]
    assert status == {"stringValue": "resolved"}, status
    print(f"jaeger: trace {root['traceId']} with {len(got)} spans, each stored once; "
          f"approval span {waited:.2f}s owning the refund and its reconciliation")

    wait_for("metrics in Prometheus",
             lambda: prom('triagewright_cases_total{triagewright_status="resolved"}'))
    time.sleep(6)  # one more scrape, so the final export is the one we read
    calls = prom("triagewright_tool_calls_total")
    refund = "triagewright_outcome=unknown,triagewright_tool=issue_refund"
    assert calls.get(refund, 0) >= 1, calls
    assert sum(prom("triagewright_reconciliations_total").values()) >= 1
    assert sum(prom("triagewright_approval_wait_seconds_count").values()) >= 1
    assert sum(prom("triagewright_case_duration_seconds_count").values()) >= 1
    labels = {k for r in call(f"{PROMETHEUS}/api/v1/series?" + urllib.parse.urlencode(
        {"match[]": '{__name__=~"triagewright_.*"}'}))["data"] for k in r
        if k.startswith("triagewright_")}
    assert labels <= {"triagewright_tool", "triagewright_outcome", "triagewright_status",
                      "triagewright_approved"}, labels
    print(f"prometheus: {int(sum(calls.values()))} tool calls, labels {sorted(labels)}")
    print("PASS")


if __name__ == "__main__":
    main()
