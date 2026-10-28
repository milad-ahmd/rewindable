"""Crash in the middle of a research pipeline, then resume without paying twice.

python examples/01_crash_and_resume.py --crash   # dies while writing the report
rewindable runs                                   # the run is stuck in "running"
rewindable resume <run_id>                        # finishes; earlier steps are replayed
rewindable show <run_id>                          # step-by-step timeline with costs
"""

from __future__ import annotations

import os
import sys

from _shared import model, task_text

from rewindable import Agent, Engine

SOURCES = {
    "https://example.com/temporal": "Temporal persists workflow history so code can resume after failures.",
    "https://example.com/dbos": "DBOS checkpoints each step of a workflow in Postgres.",
    "https://example.com/sagas": "Sagas split long transactions into steps with compensating actions.",
}


def search(topic: str) -> list[str]:
    return list(SOURCES)


def fetch(url: str) -> str:
    return SOURCES[url]


def notes_offline(request: dict) -> str:
    return "Key point: " + task_text(request).split("\n", 1)[-1][:80]


def report_offline(request: dict) -> str:
    return (
        "# Durable execution\n\nAll three approaches record progress outside the process "
        "(history, checkpoints or saga steps), so a crash costs a retry of one step, not the whole job."
    )


summarizer = Agent(
    "summarizer",
    model("summarizer", notes_offline, claude="claude-haiku-5-5"),
    instructions="Summarize the source in one sentence for a technical reader.",
)
writer = Agent(
    "writer",
    model("writer", report_offline),
    instructions="Write a short markdown report from the notes. Be concrete.",
)


def research(ctx, topic: str) -> str:
    urls = ctx.step("search", search, topic)
    pages = ctx.parallel([lambda c, u=u: c.step(f"fetch {u.rsplit('/', 1)[-1]}", fetch, u) for u in urls], name="fetch")
    notes = ctx.parallel(
        [lambda c, p=p: summarizer.run(c, f"Summarize:\n{p}").text for p in pages],
        name="summarize",
    )

    if os.environ.get("REWINDABLE_DEMO_CRASH"):
        print("\n💥 simulated crash (kill -9) right before the report is written\n", flush=True)
        os._exit(1)

    report = writer.run(ctx, f"Topic: {topic}\nNotes:\n- " + "\n- ".join(notes))
    return report.text


if __name__ == "__main__":
    if "--crash" in sys.argv:
        os.environ["REWINDABLE_DEMO_CRASH"] = "1"
    result = Engine().run(research, "durable execution")
    print(result)
    print(result.output)
