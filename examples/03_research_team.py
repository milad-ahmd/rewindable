"""A lead agent delegates to specialist sub-agents in parallel, under a hard budget.

    python examples/03_research_team.py
    rewindable show <run_id>        # nested timeline: lead -> parallel specialists -> lead

Each specialist is an Agent exposed as a tool with ``as_tool()``. When the lead
calls several in one turn they run concurrently, and every call inside them is
journaled, so a crash in one specialist never re-runs the others.
"""

from __future__ import annotations

from _shared import model, task_text, tool_results

from rewindable import Agent, Budget, Engine, call, calls, tool


@tool
def market_size(segment: str) -> dict:
    """Return a market size estimate for a segment (demo data)."""
    return {"segment": segment, "tam_usd_bn": 4.2, "cagr": "31%"}


def specialist_offline(findings: str):
    def respond(request: dict) -> object:
        results = tool_results(request)
        if "market" in task_text(request).lower() and not results:
            return call("market_size", segment="AI agent infrastructure")
        if results:
            return f"{findings} Data: {results[-1]['content']}"
        return findings

    return respond


market = Agent(
    "market",
    model(
        "market",
        specialist_offline("Demand is driven by teams moving agents to production."),
        claude="claude-sonnet-5-5",
    ),
    instructions="You are a market analyst. Use tools for numbers. Answer in 2 sentences.",
    tools=[market_size],
)
tech = Agent(
    "tech",
    model(
        "tech",
        specialist_offline("The hard parts are durability, observability and cost control."),
        claude="claude-sonnet-5-5",
    ),
    instructions="You are a staff engineer. Name the hardest technical risks. 2 sentences.",
)
risk = Agent(
    "risk",
    model(
        "risk",
        specialist_offline("Main risk: frameworks commoditize fast; differentiate on reliability."),
        claude="claude-sonnet-5-5",
    ),
    instructions="You are a skeptical investor. Name the biggest risk. 2 sentences.",
)


def lead_offline(request: dict) -> object:
    results = tool_results(request)
    if not results:
        return calls(
            "I'll brief three specialists in parallel.",
            call("market", task="Size the market for AI agent infrastructure."),
            call("tech", task="What makes agents hard to run in production?"),
            call("risk", task="What is the biggest risk for a new agent framework?"),
        )
    return "## Brief\n\n" + "\n\n".join(f"- {r['content']}" for r in results)


lead = Agent(
    "lead",
    model("lead", lead_offline),
    instructions="You lead a research team. Delegate to specialists in parallel, then write a 5-line brief.",
    tools=[market.as_tool(), tech.as_tool(), risk.as_tool()],
)


def brief(ctx, question: str) -> str:
    return lead.run(ctx, question).text


if __name__ == "__main__":
    result = Engine().run(
        brief, "Should we build an open-source durable agent runtime?", budget=Budget(max_cost_usd=2.00)
    )
    print(result)
    print(result.output)
