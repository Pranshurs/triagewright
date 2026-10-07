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

# Review of the HubSpot connector and observability changes

One independent review pass was run on commit `a0dc7d5` (the changes since
v0.1.0-alpha), under the same protocol: a reviewer without context, read-only, a
PASS/FAIL verdict, a reproduction for every blocker, one bounded repair, no second
review.

## Verdict: FAIL, with one blocker (reproduced independently before repair)

| ID | Finding | Status |
|----|---------|--------|
| B1 | Account links (HubSpot company id to account) were not tied to the HubSpot account they were declared for. After disconnecting and connecting a different HubSpot account, a company there with the same id was readable, and writable with approval, as the linked account | Fixed: connecting a different account discards the links, a link cannot be declared before an account is connected, and a grant that names no account is refused. Test `test_links_do_not_carry_over_to_a_different_hubspot_account`; reintroduction mutant H12 |

## Non-blocking findings

| # | Finding | Disposition |
|---|---------|-------------|
| 1 | A grant missing required scopes was not stored but stayed live at HubSpot | Fixed: it is revoked |
| 2 | Association paging could loop forever on an empty page that still offered a cursor, while holding the service lock | Fixed: cut off, and the lookup concludes nothing; mutant H13 |
| 3 | The connector doc said the agent has no access to `recheck`; over HTTP it is an operator route like approvals | Doc corrected; a test assertion that did not show what it claimed was removed |
| 4 | Connect and callback need no authentication; pending authorizations are capped, so a flood can cancel one in flight | Accepted: follows from the documented no-authentication model of the API |
| 5 | The connection store is locked in-process only; CLI and server could interleave writes | Accepted and documented as a limit |
| 6 | Ids with a leading zero were accepted and could name one record two ways | Fixed: rejected |
| 7 | "4xx means no effect" assumes HubSpot never applies a write and then answers 4xx | Accepted; the consequence is documented |
| 8 | A successful recheck does not reopen a case that ended `needs_attention` | Accepted and documented |
| 9 | Listing cases rewrites each case's state file | Accepted: harmless in single-process use |
| 10 | HubSpot calls run under the case service lock | Accepted and documented as a limit |
| 11 | No mutants for HubSpot tenant scope or `recheck` | Partly addressed by H12; the scope rule is covered by behavioural tests for mixed, unlinked and foreign records |

## Confirmed by the review without change

- No path was found that sends an external write twice, and none that turns an
  unknown external outcome into failed.
- State handling, token custody, error reduction and the access-log redaction held.
  The marker, tokens and secrets were not found in any output searched.
- Scope is re-derived at approval time, and a scope lookup error voids the approval.
- Incremental span export produced no duplicate ids and no orphaned parents, and
  metric labels were limited to tool names, outcomes and statuses.
- Simulated-tool behaviour and the frozen tests were unchanged.

The review had no network access: real HubSpot behaviour is covered only by the manual
check recorded in `HUBSPOT.md`.
