# Pre-publication review

One independent review pass was run on commit `e64a86b`, followed by one bounded
repair. The protocol was fixed before the review started:

- the reviewer started without context and worked read-only on a scratch copy;
- the verdict is PASS or FAIL, and every blocker needs a reproduction;
- the author fixes real blockers once and re-runs the affected gates;
- there is no second review unless the owner decides otherwise.

## Verdict: FAIL, with three blockers (all reproduced independently before repair)

| ID | Finding | Status |
|----|---------|--------|
| B1 | A telemetry test was nondeterministic (substring search over wall-clock timestamps). The reported "54/54" mutation result was false: C13's kill came from that test | Fixed; result corrected in `mutation/RESULTS.md` |
| B2 | `get_incident` returned other tenants' ticket ids linked to a shared incident | Fixed; class test over all global tools |
| B3 | The scorer did not check webhook-delivery references, so a cross-tenant redelivery went unflagged | Fixed; coverage test over all id arguments |

## Non-blocking findings

| # | Finding | Disposition |
|---|---------|-------------|
| 1 | Denials distinguished missing ids from other tenants' ids | Fixed (touches the repaired scope boundary) |
| 2 | `Resolution.outcome` is a free string | Accepted: unknown values fall back to `needs_attention` |
| 3 | `reopen` works on a rejected approval of a closed case | Accepted: it cannot execute anything, because decisions on closed cases are refused |
| 4 | A pending approval outlived a case ending in `needs_info`, and a later approval executed | Fixed (touches the repaired approval boundary) |
| 5 | Release gate 5 (wheel install, Docker smoke) has no script | Accepted for now; done by hand and recorded |

## Confirmed by the review without change

- The HTTP attack probe found no bypass. Denied tools are refused, a refund only
  requests approval, wrong or repeated decisions get 409, there is no reopen route,
  traversal gets 404 and extra fields get 422.
- No bypass was found in approval binding and single use, key persistence before
  dispatch, settlement after restart, the unknown-write guard, or the policy re-check
  at approval.
- Hygiene was clean.

## After repair

124 tests (25/25 repeated runs pass), `triagewright eval` 24/24, mutation pass 3: 60/60
non-equivalent killed with identical verdicts across two runs, ruff and mypy clean.
