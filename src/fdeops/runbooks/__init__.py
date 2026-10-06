"""Internal runbooks, shipped as markdown, with a small keyword search."""

from __future__ import annotations

import re
from functools import cache
from importlib import resources
from typing import Any

_WORD = re.compile(r"[a-z0-9]+")


@cache
def _load() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for f in sorted(resources.files(__name__).iterdir(), key=lambda p: p.name):
        if f.name.endswith(".md"):
            text = f.read_text(encoding="utf-8")
            title = text.splitlines()[0].lstrip("# ").strip()
            out[f.name[:-3]] = {"id": f.name[:-3], "title": title, "body": text}
    return out


def get(runbook_id: str) -> dict[str, str] | None:
    return _load().get(runbook_id)


def search(query: str, limit: int = 3) -> list[dict[str, Any]]:
    terms = set(_WORD.findall(query.lower()))
    scored = []
    for rb in _load().values():
        words = _WORD.findall(rb["body"].lower())
        title = set(_WORD.findall(rb["title"].lower()))
        score = sum(words.count(t) for t in terms) + 5 * len(terms & title)
        if score:
            scored.append((score, rb["id"], rb["title"]))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [{"runbook_id": i, "title": t, "score": s} for s, i, t in scored[:limit]]
