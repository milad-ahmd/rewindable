import subprocess
import sys
import textwrap

import pytest

from rewindable import Budget, Engine, NestedStepError, NonDeterminismError, ScriptedModel


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "runs.db")


class Counter:
    def __init__(self):
        self.calls = {}

    def __call__(self, name, value=None):
        def fn(*args):
            self.calls[name] = self.calls.get(name, 0) + 1
            return value if value is not None else f"{name}-result"

        return fn


def test_completed_run_returns_output(engine):
    def flow(ctx, n):
        a = ctx.step("double", lambda: n * 2)
        return {"a": a, "b": ctx.step("inc", lambda: a + 1)}

    result = engine.run(flow, 5)
    assert result.ok
    assert result.output == {"a": 10, "b": 11}
    assert result.executed_steps == 2
    assert [s.name for s in engine.store.steps(result.run_id)] == ["double", "inc"]


def test_resume_after_failure_replays_completed_steps(engine):
    seen = Counter()
    state = {"broken": True}

    def flaky():
        if state["broken"]:
            raise RuntimeError("upstream is down")
        return "ok"

    def flow(ctx):
        ctx.step("one", seen("one"))
        ctx.step("two", seen("two"))
        ctx.step("three", flaky)
        return ctx.step("four", seen("four", "done"))

    first = engine.run(flow)
    assert first.status == "failed"
    assert "upstream is down" in first.error

    state["broken"] = False
    second = engine.resume(first.run_id, flow)
    assert second.ok and second.output == "done"
    assert seen.calls == {"one": 1, "two": 1, "four": 1}
    assert second.replayed_steps == 2
    assert second.executed_steps == 2


def test_hard_crash_then_resume_from_another_process(tmp_path):
    script = tmp_path / "pipeline.py"
    marker = tmp_path / "executions.log"
    script.write_text(
        textwrap.dedent(
            f"""
            import os, sys
            from rewindable import Engine

            def record(name):
                with open({str(marker)!r}, "a") as f:
                    f.write(name + "\\n")
                return name

            def pipeline(ctx, topic):
                for name in ["search", "read", "summarize", "publish"]:
                    if name == "summarize" and os.environ.get("CRASH"):
                        os._exit(1)  # kill -9 style: no cleanup, no exception handling
                    ctx.step(name, record, name)
                return "published " + topic

            if __name__ == "__main__":
                result = Engine(sys.argv[1]).run(pipeline, "durability")
                print(result.run_id)
            """
        )
    )
    db = tmp_path / "runs.db"
    crashed = subprocess.run(
        [sys.executable, str(script), str(db)], env={"CRASH": "1", "PATH": ""}, capture_output=True, text=True
    )
    assert crashed.returncode == 1

    engine = Engine(db)
    [run] = engine.store.list_runs()
    assert run.status == "running"  # nobody got to mark it failed

    result = engine.resume(run.id)  # imports pipeline() from the recorded script path
    assert result.ok and result.output == "published durability"
    assert marker.read_text().split() == ["search", "read", "summarize", "publish"]
    assert result.replayed_steps == 2


def test_changed_workflow_raises_non_determinism(engine):
    def v1(ctx):
        ctx.step("fetch", lambda: 1)
        raise RuntimeError("stop")

    def v2(ctx):
        ctx.step("summarize", lambda: 2)

    result = engine.run(v1)
    resumed = engine.resume(result.run_id, v2)
    assert resumed.status == "failed"
    assert "NonDeterminismError" in resumed.error


def test_nested_steps_are_rejected(engine):
    def flow(ctx):
        return ctx.step("outer", lambda: ctx.step("inner", lambda: 1))

    result = engine.run(flow)
    assert result.status == "failed"
    assert NestedStepError.__name__ in result.error


def test_retries(engine):
    attempts = {"n": 0}

    def sometimes():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("blip")
        return "ok"

    result = engine.run(lambda ctx: ctx.step("call", sometimes, retries=3, backoff=0))
    assert result.ok
    [step] = engine.store.steps(result.run_id)
    assert step.attempts == 3


def test_results_are_json_round_tripped_consistently(engine):
    outputs = []

    def flow(ctx):
        value = ctx.step("tuple", lambda: (1, 2))
        outputs.append(value)
        raise RuntimeError("again")

    run = engine.run(flow)
    engine.resume(run.run_id, flow)
    assert outputs == [[1, 2], [1, 2]]


def test_wait_for_suspends_and_resumes(engine):
    def flow(ctx):
        ctx.step("prepare", lambda: 1)
        answer = ctx.wait_for("reviewer", "Which title?")
        return f"chosen: {answer}"

    first = engine.run(flow)
    assert first.status == "waiting"
    assert first.waiting_for == ["reviewer"]
    [pending] = engine.store.signals(pending_only=True)
    assert pending.prompt == "Which title?"

    assert engine.resume(first.run_id, flow).status == "waiting"  # still nothing delivered

    engine.signal(first.run_id, "reviewer", "Option B")
    done = engine.resume(first.run_id, flow)
    assert done.ok and done.output == "chosen: Option B"
    assert engine.store.signals(pending_only=True) == []


def test_approve_and_reject(engine):
    def flow(ctx):
        decision = ctx.approve("Deploy to prod?")
        return {"approved": decision.approved, "note": decision.note}

    first = engine.run(flow)
    [key] = first.waiting_for
    engine.approve(first.run_id, key, approved=False, note="freeze week")
    assert engine.resume(first.run_id, flow).output == {"approved": False, "note": "freeze week"}


def test_parallel_runs_branches_and_survives_suspension(engine):
    seen = Counter()

    def flow(ctx):
        def branch(name, needs_human=False):
            def run(c):
                c.step(f"{name}-work", seen(name))
                if needs_human:
                    c.wait_for(f"{name}-ok")
                return name

            return run

        return ctx.parallel([branch("a"), branch("b", needs_human=True), branch("c")])

    first = engine.run(flow)
    assert first.status == "waiting"
    assert seen.calls == {"a": 1, "b": 1, "c": 1}

    engine.signal(first.run_id, "b-ok", True)
    done = engine.resume(first.run_id, flow)
    assert done.ok and done.output == ["a", "b", "c"]
    assert seen.calls == {"a": 1, "b": 1, "c": 1}
    keys = sorted(s.key for s in engine.store.steps(done.run_id))
    assert keys == ["1", "1.0.1", "1.1.1", "1.1.2", "1.2.1"]


def test_budget_stops_runaway_runs_and_can_be_raised(engine):
    model = ScriptedModel(lambda request: "ok", tokens_per_call=(1000, 1000))

    def flow(ctx):
        for _ in range(5):
            ctx.llm(model, messages=[{"role": "user", "content": "hi"}])
        return "done"

    first = engine.run(flow, budget=Budget(max_llm_calls=2))
    assert first.status == "failed"
    assert "BudgetExceeded" in first.error
    assert model.calls == 2

    done = engine.resume(first.run_id, flow, budget=Budget(max_llm_calls=10))
    assert done.ok
    assert model.calls == 5
    assert done.replayed_steps == 2
    assert done.saved_usd > 0


def test_rewind_reexecutes_from_a_step_and_keeps_the_original(engine):
    seen = Counter()
    version = {"summary": "buggy summary"}

    def flow(ctx):
        ctx.step("search", seen("search"))
        ctx.step("read", seen("read"))
        summary = ctx.step("summarize", lambda: version["summary"])
        return ctx.step("format", lambda: summary.upper())

    original = engine.run(flow)
    assert original.output == "BUGGY SUMMARY"

    version["summary"] = "fixed summary"
    branched = engine.rewind(original.run_id, "3", flow)
    assert branched.ok and branched.output == "FIXED SUMMARY"
    assert seen.calls == {"search": 1, "read": 1}
    assert branched.replayed_steps == 2
    assert engine.store.get_run(original.run_id).output == "BUGGY SUMMARY"
    child = engine.store.get_run(branched.run_id)
    assert (child.parent_run_id, child.forked_at) == (original.run_id, "3")


def test_rewind_into_a_parallel_branch_reruns_the_enclosing_group(engine):
    seen = Counter()

    def flow(ctx):
        return ctx.parallel([lambda c: c.step("left", seen("left")), lambda c: c.step("right", seen("right"))])

    original = engine.run(flow)
    branched = engine.rewind(original.run_id, "1.1.1", flow)
    assert branched.ok
    assert seen.calls == {"left": 1, "right": 2}


def test_now_and_uuid_are_stable_across_replays(engine):
    seen = []

    def flow(ctx):
        seen.append((ctx.now(), ctx.uuid()))
        if len(seen) == 1:
            raise RuntimeError("crash")

    run = engine.run(flow)
    engine.resume(run.run_id, flow)
    assert seen[0] == seen[1]


def test_non_determinism_error_is_exported():
    assert issubclass(NonDeterminismError, Exception)
