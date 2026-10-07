"""Bounded mutation campaign over the five high-risk boundaries.

Each mutant weakens one rule. `find` must occur exactly once in `file`; a mutant
whose find string is missing is INVALID and fails the campaign (it is a rule that
lost its only attacker), it is never counted as killed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mutant:
    id: str
    boundary: str
    file: str
    find: str
    replace: str
    what: str
    equivalent: str = ""   # proof that no input distinguishes this mutant, if claimed


R = "src/triagewright/runner.py"
P = "src/triagewright/policy.py"
S = "src/triagewright/state.py"
C = "src/triagewright/tools/catalog.py"
G = "src/triagewright/tools/base.py"
SV = "src/triagewright/service.py"
MC = "src/triagewright/mcp_server.py"
API = "src/triagewright/api.py"
SC = "src/triagewright/evals/scorer.py"

APPROVAL = "approval binding / operator authority"
SCOPE = "tenant-scope derivation"
UNKNOWN = "unknown outcome / idempotency"
TRANSPORT = "transport / service bypass"
SCORER = "independent scorer"
EXTERNAL = "external write / OAuth"
HC = "src/triagewright/hubspot/client.py"
HO = "src/triagewright/hubspot/oauth.py"

MUTANTS = [
    # -- approval binding / operator authority -------------------------------------
    Mutant("A01", APPROVAL, R, "        if ap.status is not ApprovalStatus.PENDING:\n            raise StaleApproval",
           "        if False:\n            raise StaleApproval", "decide a non-pending approval"),
    Mutant("A02", APPROVAL, R, "if expected_binding != ap.binding or expected_binding != current:",
           "if expected_binding != ap.binding:", "ignore drift of the stored request"),
    Mutant("A03", APPROVAL, R, "if expected_binding != ap.binding or expected_binding != current:",
           "if expected_binding != current:", "ignore drift of the stored binding"),
    Mutant("A04", APPROVAL, R, "        if approve:\n            self._execute_approved(ap)",
           "        if True:\n            self._execute_approved(ap)", "execute rejected actions"),
    Mutant("A05", APPROVAL, R, "                ap.binding != expected:", "                False:",
           "execution skips binding re-derivation"),
    Mutant("A06", APPROVAL, R, "        if ap.case_id != s.case.id or ap.account_id",
           "        if False or ap.account_id", "execution skips case identity check"),
    Mutant("A07", APPROVAL, R, "        if verdict.verdict is Verdict.DENY:\n            self.trace.emit(\"approval_void\"",
           "        if False:\n            self.trace.emit(\"approval_void\"",
           "execution skips policy re-check"),
    Mutant("A08", APPROVAL, S, 'material = "\\n".join([case_id, account_id, tool, canonical_args(args)])',
           'material = "\\n".join([case_id, account_id, tool])', "binding omits arguments"),
    Mutant("A09", APPROVAL, S, 'material = "\\n".join([case_id, account_id, tool, canonical_args(args)])',
           'material = "\\n".join([account_id, tool, canonical_args(args)])', "binding omits case"),
    Mutant("A10", APPROVAL, R, "            approval.status = ApprovalStatus.EXECUTED\n",
           "            pass\n", "approval not consumed at dispatch"),
    Mutant("A11", APPROVAL, R, "            if ap.status is ApprovalStatus.PENDING:\n                return f\"already",
           "            if False:\n                return f\"already", "duplicate pending requests"),
    Mutant("A12", APPROVAL, R, "if ap.status is ApprovalStatus.REJECTED and not self._new_evidence(ap, evidence):",
           "if False:", "re-propose rejected actions freely"),
    Mutant("A13", APPROVAL, R, "            if o is None or o.step <= cutoff:", "            if o is None:",
           "pre-rejection evidence counts as new",
           equivalent="An observation with step <= cutoff is itself a member of `earlier` "
                      "(same tool, same args, step <= cutoff), so `o.result not in earlier` "
                      "is False for it. Removing the step test changes no outcome."),
    Mutant("A14", APPROVAL, R, "            if o.ok and o.result not in earlier:", "            if o.ok:",
           "re-reading unchanged data counts as new"),
    Mutant("A15", APPROVAL, R, "if ap is None or ap.status is not ApprovalStatus.REJECTED:",
           "if ap is None:", "reopen any approval"),
    Mutant("A16", APPROVAL, R, "        if verdict.verdict is Verdict.NEEDS_APPROVAL:\n            return self._request_approval",
           "        if False:\n            return self._request_approval",
           "consequential actions run without approval"),
    Mutant("A17", APPROVAL, R, "        if status.terminal:\n            # A closed case carries no write authority",
           "        if False:\n            # A closed case carries no write authority",
           "pending approvals survive the case closing (review repair, reintroduced)"),
    Mutant("A18", APPROVAL, R, "        if self.state.status.terminal:\n            raise StaleApproval",
           "        if False:\n            raise StaleApproval",
           "operator may decide on a closed case (review repair, reintroduced)"),
    # -- tenant scope ---------------------------------------------------------------
    Mutant("T01", SCOPE, P, "    if scope is not None and scope != case.account_id:", "    if False:",
           "no cross-account check"),
    Mutant("T02", SCOPE, C, "      get_ticket, _ticket)", "      get_ticket, _global)",
           "get_ticket unscoped"),
    Mutant("T03", SCOPE, C, "      issue_refund, _payment)", "      issue_refund, _global)",
           "refund target unscoped"),
    Mutant("T04", SCOPE, C, '    return str(_need(row, "contact")["account_id"])', "    return None",
           "contact lookup unscoped"),
    Mutant("T05", SCOPE, C, "NoteIn, add_internal_note, _ticket)", "NoteIn, add_internal_note, _global)",
           "internal note unscoped"),
    Mutant("T06", SCOPE, P, '        return _deny("scope.unresolved", e.message)', "        scope = None",
           "unresolvable reference treated as global"),
    Mutant("T07", SCOPE, C, '"SELECT * FROM payment_events WHERE account_id=? ORDER BY created_at, id"',
           '"SELECT * FROM payment_events WHERE 1=1 OR account_id=? ORDER BY created_at, id"',
           "payment listing leaks other tenants"),
    Mutant("T08", SCOPE, C, "AccountIn, list_invoices,\n      _account)", "AccountIn, list_invoices,\n      _global)",
           "invoice listing unscoped"),
    Mutant("T09", SCOPE, C, '    return {"incident": inc}',
           '    linked = env.query("SELECT id FROM tickets WHERE incident_id=? ORDER BY id", [a.incident_id])\n    return {"incident": inc, "linked_tickets": [t["id"] for t in linked]}',
           "get_incident lists every tenant's linked tickets (review B2, reintroduced)"),
    Mutant("T10", SCOPE, R, '                return f"denied by policy (scope): {tool.name} is not available for this case"',
           '                return f"denied by policy ({verdict.rule}): {verdict.reason}"',
           "scope denials reveal whether an id exists (review nb1, reintroduced)"),
    Mutant("T11", SCOPE, C, '    return str(_need(row, "contact")["account_id"])',
           '    return None if row is None else str(row["account_id"])',
           "unknown email treated as global (review nb1 path, reintroduced)"),
    # -- unknown outcome / idempotency -----------------------------------------------
    Mutant("U01", UNKNOWN, R, "                      and a.status is not ActionStatus.UNKNOWN)",
           "                      )", "unknown attempts advance the key",
           equivalent="The count only differs while an identical action is UNKNOWN. Every "
                      "path that creates an action passes the write guard (U06) or executes "
                      "an approval, and an approval for that binding cannot be requested "
                      "while the identical action is unknown (same guard). Settlement reuses "
                      "the stored key, not _key(). So _key() is never evaluated in the "
                      "differing state."),
    Mutant("U02", UNKNOWN, R, 'material = f"{self.state.case.id}\\n{tool}\\n{canonical_args(args)}\\n{settled}"',
           'material = f"{self.state.case.id}\\n{tool}\\n{canonical_args(args)}"',
           "key ignores settled attempts"),
    Mutant("U03", UNKNOWN, R, "        r = self.gw.reconcile(rec.tool, rec.args, rec.idempotency_key)\n        self.trace.emit(\"reconcile\"",
           "        r = self.gw.reconcile(rec.tool, rec.args, rec.idempotency_key + str(rec.reconcile_attempts))\n        self.trace.emit(\"reconcile\"",
           "reconcile mints a fresh key"),
    Mutant("U04", UNKNOWN, R, "        if r.outcome is Outcome.ERROR and r.retryable:\n            return",
           "        if False:\n            return", "transient reconcile error settles as failed"),
    Mutant("U05", UNKNOWN, R, "RECONCILE_ATTEMPTS = 3", "RECONCILE_ATTEMPTS = 0", "no reconciliation"),
    Mutant("U06", UNKNOWN, R, "        if any(a.status is ActionStatus.UNKNOWN for a in same):", "        if False:",
           "repeat a write whose outcome is unknown"),
    Mutant("U07", UNKNOWN, R, "        if tool not in REPEATABLE and any(a.status is ActionStatus.SUCCEEDED for a in same):",
           "        if False:", "repeat a succeeded non-repeatable write"),
    Mutant("U08", UNKNOWN, R, "if rec.status is ActionStatus.UNKNOWN and rec.observation_id is None:", "if False:",
           "no settlement after restart"),
    Mutant("U09", UNKNOWN, R, "        self._checkpoint(s)\n        approval_id = rec.approval_id",
           "        approval_id = rec.approval_id", "no checkpoint before dispatch"),
    Mutant("U10", UNKNOWN, G, "            if prior is not None:", "            if False:",
           "upstream ignores idempotency keys"),
    Mutant("U11", UNKNOWN, G, "            Outcome.UNKNOWN if tool.effect.writes else Outcome.ERROR,\n            error_code=\"TIMEOUT\"",
           "            Outcome.ERROR,\n            error_code=\"TIMEOUT\"", "write timeout reported as failure"),
    Mutant("U12", UNKNOWN, R, '            rec.status = ActionStatus.UNKNOWN\n            rec.detail = f"{r.error_code}: outcome unknown"',
           '            rec.status = ActionStatus.FAILED\n            rec.detail = f"{r.error_code}: outcome unknown"',
           "runner treats unknown as failed"),
    Mutant("U13", UNKNOWN, R, "        elif s.unknown_actions():", "        elif False:",
           "case may finish over an unknown write"),
    # -- transport / service bypass ---------------------------------------------------
    Mutant("X01", TRANSPORT, SV, "expected_binding=binding)", "expected_binding=None)",
           "service drops the shown binding"),
    Mutant("X02", TRANSPORT, MC, "        result = service.submit(case_id, decision)",
           "        if isinstance(decision, UseTool):\n"
           "            r = service.session(case_id).runner.gw.invoke(decision.tool, decision.args, 'mcp')\n"
           "            result = {'feedback': 'ok' if r.outcome.value == 'ok' else 'rejected'}\n"
           "        else:\n            result = service.submit(case_id, decision)",
           "MCP calls the gateway directly"),
    Mutant("X03", TRANSPORT, SV, '            case = sc.case.model_copy(update={"id": case_id})',
           "            case = sc.case", "cases share one identity"),
    Mutant("X04", TRANSPORT, SV, "            feedback = s.runner.submit(decision)",
           "            s.runner.start()\n            s.runner._apply(decision)\n"
           "            feedback = s.runner._feedback", "service skips the accepting check"),
    Mutant("X05", TRANSPORT, API, "class OperatorDecision(BaseModel):\n    model_config = ConfigDict(extra=\"forbid\")",
           "class OperatorDecision(BaseModel):\n    model_config = ConfigDict(extra=\"ignore\")",
           "operator decision accepts extra fields"),
    # -- independent scorer -------------------------------------------------------------
    Mutant("C01", SCORER, SC, "            if f.matches(tool, args):", "            if False:",
           "ignore forbidden effects"),
    Mutant("C02", SCORER, SC, "        if tool in gold.consequential:", "        if False:",
           "skip approval-of-effect check"),
    Mutant("C03", SCORER, SC, '        if st == "failed" and key in applied_keys:', "        if False:",
           "trust runner failure claims"),
    Mutant("C04", SCORER, SC, "        if not pred(run.snapshot):", "        if False:",
           "ignore business predicates"),
    Mutant("C05", SCORER, SC, "    if seen:", "    if False:", "ignore foreign identifiers"),
    Mutant("C06", SCORER, SC, "                    and owners[str(v)] != run.account_id:", "                    and False:",
           "ignore effects on other accounts"),
    Mutant("C07", SCORER, SC, "        if n > 1 and t not in gold.repeatable:", "        if False:",
           "ignore duplicate effects"),
    Mutant("C08", SCORER, SC, '        if st == "unknown" and run.status != "needs_attention":', "        if False:",
           "accept unknown writes in any terminal state"),
    Mutant("C09", SCORER, SC, "        if not req.min <= n <= req.max:", "        if False:",
           "ignore required effects"),
    Mutant("C10", SCORER, SC, "    card.resolution = (run.status in gold.terminal and not unmet and card.harmful_effects == 0",
           "    card.resolution = (run.status in gold.terminal or not unmet and card.harmful_effects == 0",
           "trust the runner's terminal status"),
    Mutant("C11", SCORER, SC, "        card.ungrounded += int(bad)", "        card.ungrounded += 0",
           "skip grounding re-check"),
    Mutant("C12", SCORER, SC, 'ok = any(r["tool"] == tool and r["args"] == args and aid in approved',
           'ok = any(r["tool"] == tool and aid in approved', "approval matched without arguments"),
    Mutant("C13", SCORER, SC, 'if e["type"] == "approval_decided" and e["approved"]}',
           'if e["type"] == "approval_decided"}', "rejected approvals count as approved"),
    Mutant("C14", SCORER, SC, "        if st == \"succeeded\" and key not in applied_keys:", "        if False:",
           "trust runner success claims"),
    Mutant("C15", SCORER, SC, 'OWNED_ARGS = frozenset({a for _k, a in _OWNED.values()} | set(_OWNED_VIA_PARENT.values()))',
           'OWNED_ARGS = frozenset({a for _k, a in _OWNED.values()})',
           "scorer ignores delivery/job references (review B3, reintroduced)"),
    Mutant("H01", EXTERNAL, G, "        if tool is None or tool.external is None:\n            return self.invoke(name, args, idempotency_key)",
           "        if True:\n            return self.invoke(name, args, idempotency_key)",
           "reconcile re-sends an external write"),
    Mutant("H02", EXTERNAL, HC, "            if write:  # it may have been applied before the failure",
           "            if False:", "5xx on a write is reported as a definite failure"),
    Mutant("H03", EXTERNAL, HC, "                if write:\n                    raise OutcomeUnknown(\"RESPONSE_LOST\"",
           "                if False:\n                    raise OutcomeUnknown(\"RESPONSE_LOST\"",
           "a lost response on a write is reported as a definite failure"),
    Mutant("H04", EXTERNAL, HC, "        if len(matches) > 1:", "        if False:",
           "two matching notes settle as one success"),
    Mutant("H05", EXTERNAL, HC, "        if not complete:", "        if False:",
           "an incomplete lookup concludes"),
    Mutant("H06", EXTERNAL, HO, "        if not state or not self._take(state):", "        if False:",
           "callback accepted without a valid state"),
    Mutant("H07", EXTERNAL, HO, "            return self._pending.pop(match) > self._clock()",
           "            return self._pending[match] > self._clock()", "state can be replayed"),
    Mutant("H08", EXTERNAL, HO, "            return self._pending.pop(match) > self._clock()",
           "            return self._pending.pop(match) > 0", "state never expires"),
    Mutant("H09", EXTERNAL, HO, "        if missing:  # nothing is stored", "        if False:  # nothing is stored",
           "a grant missing required scopes is stored"),
    Mutant("H10", EXTERNAL, HO, "        secret = self._load().marker_secret.encode()", "        secret = b\"\"",
           "the upstream marker can be derived without the secret"),
    Mutant("H11", EXTERNAL, HC, "            if write:  # accepted, but we cannot read what was created",
           "            if False:", "unreadable 2xx on a write is reported as a definite failure"),
    Mutant("H12", EXTERNAL, HO, "            if st.hub_id != hub:", "            if False:",
           "account links carry over to a different HubSpot account"),
    Mutant("H13", EXTERNAL, HC, "            if len(ids) >= cap or not results:",
           "            if len(ids) >= cap:", "an empty page with a cursor is followed"),
]
