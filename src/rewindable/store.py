"""SQLite journal: the single source of truth for runs, steps and signals."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import RunNotFound

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    workflow      TEXT NOT NULL,
    input         TEXT,
    status        TEXT NOT NULL,
    output        TEXT,
    error         TEXT,
    budget        TEXT,
    parent_run_id TEXT,
    forked_at     TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    key           TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    output        TEXT,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL NOT NULL DEFAULT 0,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    UNIQUE (run_id, key)
);
CREATE TABLE IF NOT EXISTS signals (
    run_id       TEXT NOT NULL,
    key          TEXT NOT NULL,
    prompt       TEXT,
    payload      TEXT,
    requested_at REAL,
    delivered_at REAL,
    PRIMARY KEY (run_id, key)
);
CREATE INDEX IF NOT EXISTS steps_by_run ON steps (run_id, seq);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("REWINDABLE_DB", ".rewindable/runs.db"))


def _encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if hasattr(value, "model_dump"):  # pydantic models
        return value.model_dump(mode="json")
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(
        f"step results must be JSON-serializable, got {type(value).__name__}; "
        "return a dict, list, str, number, dataclass or pydantic model"
    )


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_encode)


def loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


@dataclass(frozen=True)
class Run:
    id: str
    workflow: str
    input: Any
    status: str  # running | waiting | completed | failed
    output: Any
    error: str | None
    budget: dict | None
    parent_run_id: str | None
    forked_at: str | None
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class StepRecord:
    seq: int
    run_id: str
    key: str
    name: str
    kind: str
    status: str  # running | completed | failed
    output: Any
    error: str | None
    attempts: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    started_at: float
    finished_at: float | None

    @property
    def depth(self) -> int:
        # "3" is top level, "3.0.1" is the first step of branch 0 of parallel step 3.
        return self.key.count(".") // 2

    @property
    def duration(self) -> float | None:
        return None if self.finished_at is None else self.finished_at - self.started_at


@dataclass(frozen=True)
class Signal:
    run_id: str
    key: str
    prompt: str | None
    payload: Any
    requested_at: float | None
    delivered_at: float | None

    @property
    def pending(self) -> bool:
        return self.delivered_at is None


class Store:
    """Thread-safe access to the journal. Safe to share between processes (WAL mode)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _read(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- runs ---------------------------------------------------------------

    def create_run(
        self,
        workflow: str,
        input: Any,
        *,
        run_id: str | None = None,
        budget: dict | None = None,
        parent_run_id: str | None = None,
        forked_at: str | None = None,
    ) -> Run:
        run_id = run_id or "run_" + uuid.uuid4().hex[:12]
        now = time.time()
        self._write(
            "INSERT INTO runs (id, workflow, input, status, budget, parent_run_id, forked_at,"
            " created_at, updated_at) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?)",
            (run_id, workflow, dumps(input), dumps(budget), parent_run_id, forked_at, now, now),
        )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        rows = self._read("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not rows:
            matches = self._read("SELECT * FROM runs WHERE id LIKE ?", (run_id + "%",))
            if len(matches) != 1:
                raise RunNotFound(run_id)
            rows = matches
        return _run(rows[0])

    def list_runs(self, *, status: str | None = None, limit: int = 50) -> list[Run]:
        if status:
            rows = self._read(
                "SELECT * FROM runs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self._read("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [_run(r) for r in rows]

    def set_status(self, run_id: str, status: str, *, output: Any = None, error: str | None = None) -> None:
        self._write(
            "UPDATE runs SET status = ?, output = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, dumps(output) if status == "completed" else None, error, time.time(), run_id),
        )

    def set_budget(self, run_id: str, budget: dict | None) -> None:
        self._write("UPDATE runs SET budget = ?, updated_at = ? WHERE id = ?", (dumps(budget), time.time(), run_id))

    # -- steps --------------------------------------------------------------

    def get_step(self, run_id: str, key: str) -> StepRecord | None:
        rows = self._read("SELECT * FROM steps WHERE run_id = ? AND key = ?", (run_id, key))
        return _step(rows[0]) if rows else None

    def steps(self, run_id: str) -> list[StepRecord]:
        return [_step(r) for r in self._read("SELECT * FROM steps WHERE run_id = ? ORDER BY seq", (run_id,))]

    def begin_step(self, run_id: str, key: str, name: str, kind: str) -> None:
        # A row left over from a crashed or failed attempt is restarted in place but
        # moved to the end of the timeline, so `seq` always reflects start order.
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM steps WHERE run_id = ? AND key = ?", (run_id, key))
            self._conn.execute(
                "INSERT INTO steps (run_id, key, name, kind, status, started_at) VALUES (?, ?, ?, ?, 'running', ?)",
                (run_id, key, name, kind, time.time()),
            )

    def complete_step(
        self,
        run_id: str,
        key: str,
        output: Any,
        *,
        attempts: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        self._write(
            "UPDATE steps SET status = 'completed', output = ?, error = NULL, attempts = ?,"
            " input_tokens = ?, output_tokens = ?, cost_usd = ?, finished_at = ?"
            " WHERE run_id = ? AND key = ?",
            (dumps(output), attempts, input_tokens, output_tokens, cost_usd, time.time(), run_id, key),
        )

    def fail_step(self, run_id: str, key: str, error: str, *, attempts: int) -> None:
        self._write(
            "UPDATE steps SET status = 'failed', error = ?, attempts = ?, finished_at = ? WHERE run_id = ? AND key = ?",
            (error, attempts, time.time(), run_id, key),
        )

    def subtree_usage(self, run_id: str, key: str) -> dict:
        """Step count and cost of everything nested under ``key`` (a parallel group)."""
        row = self._read(
            "SELECT COUNT(*) AS steps, COALESCE(SUM(cost_usd), 0) AS cost_usd FROM steps"
            " WHERE run_id = ? AND status = 'completed' AND key LIKE ?",
            (run_id, key + ".%"),
        )[0]
        return dict(row)

    def usage(self, run_id: str) -> dict:
        row = self._read(
            "SELECT COALESCE(SUM(input_tokens), 0) AS input_tokens,"
            " COALESCE(SUM(output_tokens), 0) AS output_tokens,"
            " COALESCE(SUM(cost_usd), 0) AS cost_usd,"
            " COALESCE(SUM(kind = 'llm'), 0) AS llm_calls"
            " FROM steps WHERE run_id = ? AND status = 'completed'",
            (run_id,),
        )[0]
        return dict(row)

    # -- signals ------------------------------------------------------------

    def request_signal(self, run_id: str, key: str, prompt: str | None) -> None:
        self._write(
            "INSERT INTO signals (run_id, key, prompt, requested_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (run_id, key) DO UPDATE SET prompt = excluded.prompt,"
            " requested_at = COALESCE(signals.requested_at, excluded.requested_at)",
            (run_id, key, prompt, time.time()),
        )

    def deliver_signal(self, run_id: str, key: str, payload: Any) -> None:
        self._write(
            "INSERT INTO signals (run_id, key, payload, delivered_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (run_id, key) DO UPDATE SET payload = excluded.payload,"
            " delivered_at = excluded.delivered_at",
            (run_id, key, dumps(payload), time.time()),
        )

    def get_signal(self, run_id: str, key: str) -> Signal | None:
        rows = self._read("SELECT * FROM signals WHERE run_id = ? AND key = ?", (run_id, key))
        return _signal(rows[0]) if rows else None

    def signals(self, run_id: str | None = None, *, pending_only: bool = False) -> list[Signal]:
        sql = "SELECT * FROM signals WHERE 1 = 1"
        params: tuple = ()
        if run_id:
            sql += " AND run_id = ?"
            params = (run_id,)
        if pending_only:
            sql += " AND delivered_at IS NULL AND requested_at IS NOT NULL"
        return [_signal(r) for r in self._read(sql + " ORDER BY requested_at", params)]

    # -- rewind -------------------------------------------------------------

    def fork(self, run_id: str, at_key: str, *, input: Any = ..., budget: Any = ...) -> Run:
        """Copy a run's journal up to (not including) ``at_key`` into a new run.

        Steps that started before ``at_key`` are kept, except for steps that enclose
        it (a parallel group around the target), which have to run again so that the
        target itself is re-executed.
        """
        source = self.get_run(run_id)
        target = self.get_step(source.id, at_key)
        if target is None:
            raise KeyError(f"run {source.id} has no step {at_key!r}")
        child = self.create_run(
            source.workflow,
            source.input if input is ... else input,
            budget=source.budget if budget is ... else budget,
            parent_run_id=source.id,
            forked_at=at_key,
        )
        kept = [
            s
            for s in self.steps(source.id)
            if s.seq < target.seq and s.status == "completed" and not at_key.startswith(s.key + ".")
        ]
        with self._lock, self._conn:
            for s in kept:
                self._conn.execute(
                    "INSERT INTO steps (run_id, key, name, kind, status, output, attempts, input_tokens,"
                    " output_tokens, cost_usd, started_at, finished_at)"
                    " VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)",
                    (
                        child.id,
                        s.key,
                        s.name,
                        s.kind,
                        dumps(s.output),
                        s.attempts,
                        s.input_tokens,
                        s.output_tokens,
                        s.cost_usd,
                        s.started_at,
                        s.finished_at,
                    ),
                )
        return child


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        workflow=row["workflow"],
        input=loads(row["input"]),
        status=row["status"],
        output=loads(row["output"]),
        error=row["error"],
        budget=loads(row["budget"]),
        parent_run_id=row["parent_run_id"],
        forked_at=row["forked_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _step(row: sqlite3.Row) -> StepRecord:
    return StepRecord(
        seq=row["seq"],
        run_id=row["run_id"],
        key=row["key"],
        name=row["name"],
        kind=row["kind"],
        status=row["status"],
        output=loads(row["output"]),
        error=row["error"],
        attempts=row["attempts"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _signal(row: sqlite3.Row) -> Signal:
    return Signal(
        run_id=row["run_id"],
        key=row["key"],
        prompt=row["prompt"],
        payload=loads(row["payload"]),
        requested_at=row["requested_at"],
        delivered_at=row["delivered_at"],
    )


def iter_tree(steps: list[StepRecord]) -> Iterator[StepRecord]:
    """Yield steps so that every parallel group is followed by its branches' steps."""
    children: dict[str, list[StepRecord]] = {}
    roots: list[StepRecord] = []
    for s in steps:
        parts = s.key.split(".")
        if len(parts) == 1:
            roots.append(s)
        else:
            children.setdefault(".".join(parts[:-2]), []).append(s)

    def walk(nodes: list[StepRecord]) -> Iterator[StepRecord]:
        for node in sorted(nodes, key=_key_order):
            yield node
            yield from walk(children.get(node.key, []))

    yield from walk(roots)


def _key_order(s: StepRecord) -> tuple[int, ...]:
    return tuple(int(p) for p in s.key.split("."))
