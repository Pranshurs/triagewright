"""Canonical case trace: an ordered, append-only list of events.

Events carry no wall-clock time, so the same case run twice produces the same trace.
Timing lives in run metrics, and the OpenTelemetry export is derived from this trace.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class Trace:
    def __init__(self, path: str | Path | None = None) -> None:
        self.events: list[dict[str, Any]] = []
        self.times: dict[int, int] = {}  # seq -> wall clock (ns); not part of the trace
        self._path = Path(path) if path else None
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._path.exists():
                for line in self._path.read_text(encoding="utf-8").splitlines():
                    self.events.append(json.loads(line))

    def emit(self, type: str, **fields: Any) -> dict[str, Any]:
        event = {"seq": len(self.events) + 1, "type": type, **fields}
        event = dict(json.loads(json.dumps(event, default=str)))  # freeze as plain JSON
        self.events.append(event)
        self.times[event["seq"]] = time.time_ns()
        if self._path:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
        return event

    def of(self, type: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["type"] == type]
