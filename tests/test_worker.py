"""Leases, durable timers, signal timeouts, the worker, idempotency keys and migrations."""

import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from rewindable import Engine, RunLocked, current_step
from rewindable.store import Store

# Workflows live at module level so the worker can import them by reference.
EFFECTS: list[str] = []


def nap_then_report(ctx):
    ctx.step("before", lambda: EFFECTS.append("before"))
    ctx.sleep(3600)
    ctx.step("after", lambda: EFFECTS.append("after"))
    return "woke up"


def ask_with_timeout(ctx):
    decision = ctx.approve("Ship it?", timeout=600)
    return {"approved": decision.approved, "note": decision.note}


def needs_approval(ctx):
    return "shipped" if ctx.approve("Ship it?") else "held"


def greet(ctx, name):
    return ctx.step("greet", lambda: f"hello {name}")


def slow_branch_and_approval(ctx):
    def slow(c):
        return c.step("slow", lambda: time.sleep(0.3) or "slow done")

    def gated(c):
        return bool(c.approve("ok?", key="gate"))

    return ctx.parallel([slow, gated])


@pytest.fixture
def engine(tmp_path):
    EFFECTS.clear()
    return Engine(tmp_path / "runs.db")


@pytest.fixture
def clock(monkeypatch):
    now = [time.time()]
    monkeypatch.setattr(time, "time", lambda: now[0])
    return now


def test_long_sleep_suspends_and_the_worker_wakes_it(engine, clock):
    first = engine.run(nap_then_report)
    assert first.status == "waiting"
    assert first.waiting_for == []
    assert first.wake_at == pytest.approx(clock[0] + 3600)

    assert engine.work(once=True) == 0  # not due yet
    clock[0] += 3601
    results = []
    assert engine.work(once=True, on_result=results.append) == 1
    assert results[0].output == "woke up"
    assert EFFECTS == ["before", "after"]


def test_short_sleep_happens_in_process(engine):
    started = time.time()
    result = engine.run(lambda ctx: ctx.sleep(0.2) or "done")
    assert result.ok and time.time() - started >= 0.2


def test_approval_timeout_counts_as_rejection(engine, clock):
    first = engine.run(ask_with_timeout)
    assert first.status == "waiting" and first.wake_at == pytest.approx(clock[0] + 600)
    clock[0] += 601
    engine.work(once=True)
    run = engine.store.get_run(first.run_id)
    assert run.output == {"approved": False, "note": "no answer before the timeout"}
    assert engine.store.signals(pending_only=True) == []


def test_worker_resumes_runs_whose_signal_arrived(engine):
    first = engine.run(needs_approval)
    assert engine.work(once=True) == 0  # nothing has changed
    engine.approve(first.run_id, first.waiting_for[0])
    assert engine.work(once=True) == 1
    assert engine.store.get_run(first.run_id).output == "shipped"
    assert engine.work(once=True) == 0  # the signal was consumed


def test_signal_that_arrives_while_other_branches_run_is_not_lost(engine):
    results = {}

    def drive():
        results["first"] = engine.run(slow_branch_and_approval)

    thread = threading.Thread(target=drive)
    thread.start()
    time.sleep(0.1)  # the gate is requested, the slow branch is still running
    engine.approve(engine.store.list_runs()[0].id, "gate")
    thread.join()
    assert results["first"].status == "waiting"  # suspended after the answer came in
    assert engine.work(once=True) == 1
    assert engine.store.get_run(results["first"].run_id).output == ["slow done", True]


def test_submit_queues_for_the_worker(engine):
    run_id = engine.submit(greet, "worker")
    assert engine.store.get_run(run_id).status == "queued"
    engine.work(once=True)
    assert engine.store.get_run(run_id).output == "hello worker"


def test_worker_marks_unimportable_workflows_failed(engine):
    run_id = engine.submit("no_such_module_xyz:flow")
    engine.work(once=True)
    run = engine.store.get_run(run_id)
    assert run.status == "failed" and "no_such_module_xyz" in run.error


def test_work_loop_stops_on_event(engine):
    stop = threading.Event()
    thread = threading.Thread(target=engine.work, kwargs={"poll": 0.05, "stop": stop})
    thread.start()
    run_id = engine.submit(greet, "loop")
    deadline = time.time() + 5
    while engine.store.get_run(run_id).status != "completed" and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert engine.store.get_run(run_id).output == "hello loop"


def test_live_lease_blocks_a_second_driver(engine):
    first = engine.run(needs_approval)
    assert engine.store.acquire_lease(first.run_id, "other-host:123:abc", ttl=60)
    engine.approve(first.run_id, first.waiting_for[0])
    with pytest.raises(RunLocked):
        engine.resume(first.run_id)
    assert engine.work(once=True) == 0  # the worker skips leased runs too
    assert engine.resume(first.run_id, force=True).output == "shipped"


def test_lease_of_a_dead_local_process_is_taken_over(engine):
    import socket

    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    owner = f"{socket.gethostname()}:{dead.stdout.strip()}:dead"
    first = engine.run(needs_approval)
    assert engine.store.acquire_lease(first.run_id, owner, ttl=600)
    engine.approve(first.run_id, first.waiting_for[0])
    assert engine.resume(first.run_id).output == "shipped"


def test_lease_is_released_after_driving(engine):
    result = engine.run(greet, "x")
    run = engine.store.get_run(result.run_id)
    assert run.lease_owner is None and run.lease_expires is None


def test_current_step_gives_a_stable_idempotency_key(engine):
    seen = []

    def charge():
        info = current_step()
        seen.append(info.idempotency_key)
        if len(seen) == 1:
            raise ConnectionError("network blip after the charge went through")
        return "charged"

    result = engine.run(lambda ctx: ctx.step("charge", charge, retries=1, backoff=0))
    assert result.ok
    assert seen == [f"{result.run_id}:1", f"{result.run_id}:1"]
    assert current_step() is None


def test_journals_from_0_1_are_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE runs (id TEXT PRIMARY KEY, workflow TEXT NOT NULL, input TEXT, status TEXT NOT NULL,
            output TEXT, error TEXT, budget TEXT, parent_run_id TEXT, forked_at TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE signals (run_id TEXT NOT NULL, key TEXT NOT NULL, prompt TEXT, payload TEXT,
            requested_at REAL, delivered_at REAL, PRIMARY KEY (run_id, key));
        INSERT INTO runs VALUES ('run_old', 'm:f', 'null', 'completed', '"ok"', NULL, 'null', NULL, NULL, 1, 1);
        """
    )
    conn.commit()
    conn.close()
    store = Store(path)
    run = store.get_run("run_old")
    assert run.output == "ok" and run.wake_at is None
    assert store.signals() == []
