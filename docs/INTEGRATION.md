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

`GET /api/cases/{id}/otel` and `triagewright otel <case_id>` return the case as
OTLP/JSON: a root span per case, a span per decision, child spans per tool call and
reconciliation, and approval and status changes as span events. Set
`TRIAGEWRIGHT_OTLP_ENDPOINT` (for example `http://localhost:4318/v1/traces`) to push to
a collector after each case step.

The export uses an allow-list: tool names, verdicts, rules, outcomes, error codes,
record ids and operators. Arguments, results, rationales, notes, approval bindings and
idempotency keys are not exported. Export failures are logged and swallowed; a test
checks that a failing exporter leaves the trace, decisions and effects unchanged.

## Docker

```bash
docker compose up --build
```

This serves the console at http://localhost:8000. Case data lives in a named volume and
survives container restarts (pending approvals included). The container runs as a
non-root user. Docker here proves deployability; it is not a security boundary.
