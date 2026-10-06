"""Render a case record an operator or auditor can read."""

from __future__ import annotations

import json
from typing import Any

from triagewright.state import CaseState


def _args(a: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v}" for k, v in a.items())


def case_record(state: CaseState, events: list[dict[str, Any]]) -> str:
    c, r = state.case, state.resolution
    out = [f"# Case {c.id}", "",
           f"- Account: `{c.account_id}`  ·  Ticket: `{c.ticket_id}`"
           f"  ·  Contact: {c.contact_email}",
           f"- Status: **{state.status.value}** ({state.status_reason})",
           f"- Steps: {state.step}  ·  Tool calls: {state.tool_calls}", "",
           "## Request", "", f"> {c.request}", ""]
    if r:
        out += ["## Diagnosis", "", f"**{r.diagnosis}**: {r.summary}", ""]
        for f in r.findings:
            mark = "grounded" if f.grounded else "UNGROUNDED"
            out.append(f"- {f.claim} [{', '.join(f.evidence)}; {mark}]")
        if r.uncertainty:
            out += ["", f"Uncertainty: {r.uncertainty}"]
        out.append("")
    out += ["## Actions (as recorded by the runner)", ""]
    for a in state.actions:
        extra = " · replayed from idempotency store" if a.replayed else ""
        appr = f" · approval {a.approval_id}" if a.approval_id else ""
        rec = f" · reconciled after {a.reconcile_attempts} check(s)" if a.reconcile_attempts else ""
        out.append(f"- `{a.id}` {a.tool}({_args(a.args)}) → **{a.status.value}**{appr}{rec}{extra}")
    if not state.actions:
        out.append("- none")
    out += ["", "## Approvals", ""]
    for ap in state.approvals:
        out.append(f"- `{ap.id}` {ap.tool}({_args(ap.args)}) → **{ap.status.value}**"
                   f" by {ap.decided_by or '-'}; evidence {', '.join(ap.evidence) or '-'}"
                   f"; rationale: {ap.rationale}")
    if not state.approvals:
        out.append("- none")
    out += ["", "## Timeline", ""]
    for e in events:
        out.append(f"{e['seq']:>3}. {_line(e)}")
    return "\n".join(out) + "\n"


def _line(e: dict[str, Any]) -> str:
    t = e["type"]
    if t == "decision":
        d = e["decision"]
        what = d.get("tool") or d.get("kind")
        return f"decide [{e['step']}] {what} — {d.get('rationale') or ''}".rstrip(" —")
    if t == "policy":
        return f"policy {e['tool']}: {e['verdict']} ({e['rule']}) {e['reason']}".rstrip()
    if t == "tool_call":
        err = f" {e['error_code']}" if e.get("error_code") else ""
        return f"call {e['tool']} → {e['outcome']}{err}"
    if t == "observation":
        return f"observe {e['obs_id']} from {e['tool']} ok={e['ok']}"
    if t == "reconcile":
        return (f"reconcile {e['action_id']} attempt {e['attempt']} (same key) → "
                f"{e['outcome']} replayed={e['replayed']}")
    if t == "action":
        return f"action {e['action_id']} → {e['status']}"
    if t in ("approval_requested", "approval_decided", "approval_void"):
        rest = {k: v for k, v in e.items() if k not in ("seq", "type", "binding", "args")}
        return f"{t.replace('_', ' ')} {json.dumps(rest)}"
    if t == "case_status":
        return f"status → {e['status']} ({e['reason']})"
    rest = {k: v for k, v in e.items() if k not in ("seq", "type")}
    return f"{t} {json.dumps(rest, default=str)[:160]}"
