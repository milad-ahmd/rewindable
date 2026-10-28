"""A support agent that must get a human's OK before it moves money.

    python examples/02_human_approval.py           # the run pauses before issue_refund
    rewindable pending                              # see what it wants to do
    rewindable approve <run_id> <key>               # or: --reject --note "needs a receipt"

The process exits while the run waits. Approval can come hours later, from any
machine with access to the journal; the run resumes exactly where it stopped.
"""

from __future__ import annotations

from _shared import model, tool_results

from rewindable import Agent, Engine, call, calls, tool

ORDERS = {"A-1042": {"customer": "dana@example.com", "item": "Mechanical keyboard", "amount": 129.0}}


@tool
def lookup_order(order_id: str) -> dict:
    """Look up an order by id.

    Args:
        order_id: Order id such as A-1042.
    """
    return ORDERS[order_id]


@tool(requires_approval=True)
def issue_refund(order_id: str, amount: float, reason: str) -> str:
    """Refund an order. A human must approve every refund.

    Args:
        order_id: The order to refund.
        amount: Amount in EUR.
        reason: Short reason shown to finance.
    """
    print(f"💸 refunded €{amount:.2f} for {order_id}")
    return f"Refund of €{amount:.2f} issued for {order_id}."


def offline(request: dict) -> object:
    results = tool_results(request)
    if not results:
        return calls("Let me check the order.", call("lookup_order", order_id="A-1042"))
    if len(results) == 1:
        return call("issue_refund", order_id="A-1042", amount=129.0, reason="Arrived with broken keys")
    last = results[-1]
    if last.get("is_error"):
        return "I couldn't issue the refund: " + last["content"] + " I've escalated your ticket to a teammate."
    return "Done! " + last["content"] + " You'll see it in 3-5 business days."


support = Agent(
    "support",
    model("support", offline),
    instructions="You are a support agent. Verify the order before refunding. Be brief and kind.",
    tools=[lookup_order, issue_refund],
)


def handle_ticket(ctx, ticket: str) -> str:
    return support.run(ctx, ticket).text


if __name__ == "__main__":
    result = Engine().run(handle_ticket, "Order A-1042 arrived with broken keys. I'd like a refund.")
    print(result)
    if result.waiting_for:
        print("\nThe run is paused and this process can exit. Next:")
        print("  rewindable pending")
        print(f"  rewindable approve {result.run_id} {result.waiting_for[0]}")
    else:
        print(result.output)
