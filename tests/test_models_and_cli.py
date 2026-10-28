import json
import textwrap
from types import SimpleNamespace

import pytest

from rewindable import AnthropicModel, Engine, OpenAICompatModel, ScriptedModel, call, calls
from rewindable.cli import main
from rewindable.models import from_openai_response, priced_cost, to_openai_messages


class FakeMessage:
    def __init__(self, data):
        self.data = data

    def to_dict(self, mode="python"):
        return self.data


def test_anthropic_adapter_uses_fallbacks_and_prices_by_served_model():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return FakeMessage(
            {
                "id": "msg_1",
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "hi"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 100_000},
            }
        )

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    model = AnthropicModel(client=client, effort="high")
    response = model.complete({"messages": [{"role": "user", "content": "hi"}]})

    assert captured["model"] == "claude-opus-5-5"
    assert captured["betas"] == ["server-side-fallback-2026-07-01"]
    assert captured["fallbacks"] == "default"
    assert captured["output_config"] == {"effort": "high"}
    assert response["content"] == [{"type": "text", "text": "hi"}]
    assert model.cost(response) == pytest.approx(4.00 + 2.00)


def test_anthropic_adapter_without_fallbacks_uses_plain_endpoint():
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: FakeMessage(
                {"content": [], "stop_reason": "end_turn", "usage": {}, "model": kw["model"]}
            )
        )
    )
    model = AnthropicModel("claude-haiku-5-5", client=client, fallbacks=None)
    assert model.complete({"messages": []})["model"] == "claude-haiku-5-5"


def test_openai_round_trip():
    request = {
        "system": "be brief",
        "messages": [
            {"role": "user", "content": "weather in Paris?"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "t1", "name": "weather", "input": {"city": "Paris"}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "18C"}]},
        ],
    }
    assert to_openai_messages(request) == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "weather in Paris?"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "weather", "arguments": '{"city": "Paris"}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "18C"},
    ]

    response = from_openai_response(
        {
            "model": "llama3.2",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "content": None,
                        "tool_calls": [{"id": "c1", "function": {"name": "weather", "arguments": '{"city":"Rome"}'}}],
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        "llama3.2",
    )
    assert response["stop_reason"] == "tool_use"
    assert response["content"] == [{"type": "tool_use", "id": "c1", "name": "weather", "input": {"city": "Rome"}}]
    assert response["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_openai_adapter_sends_tools():
    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {}}

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    model = OpenAICompatModel("llama3.2", client=client)
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    assert (
        model.complete({"messages": [{"role": "user", "content": "x"}], "tools": tools})["content"][0]["text"] == "ok"
    )
    assert captured["tools"][0]["function"]["name"] == "t"
    assert model.cost({"usage": {"input_tokens": 99}}) == 0.0


def test_priced_cost_counts_cache_tokens():
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1_000_000}
    assert priced_cost(usage, (4.0, 20.0, 0.2)) == pytest.approx(0.2)
    assert priced_cost(usage, (4.0, 20.0)) == pytest.approx(0.4)


def test_scripted_model_list_and_calls_helper():
    model = ScriptedModel(["first", calls("Checking.", call("search", q="x"))])
    assert model.complete({})["content"] == [{"type": "text", "text": "first"}]
    second = model.complete({})
    assert second["stop_reason"] == "tool_use"
    assert second["content"][0] == {"type": "text", "text": "Checking."}
    with pytest.raises(RuntimeError):
        model.complete({})


# -- CLI --------------------------------------------------------------------


@pytest.fixture
def approval_script(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "flow.py").write_text(
        textwrap.dedent(
            """
            def release(ctx, version):
                notes = ctx.step("changelog", lambda: f"notes for {version}")
                if not ctx.approve(f"Ship {version}?"):
                    return "cancelled"
                return ctx.step("publish", lambda: f"published {version}: {notes}")
            """
        )
    )
    import importlib.util

    spec = importlib.util.spec_from_file_location("flow_under_test", tmp_path / "flow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.release.__module__ = "__main__"  # behave like a script run with `python flow.py`
    import sys

    monkeypatch.setattr(sys.modules["__main__"], "__file__", str(tmp_path / "flow.py"), raising=False)
    return module.release


def test_cli_full_approval_flow(tmp_path, approval_script, capsys):
    db = str(tmp_path / "runs.db")
    first = Engine(db).run(approval_script, "1.0.0")
    assert first.status == "waiting"

    assert main(["--db", db, "pending"]) == 0
    out = capsys.readouterr().out
    assert "Ship 1.0.0?" in out and first.waiting_for[0] in out

    assert main(["--db", db, "approve", first.run_id[:10], first.waiting_for[0]]) == 0
    out = capsys.readouterr().out
    assert "completed" in out and "replayed 1 step " in out

    assert main(["--db", db, "show", first.run_id]) == 0
    out = capsys.readouterr().out
    assert "changelog" in out and "approved" in out and "published 1.0.0: notes for 1.0.0" in out

    assert main(["--db", db, "runs"]) == 0
    assert first.run_id in capsys.readouterr().out

    assert main(["--db", db, "rewind", first.run_id, "--to", "3"]) == 0
    assert "rewound" in capsys.readouterr().out
    assert len(Engine(db).store.list_runs()) == 2


def test_cli_reports_unknown_run(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "runs.db"), "show", "run_nope"]) == 1
    assert "run_nope" in capsys.readouterr().err


def test_signal_payload_json(tmp_path):
    engine = Engine(tmp_path / "runs.db")

    def flow(ctx):
        return ctx.wait_for("choice")

    run = engine.run(flow)
    engine.signal(run.run_id, "choice", json.loads('{"pick": 2}'))
    assert engine.resume(run.run_id, flow).output == {"pick": 2}
