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

## Not gates

Live-model runs (`triagewright live`) are demonstrations, recorded with model id,
settings and date. Their success rates are not release criteria.

## Manual checks before release

- Browser smoke test of the operator console (2026-10-07): passed. A case loaded with
  its evidence, diagnosis, score and trace. A stale approval page was refused by the
  server. A genuine approval executed once. State survived a page refresh and a server
  restart.
