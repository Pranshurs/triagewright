# Transports and integration

Every way in (CLI, HTTP, MCP) goes through `CaseService`, which forwards decisions to
the runner. No transport contains policy, scope, approval or idempotency logic, and
none can reach the tool gateway directly. Tests drive the same proposals through the
scripted loop, the service, HTTP and MCP and require identical runner decisions and
upstream effects (`tests/test_transports.py`).

## HTTP API (`triagewright serve`)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Operator console |
| GET | `/api/scenarios`, `/api/tools` | Catalogue |
| POST | `/api/cases` | `{"scenario": "S01", "arm": "good"}`; `arm: null` = external agent drives |
| GET | `/api/cases`, `/api/cases/{id}` | Case list; case state (observations, actions, approvals) |
| POST | `/api/cases/{id}/run` | Run a scripted case until it finishes or waits |
| POST | `/api/cases/{id}/decisions` | One agent decision: `use_tool`, `ask_customer` or `finish` |
| POST | `/api/cases/{id}/approvals/{approval_id}` | Operator decision `{approve, operator, binding, note?}` |
| GET | `/api/cases/{id}/trace`, `/record`, `/score`, `/otel` | Canonical trace, case record, independent score, OTLP/JSON |
| POST | `/api/cases/{id}/actions/{action_id}/recheck` | Operator: look upstream again for an unknown write on an external system |
| GET | `/api/hubspot/status`, `/connect`, `/callback` | HubSpot connection (only when configured; see `HUBSPOT.md`) |
| POST | `/api/hubspot/disconnect` | Revoke and forget the HubSpot grant |

An operator decision must carry the `binding` of the approval the operator was shown.
If the stored request no longer hashes to it, or the approval is no longer pending, the
server answers 409 and records nothing. Request bodies reject unknown fields, so a
client cannot supply an account, arguments or a key alongside a decision.

There is no authentication: the operator name is a field. Put the API behind your own
gateway before exposing it.

## MCP (`triagewright mcp <case_id>`)

Serves the tool catalogue over stdio to an MCP client, bound to one case. Each tool
call is a proposal submitted to the runner. Two extra tools: `triagewright_case`
(read-only state) and `triagewright_finish` (submit a resolution). Optional reserved
arguments `_rationale` and `_evidence` carry the proposal's reason and cited
observation ids. There are no tools for approving, rejecting or reopening: those
belong to the operator.

```bash
triagewright open S01 --external --runs runs
```

The command prints a case id. Configure the MCP client to run
`triagewright mcp <case_id> --runs runs`.

## OpenTelemetry

Both signals are derived from the canonical trace, which stays authoritative. Set
`TRIAGEWRIGHT_OTLP_ENDPOINT` (for example `http://localhost:4318/v1/traces`) to push
OTLP/JSON to a collector after each case step. Metrics go to the sibling
`/v1/metrics` path, or to `TRIAGEWRIGHT_OTLP_METRICS_ENDPOINT` if set. Export failures
are logged and swallowed; tests check that a failing exporter leaves the trace,
decisions and effects unchanged.

**Traces.** One trace per case: a root span, a span per decision with its tool calls,
and a span per approval that runs from the request to the end of the write it
authorised, with the operator's decision as an event and the write and any
reconciliation as children. Each span is pushed once, when nothing about it can still
change: tool calls at once, a decision when the next one starts, the case and its
approvals when the case ends. `GET /api/cases/{id}/otel` and `triagewright otel
<case_id>` return the whole case at any time.

**Metrics** (cumulative):

| Metric | Type | Labels |
|--------|------|--------|
| `triagewright.tool.calls` | counter | tool, outcome |
| `triagewright.reconciliations` | counter | tool, outcome |
| `triagewright.cases` | counter | final status |
| `triagewright.case.duration` | histogram, seconds | final status |
| `triagewright.approval.wait` | histogram, seconds | tool, approved |

**What leaves the process.** Trace attributes come from an allow-list: tool names,
verdicts, rules, outcomes, error codes, record ids and operators. Metric labels are
tool names, outcomes and statuses only. Arguments, results, rationales, notes,
approval bindings, idempotency keys and credentials are not exported.

**Real backends.** The compose profile `observability` adds an OpenTelemetry
Collector, Jaeger and Prometheus:

```bash
TRIAGEWRIGHT_OTLP_ENDPOINT=http://collector:4318/v1/traces \
  docker compose --profile observability up --build -d
python observability/smoke.py
```

The smoke test runs scenario S01 through the API, then reads the trace back from
Jaeger (every span stored once, the refund and its reconciliation under the approval
span) and the metrics from Prometheus (only the labels above). Jaeger is at
http://localhost:16686 and Prometheus at http://localhost:9090. Last run 2026-10-07
with Collector 0.162.0, Jaeger 2.22.0 and Prometheus 3.15.0: passed.

Durations use wall-clock times that exist only in the process that emitted the events.
After a restart, earlier events of a case take the earliest time the new process knows,
and that case contributes no duration to the histograms.

## Docker

```bash
docker compose up --build
```

This serves the console at http://localhost:8000. Case data lives in a named volume and
survives container restarts (pending approvals included). The container runs as a
non-root user. Docker here proves deployability; it is not a security boundary.
