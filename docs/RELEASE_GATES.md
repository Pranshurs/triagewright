# Release gates

Deterministic gates. A release requires all of them.

1. `pytest`: unit, integration, scenario, transport-parity, crash/resume and boundary
   tests.
2. `triagewright eval`: 24 declared arms with 24/24 expected score cards.
3. `python mutation/run.py`: 0 survived, 0 invalid; equivalent mutants carry a proof.
   Every test must be deterministic: a kill by a test that can fail by chance is not a
   kill (see the pass-2 correction in `mutation/RESULTS.md`).
4. `ruff check .` and `mypy` (strict).
5. Fresh-venv install from the built wheel, and a Docker compose smoke test.
6. `python observability/smoke.py` against the `observability` compose profile.

## Frozen regression tests

These pin properties that must not change without an explicit decision:

| Property | Test |
|----------|------|
| Telemetry never exports idempotency keys, approval bindings, amounts, rationales, notes, emails, request text or API keys | `test_transports.py::test_otel_export_carries_no_secrets_or_payloads` |
| A failing telemetry exporter changes no decision, trace or effect | `test_transports.py::test_telemetry_failure_changes_nothing` |
| MCP exposes no approve, reject or reopen capability | `test_transports.py::test_mcp_lists_no_operator_powers` |
| The model adapter never receives bindings, keys or operator tools | `test_endpoint_adapter.py::test_model_never_sees_approval_or_idempotency_authority` |
| CLI, service, HTTP and MCP reach identical decisions and effects | `test_transports.py::test_transport_parity` |
| HTTP exposes no route that executes a tool directly | `test_transports.py::test_http_has_no_route_that_executes_tools` |
| The scorer catches harm applied around the runner | `test_scorer_independence.py` |
| A write on an external system is never sent twice; reconciliation is a lookup | `test_hubspot.py::test_lost_response_after_effect_is_found_not_resent`, `::test_lost_request_before_effect_stays_unknown_and_is_not_resent` |
| OAuth state is required, single-use and short-lived | `test_hubspot.py::test_callback_state_is_required_single_use_and_short_lived` |
| No token, client secret or marker secret reaches state, trace, record, score, telemetry or logs | `test_hubspot.py::test_no_token_secret_or_key_reaches_any_output` |
| Metric labels are tool names, outcomes and statuses only | `test_observability.py::test_metrics_carry_only_tool_names_outcomes_and_statuses` |
| A span is exported once | `test_observability.py::test_each_span_is_pushed_once_and_only_when_final` |

Contract change (explicit decision, 2026-10-07): the list of HTTP POST routes pinned
by `test_http_has_no_route_that_executes_tools` gained two operator routes with the
HubSpot connector, `recheck` (a read-only lookup of an existing unknown write) and
`hubspot/disconnect` (manages the connection). Neither executes a tool, and the test
still pins the full list.

## Publication scan

Before publication, the current tree and all reachable history are scanned,
case-insensitively, for references to development tools or process attribution. The
scan uses whole-word matching for short terms. Intentional, explained matches:

- `LICENSE`: one generic phrase in the standard Apache-2.0 text, kept verbatim.
- `docs/RELATED_WORK.md`: the third-party project names HolmesGPT and AIOpsLab.

## Not gates

Live-model runs (`triagewright live`) are demonstrations, recorded with model id,
settings and date. Their success rates are not release criteria. No release so far
includes a live-model run (status: live demonstration pending).

## Manual checks before release

- Browser smoke test of the operator console (2026-10-07): passed. A case loaded with
  its evidence, diagnosis, score and trace. A stale approval page was refused by the
  server. A genuine approval executed once. State survived a page refresh and a server
  restart.
