from typing import Literal

import pytest

from rewindable import Agent, Engine, ScriptedModel, call, calls, tool
from rewindable.models import text_of


@pytest.fixture
def engine(tmp_path):
    return Engine(tmp_path / "runs.db")


def tool_results(request):
    """All tool_result blocks the model has been shown so far."""
    return [
        block
        for message in request["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def test_schema_from_type_hints_and_docstring():
    @tool
    def search(query: str, limit: int = 5, lang: Literal["en", "de"] = "en", tags: list[str] | None = None):
        """Search the web.

        Args:
            query: What to look for.
            limit: Max results,
                at most 20.
        """

    assert search.name == "search"
    assert search.description == "Search the web."
    schema = search.input_schema
    assert schema["required"] == ["query"]
    assert schema["properties"]["query"] == {"type": "string", "description": "What to look for."}
    assert schema["properties"]["limit"] == {"type": "integer", "description": "Max results, at most 20.", "default": 5}
    assert schema["properties"]["lang"]["enum"] == ["en", "de"]
    assert schema["properties"]["tags"]["type"] == "array"
    assert schema["properties"]["tags"]["items"] == {"type": "string"}


def test_agent_tool_loop_is_journaled(engine):
    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    def respond(request):
        results = tool_results(request)
        if not results:
            return call("add", a=2, b=3)
        return f"The answer is {results[-1]['content']}"

    model = ScriptedModel(respond)
    agent = Agent("calc", model, tools=[add])
    result = engine.run(lambda ctx: agent.run(ctx, "2+3?").text)
    assert result.output == "The answer is 5"
    assert [s.name for s in engine.store.steps(result.run_id)] == ["calc:llm", "tool:add", "calc:llm"]


def test_tool_errors_are_reported_to_the_model(engine):
    @tool
    def flaky() -> str:
        """Always fails."""
        raise TimeoutError("took too long")

    def respond(request):
        results = tool_results(request)
        if not results:
            return call("flaky")
        assert results[0]["is_error"] is True
        return "handled: " + results[0]["content"]

    agent = Agent("a", ScriptedModel(respond), tools=[flaky])
    result = engine.run(lambda ctx: agent.run(ctx, "go").text)
    assert result.output == "handled: TimeoutError: took too long"


def test_parallel_tool_calls_run_as_branches(engine):
    @tool
    def lookup(city: str) -> str:
        """Weather for a city."""
        return f"{city}: sunny"

    def respond(request):
        results = tool_results(request)
        if not results:
            return calls(call("lookup", city="Berlin"), call("lookup", city="Tehran"))
        return " | ".join(r["content"] for r in results)

    agent = Agent("weather", ScriptedModel(respond), tools=[lookup])
    result = engine.run(lambda ctx: agent.run(ctx, "weather?").text)
    assert result.output == "Berlin: sunny | Tehran: sunny"
    keys = {s.key: s.name for s in engine.store.steps(result.run_id)}
    assert keys["2"] == "weather:tools"
    assert keys["2.0.1"] == keys["2.1.1"] == "tool:lookup"


def test_approval_gate_suspends_then_runs_tool(engine):
    sent = []

    @tool(requires_approval=True)
    def send_email(to: str, body: str) -> str:
        """Send an email."""
        sent.append(to)
        return "sent"

    def respond(request):
        results = tool_results(request)
        if not results:
            return call("send_email", to="boss@example.com", body="hi")
        return "result: " + results[0]["content"]

    model = ScriptedModel(respond)
    agent = Agent("mailer", model, tools=[send_email])

    def flow(ctx):
        return agent.run(ctx, "email my boss").text

    first = engine.run(flow)
    assert first.status == "waiting"
    assert sent == []
    [pending] = engine.store.signals(pending_only=True)
    assert "send_email" in pending.prompt and "boss@example.com" in pending.prompt

    engine.approve(first.run_id, pending.key)
    done = engine.resume(first.run_id, flow)
    assert done.output == "result: sent"
    assert sent == ["boss@example.com"]
    assert model.calls == 2  # the first model call was replayed, not repeated


def test_rejected_tool_call_is_explained_to_the_model(engine):
    @tool(requires_approval=True)
    def delete_repo(name: str) -> str:
        """Delete a repository."""
        raise AssertionError("must not run")

    def respond(request):
        results = tool_results(request)
        return call("delete_repo", name="prod") if not results else results[0]["content"]

    agent = Agent("ops", ScriptedModel(respond), tools=[delete_repo])

    def flow(ctx):
        return agent.run(ctx, "clean up").text

    first = engine.run(flow)
    engine.approve(first.run_id, first.waiting_for[0], approved=False, note="never touch prod")
    assert engine.resume(first.run_id, flow).output == "A human declined this call. Reason: never touch prod"


def test_crash_mid_agent_resumes_without_repeating_model_calls(engine):
    class Crash(BaseException):
        """Simulates the process dying (not an ordinary tool error)."""

    state = {"crash": True}

    @tool
    def fetch(url: str) -> str:
        """Fetch a page."""
        if state["crash"]:
            raise Crash()
        return "<html>ok</html>"

    def respond(request):
        results = tool_results(request)
        return call("fetch", url="https://example.com") if not results else "fetched " + results[0]["content"]

    model = ScriptedModel(respond)
    agent = Agent("reader", model, tools=[fetch])

    def flow(ctx):
        return agent.run(ctx, "read it").text

    with pytest.raises(Crash):
        engine.run(flow)
    [run] = engine.store.list_runs()
    assert run.status == "running"

    state["crash"] = False
    done = engine.resume(run.id, flow)
    assert done.output == "fetched <html>ok</html>"
    assert model.calls == 2


def test_sub_agents_as_tools(engine):
    researcher = Agent("researcher", ScriptedModel(lambda r: "Durable execution means resuming from a journal."))

    def lead_respond(request):
        results = tool_results(request)
        if not results:
            return call("researcher", task="What is durable execution?")
        return "Summary: " + results[0]["content"]

    lead = Agent("lead", ScriptedModel(lead_respond), tools=[researcher.as_tool()])
    result = engine.run(lambda ctx: lead.run(ctx, "explain durability").text)
    assert result.output == "Summary: Durable execution means resuming from a journal."
    assert [s.name for s in engine.store.steps(result.run_id)] == ["lead:llm", "researcher:llm", "lead:llm"]


def test_max_turns(engine):
    agent = Agent("loop", ScriptedModel(lambda r: call("noop")), tools=[tool(lambda: "x", name="noop")], max_turns=3)
    result = engine.run(lambda ctx: agent.run(ctx, "spin").stop_reason)
    assert result.output == "max_turns"


def test_text_of():
    assert (
        text_of({"content": [{"type": "text", "text": "a"}, {"type": "tool_use"}, {"type": "text", "text": "b"}]})
        == "ab"
    )
