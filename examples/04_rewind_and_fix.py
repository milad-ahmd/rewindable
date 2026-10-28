"""Debug an agent by rewinding: fix a buggy tool and re-run from the step that broke.

    python examples/04_rewind_and_fix.py            # wrong answer: the converter has a bug
    rewindable show <run_id>                        # spot the bad tool result at step 2
    REWINDABLE_DEMO_FIXED=1 rewindable rewind <run_id> --to 2

The rewind branches a new run that keeps step 1 (the first model call) from the
journal and re-executes from step 2 with the fixed code. The original run is
kept for comparison. In a real project you would edit the code instead of
flipping an environment variable.
"""

from __future__ import annotations

import os

from _shared import model, tool_results

from rewindable import Agent, Engine, call, tool

RATES = {"EUR": 1.0, "USD": 1.08, "IRR": 45000.0}


@tool
def convert(amount: float, source: str, target: str) -> float:
    """Convert money between currencies.

    Args:
        amount: Amount in the source currency.
        source: ISO code, e.g. EUR.
        target: ISO code, e.g. USD.
    """
    if os.environ.get("REWINDABLE_DEMO_FIXED"):
        return round(amount / RATES[source] * RATES[target], 2)
    return round(amount * RATES[source] / RATES[target], 2)  # bug: the ratio is upside down


def offline(request: dict) -> object:
    results = tool_results(request)
    if not results:
        return call("convert", amount=250, source="EUR", target="USD")
    return f"250 EUR is {results[-1]['content']} USD."


assistant = Agent(
    "fx",
    model("fx", offline),
    instructions="Answer currency questions using the convert tool.",
    tools=[convert],
)


def answer(ctx, question: str) -> str:
    return assistant.run(ctx, question).text


if __name__ == "__main__":
    result = Engine().run(answer, "How much is 250 EUR in USD?")
    print(result)
    print(result.output, "(should be 270.0)")
    print(f"\nInspect it:  rewindable show {result.run_id}")
    print(f"Fix + rewind: REWINDABLE_DEMO_FIXED=1 rewindable rewind {result.run_id} --to 2")
