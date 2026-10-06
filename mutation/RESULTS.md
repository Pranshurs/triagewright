# Mutation campaign (P5)

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

## Pass 2 (baseline 111 tests): 54 killed, 2 equivalent, 0 survived, 0 invalid of 56

The kill rate over non-equivalent mutants is 54/54. `results.json` holds this run. The
two equivalent mutants are reported separately and are not counted as kills.
