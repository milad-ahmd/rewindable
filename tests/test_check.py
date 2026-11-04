"""`Engine.check` / `rewindable check`: replay recorded runs against new code, run nothing."""

import sqlite3
import textwrap

import pytest

from rewindable import Engine, ScriptedModel
from rewindable.cli import main

CALLS: list[str] = []


def record(name, value=None):
    def fn():
        CALLS.append(name)
        return value if value is not None else name

    return fn


def journal(engine):
    """Every row of every table, to prove a check writes nothing."""
    with sqlite3.connect(engine.store.path) as conn:
        return list(conn.iterdump())


@pytest.fixture
def engine(tmp_path):
    CALLS.clear()
    return Engine(tmp_path / "runs.db")


def test_waiting_run_checks_ok_and_nothing_runs(engine):
    model = ScriptedModel(lambda request: "a summary")

    def flow(ctx):
        ctx.step("fetch", record("fetch"))
        ctx.llm(model, messages=[{"role": "user", "content": "sum up"}])
        ctx.sleep(0.01)
        if not ctx.approve("Publish?"):
            return "held"
        return ctx.step("publish", record("publish"))

    run = engine.run(flow)
    assert run.status == "waiting"
    CALLS.clear()
    model.calls = 0
    before = journal(engine)

    result = engine.check(run.run_id, flow)
    assert result.ok and result.outcome == "ok"
    assert result.replayed_steps == 3
    assert result.resumes_at == "4" and result.next_step == "signal:approve-4"
    assert CALLS == [] and model.calls == 0
    assert journal(engine) == before


def test_divergence_is_reported_with_key_and_both_names(engine):
    def v1(ctx):
        ctx.step("fetch", record("fetch"))
        ctx.step("summarize", record("summarize"))
        return ctx.wait_for("go")

    def v2(ctx):
        ctx.step("fetch", record("fetch"))
        ctx.step("fetch-again", record("fetch-again"))
        return ctx.wait_for("go")

    run = engine.run(v1)
    CALLS.clear()
    before = journal(engine)
    result = engine.check(run.run_id, v2)
    assert result.outcome == "diverged" and not result.ok
    assert (result.key, result.journal, result.code) == ("2", "summarize", "fetch-again")
    assert CALLS == [] and journal(engine) == before
    assert engine.store.get_run(run.run_id).status == "waiting"


def test_delivered_signal_is_not_consumed(engine):
    def flow(ctx):
        return ctx.wait_for("go")

    run = engine.run(flow)
    engine.signal(run.run_id, "go", {"x": 1})
    before = journal(engine)
    result = engine.check(run.run_id, flow)
    assert result.ok and result.resumes_at == "1"
    assert journal(engine) == before
    assert engine.store.get_signal(run.run_id, "go").consumed_at is None


def test_parallel_branches_are_checked_past_an_unfinished_group(engine):
    def v1(ctx):
        def done(c):
            return c.step("done", record("done"))

        def gated(c):
            c.step("prep", record("prep"))
            return bool(c.approve("ok?", key="gate"))

        return ctx.parallel([done, gated])

    def v2(ctx):
        def done(c):
            return c.step("done", record("done"))

        def gated(c):
            c.step("prep-v2", record("prep"))
            return bool(c.approve("ok?", key="gate"))

        return ctx.parallel([done, gated])

    run = engine.run(v1)
    assert run.status == "waiting"
    ok = engine.check(run.run_id, v1)
    assert ok.ok and ok.resumes_at == "1.1.2" and ok.replayed_steps == 2
    bad = engine.check(run.run_id, v2)
    assert bad.outcome == "diverged" and bad.key == "1.1.1"


def test_completed_parallel_group_replays_whole(engine):
    def flow(ctx):
        values = ctx.parallel([lambda c: c.step("a", record("a")), lambda c: c.step("b", record("b"))])
        return ctx.step("join", record("join", "+".join(values)))

    run = engine.run(flow)
    CALLS.clear()
    result = engine.check(run.run_id, flow)
    assert result.ok and result.resumes_at is None and result.replayed_steps == 4
    assert CALLS == []


def test_failed_step_is_where_a_resume_continues(engine):
    def flow(ctx):
        ctx.step("one", record("one"))
        ctx.step("boom", lambda: 1 / 0)

    run = engine.run(flow)
    assert run.status == "failed"
    result = engine.check(run.run_id, flow)
    assert result.ok and result.resumes_at == "2" and result.next_step == "boom"


def test_workflow_that_raises_while_replaying(engine):
    def v1(ctx):
        ctx.step("count", lambda: 3)
        return ctx.wait_for("go")

    def v2(ctx):
        n = ctx.step("count", lambda: 3)
        return {"ratio": 1 / (n - 3)}

    run = engine.run(v1)
    result = engine.check(run.run_id, v2)
    assert result.outcome == "failed" and "ZeroDivisionError" in result.error


# -- ctx.patched -----------------------------------------------------------


def test_patched_takes_the_new_path_only_for_new_runs(engine):
    def v1(ctx):
        ctx.step("fetch", record("fetch"))
        ctx.step("total", record("total", 100))
        return ctx.wait_for("go")

    def v2(ctx):
        ctx.step("fetch", record("fetch"))
        if ctx.patched("vat"):
            total = ctx.step("total-with-vat", record("total-with-vat", 120))
        else:
            total = ctx.step("total", record("total", 100))
        ctx.wait_for("go")
        return total

    old = engine.run(v1)  # went past the patch point on the old code
    new = engine.run(v2)
    assert engine.check(old.run_id, v2).ok
    assert engine.check(new.run_id, v2).ok

    for run in (old, new):
        engine.signal(run.run_id, "go", True)
    CALLS.clear()
    assert engine.resume(old.run_id, v2).output == 100
    assert engine.resume(new.run_id, v2).output == 120
    assert CALLS == []  # both replayed their own path
    assert [s.name for s in engine.store.steps(new.run_id)][:3] == ["fetch", "patch:vat", "total-with-vat"]


def test_patched_on_a_run_that_had_not_reached_the_point(engine):
    def v1(ctx):
        ctx.step("fetch", record("fetch"))
        ctx.wait_for("go")
        return ctx.step("total", record("total", 100))

    def v2(ctx):
        ctx.step("fetch", record("fetch"))
        ctx.wait_for("go")
        if ctx.patched("vat"):
            return ctx.step("total-with-vat", record("total-with-vat", 120))
        return ctx.step("total", record("total", 100))

    run = engine.run(v1)
    check = engine.check(run.run_id, v2)
    assert check.ok and check.resumes_at == "2"
    engine.signal(run.run_id, "go", True)
    assert engine.resume(run.run_id, v2).output == 120


# -- CLI -------------------------------------------------------------------


@pytest.fixture
def script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "flow.py"

    def write(body):
        path.write_text(textwrap.dedent(body))
        import sys

        for name in [m for m in sys.modules if m.startswith("rewindable_wf_")]:
            del sys.modules[name]  # pick up the new code, like a fresh process would

    write(
        """
        def release(ctx, version):
            ctx.step("changelog", lambda: "notes")
            return "shipped" if ctx.approve(f"Ship {version}?") else "held"
        """
    )
    return path, write


def test_cli_check(tmp_path, script, capsys):
    path, write = script
    db = str(tmp_path / "runs.db")
    assert main(["--db", db, "start", f"{path}:release", "--input", '"1.0"']) == 0
    assert main(["--db", db, "start", f"{path}:release", "--input", '"2.0"']) == 0
    capsys.readouterr()

    assert main(["--db", db, "check"]) == 0
    out = capsys.readouterr().out
    assert out.count(" ok ") == 2 and "2 runs checked: 2 ok, 0 not" in out

    write(
        """
        def release(ctx, version):
            ctx.step("release-notes", lambda: "notes")
            return "shipped" if ctx.approve(f"Ship {version}?") else "held"
        """
    )
    assert main(["--db", db, "check", "flow.py:release"]) == 1
    out = capsys.readouterr().out
    assert "diverges at step 1: journal changelog, code release-notes" in out

    assert main(["--db", db, "check", "other.py"]) == 0
    assert "No runs to check." in capsys.readouterr().out

    path.unlink()
    import sys

    for name in [m for m in sys.modules if m.startswith("rewindable_wf_")]:
        del sys.modules[name]
    assert main(["--db", db, "check", "--json"]) == 1
    import json

    results = json.loads(capsys.readouterr().out)
    assert {r["outcome"] for r in results} == {"unloadable"}

    assert main(["--db", db, "check", "--run", "run_nope"]) == 2
