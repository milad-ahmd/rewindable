"""Tell a human when a run starts waiting for them.

``Engine(on_waiting=webhook(url))`` POSTs a small JSON message for every approval
or signal a run suspends on, once per signal. ``rewindable start|resume|worker|ui
--notify-url URL`` (or ``REWINDABLE_NOTIFY_URL``) does the same from the CLI.

The message carries the run, the signal key, its prompt and the command that
answers it. It never carries step outputs or the approval code.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from .runtime import Waiting

ENV = "REWINDABLE_NOTIFY_URL"
TIMEOUT_SECONDS = 5.0


def hint(waiting: Waiting) -> str:
    """The command that answers ``waiting``."""
    if waiting.approval:
        return f"rewindable approve {waiting.run_id} {waiting.key}"
    return f"rewindable signal {waiting.run_id} {waiting.key} '<json>'"


def message(waiting: Waiting) -> dict:
    return {
        "run_id": waiting.run_id,
        "workflow": waiting.workflow,
        "key": waiting.key,
        "prompt": waiting.prompt,
        "approval": waiting.approval,
        "requested_at": waiting.requested_at,
        "ui_hint": hint(waiting),
    }


def slack_message(waiting: Waiting) -> dict:
    """Slack incoming webhooks take ``{"text": ...}`` in mrkdwn, where ``&``, ``<``
    and ``>`` must be escaped (they start links and mentions)."""

    def escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    what = "approval" if waiting.approval else f"signal `{escape(waiting.key)}`"
    workflow = waiting.workflow.rsplit("/", 1)[-1]
    lines = [f"⏸ Run `{waiting.run_id}` ({escape(workflow)}) is waiting for {what}"]
    if waiting.prompt:
        lines.append("> " + escape(waiting.prompt).replace("\n", "\n> "))
    lines.append(f"`{escape(hint(waiting))}`")
    return {"text": "\n".join(lines)}


def is_slack(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "hooks.slack.com"


def webhook(url: str, *, timeout: float = TIMEOUT_SECONDS, slack: bool | None = None) -> Callable[[Waiting], None]:
    """An ``on_waiting`` callback that POSTs to ``url``: :func:`message` as JSON, or
    Slack's shape when ``url`` is a Slack incoming webhook (or ``slack=True``).
    It raises on failure; the engine prints that as a warning and carries on."""
    as_slack = is_slack(url) if slack is None else slack

    def send(waiting: Waiting) -> None:
        body: dict[str, Any] = slack_message(waiting) if as_slack else message(waiting)
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "rewindable"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()

    return send
