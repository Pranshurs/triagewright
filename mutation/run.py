"""Run the mutation campaign on a scratch copy of the tree.

usage: python mutation/run.py [--only ID,ID] [--out mutation/results.json]

Each mutant: copy src+tests to a scratch dir, apply the edit, run pytest with that
copy first on the path (asserted), record KILLED / SURVIVED / INVALID. The baseline
(unmutated copy) must pass first or nothing is reported.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mutants import MUTANTS, Mutant  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _copy(dst: Path) -> None:
    for part in ("src", "tests", "pyproject.toml"):
        s = ROOT / part
        (shutil.copytree if s.is_dir() else shutil.copy2)(
            s, dst / part, **({"ignore": shutil.ignore_patterns("__pycache__")}
                              if s.is_dir() else {}))


def _pytest(tree: Path) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPYCACHEPREFIX": str(tree / ".pyc")}
    probe = subprocess.run(
        [sys.executable, "-c", "import triagewright;print(triagewright.__file__)"],
        env=env, capture_output=True, text=True, cwd=tree)
    loaded = probe.stdout.strip()
    if not loaded.startswith(str(tree)):
        raise SystemExit(f"wrong tree imported: {loaded}")
    p = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
                        "tests"], env=env, capture_output=True, text=True, cwd=tree)
    return p.returncode, p.stdout.strip().splitlines()[-1] if p.stdout.strip() else p.stderr[-300:]


def run(mutants: list[Mutant]) -> list[dict[str, str]]:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "baseline"
        base.mkdir()
        _copy(base)
        code, line = _pytest(base)
        if code != 0:
            raise SystemExit(f"baseline does not pass: {line}")
        print(f"baseline: {line}")
    out = []
    for m in mutants:
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / m.id
            tree.mkdir()
            _copy(tree)
            f = tree / m.file
            text = f.read_text(encoding="utf-8")
            n = text.count(m.find)
            if n != 1:
                res = {"id": m.id, "result": "INVALID", "detail": f"find occurs {n} times"}
            else:
                f.write_text(text.replace(m.find, m.replace), encoding="utf-8")
                t0 = time.monotonic()
                code, line = _pytest(tree)
                res = {"id": m.id, "result": "KILLED" if code != 0 else "SURVIVED",
                       "detail": line, "seconds": f"{time.monotonic() - t0:.1f}"}
        if res["result"] == "SURVIVED" and m.equivalent:
            res["result"] = "EQUIVALENT"
            res["proof"] = m.equivalent
        res.update({"boundary": m.boundary, "what": m.what, "file": m.file})
        print(f"{res['id']} {res['result']:8} {m.what} :: {res['detail']}")
        out.append(res)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    ms = [m for m in MUTANTS if not a.only or m.id in a.only.split(",")]
    res = run(ms)
    if a.out:
        a.out.write_text(json.dumps(res, indent=2) + "\n", encoding="utf-8")
    count = {k: sum(r["result"] == k for r in res)
             for k in ("KILLED", "EQUIVALENT", "SURVIVED", "INVALID")}
    bad = [r for r in res if r["result"] in ("SURVIVED", "INVALID")]
    print(f"\n{len(res)} mutants: {count['KILLED']} killed, {count['EQUIVALENT']} equivalent "
          f"(argued, not killed), {count['SURVIVED']} survived, {count['INVALID']} invalid; "
          f"kill rate over non-equivalent: {count['KILLED']}/{len(res) - count['EQUIVALENT']}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
