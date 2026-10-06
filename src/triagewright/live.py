"""Live-model runs: a real model drives selected scenarios through the same runner.

Results are demonstrations, recorded with model id, settings and date. They are not
release gates; the deterministic suite (`triagewright eval`, pytest) is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from triagewright.endpoint import EndpointModel
from triagewright.evals.run import record_of
from triagewright.evals.scorer import score
from triagewright.harness import open_session, run_with_operator
from triagewright.scenarios import registry

DEFAULT_SET = ("S01", "S03", "S05", "S08", "S09", "S10")
COLS = ("resolution", "harmful_effects", "unauthorized_attempts", "approval_correct",
        "unknown_closed", "scope_intact", "ungrounded", "escalation", "diagnosis_correct")


def run_live(model: str, base_url: str, scenarios: tuple[str, ...] = DEFAULT_SET,
             out: Path | None = None, temperature: float = 0.0, max_tokens: int = 1200,
             api_key_env: str = "TRIAGEWRIGHT_MODEL_KEY") -> dict[str, Any]:
    started = datetime.now(UTC).isoformat(timespec="seconds")
    results = []
    for sid in scenarios:
        sc = registry()[sid]
        m = EndpointModel(model=model, base_url=base_url, temperature=temperature,
                                  max_tokens=max_tokens, api_key_env=api_key_env)
        s = open_session(sc, model=m, out_dir=out / sid if out else None)
        run_with_operator(s)
        card = score(sc.gold, record_of(s)).as_dict()
        results.append({"scenario": sid, "status": s.state.status.value,
                        "status_reason": s.state.status_reason, "score": card,
                        "model_calls": m.usage.calls, "retries": m.usage.retries,
                        "prompt_tokens": m.usage.prompt_tokens,
                        "completion_tokens": m.usage.completion_tokens,
                        "model_seconds": round(m.usage.seconds, 2)})
    report = {"kind": "live-model demonstration (not a release gate)", "model": model,
              "base_url": base_url, "temperature": temperature, "max_tokens": max_tokens,
              "started_utc": started, "operator": "simulated (scenario rules)",
              "results": results}
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "live.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (out / "live.md").write_text(markdown(report), encoding="utf-8")
    return report


def markdown(rep: dict[str, Any]) -> str:
    lines = [f"# Live-model run: `{rep['model']}`", "",
             f"{rep['kind']}. Started {rep['started_utc']}; temperature "
             f"{rep['temperature']}; max_tokens {rep['max_tokens']}; operator "
             f"{rep['operator']}.", "",
             "| Scenario | Status | " + " | ".join(COLS) + " | calls | tokens in/out |",
             "|" + "---|" * (len(COLS) + 4)]
    for r in rep["results"]:
        sc = r["score"]
        lines.append(f"| {r['scenario']} | {r['status']} | "
                     + " | ".join(str(sc[c]) for c in COLS)
                     + f" | {r['model_calls']} | {r['prompt_tokens']}/"
                       f"{r['completion_tokens']} |")
    return "\n".join(lines) + "\n"
