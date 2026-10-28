"""Helpers shared by the examples.

Every example runs offline by default with a ScriptedModel, so you can try the
whole crash/approve/rewind cycle for free. Set REWINDABLE_LIVE=1 (and have
Anthropic credentials, e.g. ANTHROPIC_API_KEY) to use Claude instead.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from rewindable import AnthropicModel, Model, ScriptedModel

LIVE = os.environ.get("REWINDABLE_LIVE") == "1"


def model(name: str, offline: Callable[[dict], object], *, claude: str = "claude-opus-5-5") -> Model:
    if LIVE:
        return AnthropicModel(claude, effort="medium")
    # A small delay makes the offline demo feel like real model latency.
    return ScriptedModel(offline, name=name, delay=0.3)


def tool_results(request: dict) -> list[dict]:
    return [
        block
        for message in request["messages"]
        if message["role"] == "user" and isinstance(message["content"], list)
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]


def task_text(request: dict) -> str:
    first = request["messages"][0]["content"]
    return first if isinstance(first, str) else ""
