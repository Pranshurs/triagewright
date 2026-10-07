# Mutation campaign

**Summary.** An initial mutation pass exposed gaps in the test suite. After targeted
regression tests and a cold-review repair round, the v0.1.0-alpha campaign killed all
60 non-equivalent mutants; 2 additional mutants were demonstrated equivalent. With the
HubSpot connector the campaign grew to 75 mutants: 73 killed, the same 2 equivalent.
The full chronology, including a corrected intermediate result, follows.

Scope: five boundaries where a silent weakening would let bad behaviour through or
hide it. These are approval binding and operator authority, tenant-scope derivation,
unknown-outcome reconciliation and idempotency, the transport/service boundary, and
the independent scorer. The campaign does not cover formatting, rendering or
scenario prose. Mutants live in `mutants.py`, and `run.py` applies each one to a
scratch copy of the tree. The harness asserts that the scratch copy is the tree
actually imported, requires a passing baseline, and counts a mutant whose target
text is missing as INVALID, never as killed.

```bash
python mutation/run.py --out mutation/results.json
```

## Pass 1 (baseline 102 tests): 43 killed, 11 survived, 2 invalid of 56

`results-pass1.json` keeps this run unedited.

- **Invalid:** T05 and T08. Their find strings did not match the registry's line
  wrapping. They were retargeted to the same rule and both are killed.
- **Survived, closed with behavioural tests** (`tests/test_boundaries.py`):

| Mutant | Rule that had no test | Test added |
|--------|----------------------|------------|
| A03 | A decision must match the stored binding, not only the stored arguments | stored binding altered after render: refused, still pending |
| A06 | An approval naming another case is not executed | approval `case_id` altered: voided |
| A07 | Policy is re-checked at execution | payment re-homed to another account while pending: voided, no refund |
| A15 | Only rejections can be reopened | reopen of pending/executed raises; status unchanged |
| T06 | Unresolvable references fail closed | unknown account/ticket: denied, nothing observed |
| U02 | A repeatable write after a definite outcome gets a fresh key | retry, resync, retry: second attempt really runs |
| X04 | Transports cannot submit to awaiting or finished cases | service refuses; no effects |
| C08 | Scorer flags an unsettled write in a "resolved" case | rogue state: `unknown_closed` false |
| C12 | Scorer matches approvals by arguments | approved 48000, applied 4800: unapproved |

- **Survived, argued equivalent** (proofs in `mutants.py`, checked against the code):
  - A13: an observation at or before the cutoff is itself in `earlier`.
  - U01: the key counter only differs while an identical action is unknown, a state
    in which no path evaluates it.

## Pass 2 (baseline 111 tests): reported 54 killed, 2 equivalent; **one kill was spurious**

Pass 2 reported 54/54 over non-equivalent mutants. The pre-publication review showed
that this was not true. C13 (the scorer counts rejected approvals as approved) was
"killed" only by a nondeterministic test. That test searched an OTLP export for the
substring `480000` while the export contained 19-digit wall-clock timestamps, which
contain any digit run by chance. With that test deselected, C13 survived. Pass 2's
true result was therefore 53 killed, 2 equivalent and 1 survived. The pass-2 JSON was
overwritten by pass 3 and is not kept. Its C13 line read "1 failed, 109 passed".

## Pre-publication review and repair

One review pass, with one bounded repair (see `docs/REVIEW.md`). The repairs and the
tests that pin them:

- **B1:** the telemetry secret check excludes timestamps. A self-test shows an amount in
  an attribute is still found. Forcing timestamps that contain `480000` fails the old
  check and passes the new one. C13 now has a deterministic test (a rejected approval
  plus an effect applied anyway scores as unapproved).
- **B2:** `get_incident` no longer lists linked tickets across tenants. A class test
  requires that no global-scope tool returns another tenant's identifiers, and a guard
  test fails if a new unscoped tool is added without that check. S10's good arm now
  reads the incident with another tenant's ticket linked to it.
- **B3:** the scorer checks delivery and job references. A coverage test ties every id
  argument in the catalogue to the scorer's checked set.
- **Two non-blocking findings in the same boundaries, fixed in the same repair:**
  - A closed case expired none of its pending approvals, and a later operator approval
    executed. Approvals now expire when a case ends, and decisions on a closed case are
    refused.
  - Denials for a missing id and for another tenant's id differed. Model-facing
    feedback for every scope denial is now identical; the trace keeps the rule. An
    unknown email is now unresolved, not global. A class test compares the two denials
    for every scoped tool.
- **Reintroduction mutants** for each repair: A17, A18, T09, T10, T11, C15. T04 was
  retargeted to the repaired `_contact`. A new closed-case check briefly masked A01
  (re-deciding an approval): the old single-use tests re-decided on finished cases. A
  test now re-decides on a case that is still open.

## Pass 3 (baseline 124 tests): 60 killed, 2 equivalent, 0 survived, 0 invalid of 62

The kill rate over non-equivalent mutants is 60/60. `results.json` held this run for
v0.1.0-alpha (it now holds pass 4). Two
consecutive full runs gave identical verdicts for every mutant, and the suite passed
25/25 repeated runs.

## Pass 4 (baseline 196 tests): 73 killed, 2 equivalent, 0 survived, 0 invalid of 75

Run after the HubSpot connector, the observability changes and their review repair.
`results.json` holds this run.

- **New boundary, external write and OAuth (H01-H13).** Thirteen single mutants, each
  weakening one rule: reconciliation re-sends an external write; a 5xx, a lost response
  or an unreadable 2xx on a write is reported as a definite failure; two matching notes
  settle as one; an incomplete lookup concludes; the callback is accepted without a
  valid state; a state can be replayed or never expires; a grant missing scopes is
  stored; the marker can be derived without its secret; account links carry over to a
  different HubSpot account; an empty page with a cursor is followed. All killed.
- **Retargeted, same rule:** U03 and U04. Reconciliation moved into its own method, so
  their target lines changed; both are still killed.
- **A survivor, closed.** H13 first survived: the paging guard had two conditions and
  the test could only tell one apart. The redundant condition was removed and the fake
  made to return an empty page with a moving cursor.
- **A hanging mutant.** H13 reintroduces an endless loop, so its suite run never
  returns. The runner now stops a run after 120 seconds and counts that as a kill,
  saying so in the detail. H13 is the only mutant killed this way.
- **Equivalent:** A13 and U01, with the same proofs as before.
