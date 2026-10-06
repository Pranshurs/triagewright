"""SQLite-backed simulated enterprise environment.

One `Environment` is one company's business systems for one case run. It is
loaded from a fixture (table name -> rows), has a fixed logical clock so runs are
reproducible, and mints ids deterministically.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

Row = dict[str, Any]
Fixture = Mapping[str, Sequence[Mapping[str, Any]]]

# Insertion order respects foreign keys.
TABLES = (
    "accounts",
    "contacts",
    "plans",
    "subscriptions",
    "entitlements",
    "invoices",
    "payment_events",
    "services",
    "workspaces",
    "provisioning_jobs",
    "incidents",
    "tickets",
    "ticket_notes",
    "webhook_endpoints",
    "webhook_deliveries",
    "escalations",
)

JSON_COLUMNS = {("plans", "features"), ("incidents", "service_ids")}


def _schema() -> str:
    return resources.files("fdeops.env").joinpath("schema.sql").read_text(encoding="utf-8")


class Environment:
    def __init__(self, fixture: Fixture, now: str, path: str | Path = ":memory:") -> None:
        unknown = set(fixture) - set(TABLES)
        if unknown:
            raise ValueError(f"unknown fixture tables: {sorted(unknown)}")
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_schema())
        self._clock = datetime.fromisoformat(now)
        self._counters: dict[str, int] = {}
        with self.transaction():
            for table in TABLES:
                for row in fixture.get(table, ()):
                    self.insert(table, row)

    # -- clock and ids -------------------------------------------------------------

    def now(self) -> str:
        return self._clock.isoformat(timespec="seconds")

    def tick(self, minutes: int = 1) -> None:
        self._clock += timedelta(minutes=minutes)

    def new_id(self, prefix: str) -> str:
        n = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = n
        return f"{prefix}_new{n:03d}"

    # -- data access ---------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[Row]:
        cur = self._conn.execute(sql, tuple(params))
        table = _table_of(sql)
        return [_decode(table, dict(r)) for r in cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return self._conn.execute(sql, tuple(params)).rowcount

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        if table not in TABLES and table != "idempotency":
            raise ValueError(f"unknown table {table}")
        cols = list(row)
        values = [
            json.dumps(row[c]) if (table, c) in JSON_COLUMNS else row[c] for c in cols
        ]
        placeholders = ", ".join("?" for _ in cols)
        self._conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", values
        )

    def snapshot(self) -> dict[str, list[Row]]:
        """Full state, ordered, for scoring and diffing."""
        return {t: self.query(f"SELECT * FROM {t} ORDER BY rowid") for t in TABLES}

    def close(self) -> None:
        self._conn.close()


def _table_of(sql: str) -> str | None:
    words = sql.replace("\n", " ").split()
    for i, w in enumerate(words[:-1]):
        if w.upper() == "FROM":
            return words[i + 1].strip(",;")
    return None


def _decode(table: str | None, row: Row) -> Row:
    for t, c in JSON_COLUMNS:
        if t == table and isinstance(row.get(c), str):
            row[c] = json.loads(row[c])
    return row
