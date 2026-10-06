"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from triagewright import __version__
from triagewright.harness import open_session, run_with_operator
from triagewright.record import case_record
from triagewright.scenarios import registry


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="triagewright")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("scenarios", help="list built-in scenarios")
    run = sub.add_parser("run", help="run a scenario with the scripted model")
    run.add_argument("scenario")
    run.add_argument("--script", default="good")
    run.add_argument("--out", type=Path, help="directory for trace.jsonl and case.md")
    a = p.parse_args(argv)

    if a.cmd == "scenarios":
        for s in registry().values():
            print(f"{s.id}  {s.title}  (scripts: {', '.join(s.scripts)})")
        return 0
    if a.cmd == "run":
        sc = registry().get(a.scenario.upper())
        if sc is None:
            print(f"unknown scenario {a.scenario}", file=sys.stderr)
            return 2
        sess = open_session(sc, script=a.script, out_dir=a.out)
        run_with_operator(sess)
        record = case_record(sess.state, sess.trace.events)
        if a.out:
            (a.out / "case.md").write_text(record, encoding="utf-8")
            (a.out / "state.json").write_text(sess.state.model_dump_json(indent=2),
                                              encoding="utf-8")
        print(record)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
