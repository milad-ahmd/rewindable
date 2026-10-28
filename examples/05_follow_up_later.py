"""Workflows that span days: send, wait three days, check for a reply, follow up.

    python examples/05_follow_up_later.py       # sends the first email, then sleeps
    rewindable worker                            # wakes the run when the timer is due

``ctx.sleep(3 days)`` does not keep a process alive for three days. The run is
saved as ``waiting`` with a wake-up time, the process exits, and any
``rewindable worker`` on the machine resumes it when it is due, even after a
reboot. The follow-up needs a human OK, and counts as rejected if nobody
answers within a day.

Set REWINDABLE_DEMO_FAST=1 to shrink the days to seconds.
"""

from __future__ import annotations

import os

from _shared import model, tool_results

from rewindable import Agent, Engine, call, tool

FAST = os.environ.get("REWINDABLE_DEMO_FAST") == "1"
DAY = 1.5 if FAST else 86400
INBOX: dict[str, list[str]] = {"sam@example.com": []}  # nobody ever answers in this demo


@tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    print(f"📧 to {to}: {subject}")
    return "sent"


@tool
def check_replies(sender: str) -> list[str]:
    """List replies received from a sender."""
    return INBOX.get(sender, [])


def offline(request: dict) -> object:
    results = tool_results(request)
    task = request["messages"][0]["content"]
    if "follow-up" in task:
        if not results:
            return call("send_email", to="sam@example.com", subject="Re: proposal", body="Any thoughts so far?")
        return "Follow-up sent."
    if not results:
        return call("send_email", to="sam@example.com", subject="Proposal", body="Here is our proposal...")
    return "Proposal sent."


writer = Agent("writer", model("writer", offline), tools=[send_email, check_replies])


def outreach(ctx, contact: str) -> str:
    writer.run(ctx, f"Send {contact} our proposal.")
    ctx.sleep(3 * DAY)
    replies = ctx.step("check replies", check_replies.fn, contact)
    if replies:
        return f"{contact} replied: {replies[0]}"
    if not ctx.approve(f"No reply from {contact} after 3 days. Send a follow-up?", timeout=DAY):
        return "no follow-up sent"
    return writer.run(ctx, f"Write a short, friendly follow-up to {contact}.").text


if __name__ == "__main__":
    result = Engine().run(outreach, "sam@example.com")
    print(result)
    print("\nThe run is saved and this process can exit. Start a worker to wake it on time:")
    print("  rewindable worker")
