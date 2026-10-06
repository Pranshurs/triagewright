# Triagewright

An operations agent that works support and ops cases end to end inside a simulated
company. It investigates across business systems, diagnoses from what those systems
returned, fixes what policy allows, asks a human before consequential actions,
recovers from tool failures without duplicating effects, and leaves an auditable
case record.

**Status:** pre-alpha, not released. Implemented: the simulated environment, the runner
(policy, approvals, idempotent writes, reconciliation, crash recovery, grounding), ten
scenarios with adversarial arms, an independent scorer, an HTTP API with an operator
console, an MCP server and an OpenTelemetry export. All results come from scripted
agents. They are evidence about the runtime and the evaluator, not about model quality.

```bash
pip install -e '.[server,mcp]'
triagewright eval            # every scenario arm, scored
triagewright run S01         # one case, with its record and score
triagewright serve           # console at http://127.0.0.1:8000
```

Docs: [spec](docs/SPEC.md) · [evaluation](docs/EVALS.md) ·
[transports](docs/INTEGRATION.md) · [limitations](docs/LIMITATIONS.md)
