"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from triagewright import __version__
from triagewright.evals.run import record_of, run_all
from triagewright.evals.scorer import score
from triagewright.harness import open_session, run_with_operator
from triagewright.record import case_record
from triagewright.scenarios import registry

COLS = ("resolution", "harmful_effects", "unauthorized_attempts", "approval_correct",
        "unknown_closed", "scope_intact", "ungrounded", "escalation", "diagnosis_correct")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="triagewright")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("scenarios", help="list built-in scenarios and their arms")
    run = sub.add_parser("run", help="run one scenario arm with the scripted model")
    run.add_argument("scenario")
    run.add_argument("--arm", default="good")
    run.add_argument("--out", type=Path, help="directory for trace.jsonl, state.json, case.md")
    ev = sub.add_parser("eval", help="run and score every scenario arm")
    ev.add_argument("--json", type=Path, help="write the score cards as JSON")
    a = p.parse_args(argv)

    if a.cmd == "scenarios":
        for s in registry().values():
            print(f"{s.id}  {s.title}\n     invariant: {s.invariant}\n"
                  f"     arms: {', '.join(s.arms)}")
        return 0
    if a.cmd == "run":
        sc = registry().get(a.scenario.upper())
        if sc is None or a.arm not in sc.arms:
            print(f"unknown scenario/arm {a.scenario}/{a.arm}", file=sys.stderr)
            return 2
        sess = open_session(sc, arm=a.arm, out_dir=a.out)
        run_with_operator(sess)
        card = score(sc.gold, record_of(sess))
        record = case_record(sess.state, sess.trace.events)
        if a.out:
            (a.out / "case.md").write_text(record, encoding="utf-8")
            (a.out / "score.json").write_text(json.dumps(card.as_dict(), indent=2),
                                              encoding="utf-8")
        print(record)
        print("## Score\n")
        for k in COLS:
            print(f"- {k}: {getattr(card, k)}")
        for n in card.notes:
            print(f"  - {n}")
        return 0
    if a.cmd == "eval":
        results = run_all()
        print(f"{'scenario/arm':32} {'status':16} " + " ".join(c[:10].rjust(10) for c in COLS)
              + "  expected")
        bad = 0
        for card, misses in results:
            bad += bool(misses)
            row = " ".join(str(getattr(card, c))[:10].rjust(10) for c in COLS)
            print(f"{card.scenario + '/' + card.arm:32} {card.status:16} {row}  "
                  f"{'ok' if not misses else 'MISMATCH ' + '; '.join(misses)}")
        if a.json:
            a.json.write_text(json.dumps([c.as_dict() for c, _ in results], indent=2),
                              encoding="utf-8")
        good = [c for c, _ in results if c.arm == "good"]
        print(f"\n{len(results)} arms, {bad} mismatched expectations; good arms resolved "
              f"{sum(c.resolution for c in good)}/{len(good)}; harmful effects in good arms: "
              f"{sum(c.harmful_effects for c in good)}")
        return 1 if bad else 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
