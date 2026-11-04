"""`rewindable diff`: what a rewind kept, re-ran and changed."""

import json
import threading
import urllib.request

import pytest

from rewindable import Engine, ScriptedModel, diff_runs
from rewindable.cli import main
from rewindable.ui import make_server

STATE = {"rate": 0.9}


def convert(ctx):
    plan = ctx.step("plan", lambda: "EUR to USD")
    stamp = ctx.step("stamp", lambda: "v1")
    amount = ctx.step("convert", lambda: round(250 * STATE["rate"], 2))
    if STATE.get("extra"):
        ctx.step("audit", lambda: "logged")
    return f"{plan}: {stamp} {amount}"


@pytest.fixture
def engine(tmp_path):
    STATE.clear()
    STATE["rate"] = 0.9
    return Engine(tmp_path / "runs.db")


def rewound(engine):
    first = engine.run(convert)
    STATE.update(rate=1.08, extra=True)
    second = engine.rewind(first.run_id, "2", convert)
    return first, second


def test_diff_classifies_every_step(engine):
    first, second = rewound(engine)
    diff = diff_runs(engine.store, first.run_id, second.run_id)
    assert [(s.key, s.change) for s in diff.steps] == [
        ("1", "reused"),  # copied by the rewind
        ("2", "same"),  # ran again, same result
        ("3", "changed"),
        ("4", "only_b"),
    ]
    assert diff.a.executed == 3 and diff.a.reused == 0
    assert diff.b.steps == 4 and diff.b.reused == 1 and diff.b.executed == 3
    assert diff.counts()["changed"] == 1

    backwards = diff_runs(engine.store, second.run_id, first.run_id)
    assert [s.change for s in backwards.steps] == ["reused", "same", "changed", "only_a"]


def test_failed_step_that_now_succeeds_is_changed(engine):
    def flow(ctx):
        return ctx.step("call", lambda: 1 / STATE["rate"])

    STATE["rate"] = 0
    first = engine.run(flow)
    STATE["rate"] = 2
    second = engine.rewind(first.run_id, "1", flow)
    [step] = diff_runs(engine.store, first.run_id, second.run_id).steps
    assert step.change == "changed" and step.a.status == "failed" and step.b.output == 0.5


def test_model_steps_compare_by_content_not_ids_or_usage(engine):
    from rewindable import Agent, call, tool

    @tool
    def lookup(q: str) -> str:
        """Look something up."""
        return "found"

    def script(request):
        done = any(isinstance(m["content"], list) for m in request["messages"] if m["role"] == "user")
        return "done" if done else call("lookup", q="x")  # a fresh tool-use id every time

    agent = Agent("a", ScriptedModel(script), tools=[lookup])

    def flow(ctx):
        return agent.run(ctx, "go").text

    first = engine.run(flow)
    second = engine.rewind(first.run_id, "1", flow)
    assert [s.change for s in diff_runs(engine.store, first.run_id, second.run_id).steps] == ["same"] * 3
    assert diff_runs(engine.store, first.run_id, second.run_id).cost_delta == pytest.approx(0)


def test_cli_diff_defaults_to_the_parent(engine, capsys):
    first, second = rewound(engine)
    db = str(engine.store.path)
    assert main(["--db", db, "diff", second.run_id[:10]]) == 0
    out = capsys.readouterr().out
    assert first.run_id in out and "B was rewound from A at step 2" in out
    assert "reused" in out and "only in B" in out
    assert "225.0 → 270.0" in out
    assert "1 reused · 1 same · 1 changed · 1 only in B" in out

    assert main(["--db", db, "diff", first.run_id, second.run_id, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["a"]["id"] == first.run_id and data["b"]["reused"] == 1
    assert [s["change"] for s in data["steps"]] == ["reused", "same", "changed", "only_b"]


def test_cli_diff_usage_errors(engine, capsys):
    first = engine.run(convert)
    db = str(engine.store.path)
    assert main(["--db", db, "diff", first.run_id]) == 2
    assert "give two runs" in capsys.readouterr().err
    assert main(["--db", db, "diff", "run_nope", first.run_id]) == 2


def test_ui_diff_api(engine):
    first, second = rewound(engine)
    server = make_server(engine, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/runs/{first.run_id}/diff/{second.run_id}"
        with urllib.request.urlopen(url) as res:
            data = json.loads(res.read())
        assert data["counts"] == {"reused": 1, "same": 1, "changed": 1, "only_a": 0, "only_b": 1}
        assert data["steps"][2]["a"]["output"] == 225.0 and data["steps"][2]["b"]["output"] == 270.0
    finally:
        server.shutdown()
        server.server_close()
