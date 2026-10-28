"""Model adapters.

Every adapter speaks one normalized format, modeled on the Claude Messages API:

request  = {"messages": [...], "system": str?, "tools": [{"name", "description", "input_schema"}]?}
response = {"content": [blocks], "stop_reason": str, "usage": {"input_tokens", "output_tokens", ...}, "model": str}

Content blocks are plain dicts: ``{"type": "text", "text": ...}``,
``{"type": "tool_use", "id", "name", "input"}`` and, in user turns,
``{"type": "tool_result", "tool_use_id", "content", "is_error"}``.
Responses are stored in the journal exactly as returned, so they must be JSON.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Iterable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Model(Protocol):
    name: str

    def complete(self, request: dict) -> dict:
        """Send one request and return a normalized response."""
        ...

    def cost(self, response: dict) -> float:
        """Estimated USD cost of a normalized response."""
        ...


# USD per million tokens: (input, output, cache read). Estimates for budgeting,
# not billing; check the provider's pricing page for exact numbers.
CLAUDE_PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}


def priced_cost(usage: dict, price: tuple[float, ...] | None) -> float:
    if not price:
        return 0.0
    input_price, output_price = price[0], price[1]
    cache_read_price = price[2] if len(price) > 2 else input_price * 0.1
    tokens_in = usage.get("input_tokens") or 0
    tokens_out = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    return (
        tokens_in * input_price
        + tokens_out * output_price
        + cache_read * cache_read_price
        + cache_write * input_price * 1.25
    ) / 1_000_000


def text_of(response_or_blocks: dict | list) -> str:
    """Concatenate the text blocks of a response (or a list of blocks)."""
    blocks = response_or_blocks.get("content", []) if isinstance(response_or_blocks, dict) else response_or_blocks
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# -- Claude -----------------------------------------------------------------


class AnthropicModel:
    """Claude through the official ``anthropic`` SDK (``pip install "rewindable[anthropic]"``).

    Credentials come from the environment (``ANTHROPIC_API_KEY`` or an ``ant auth login``
    profile). Thinking is adaptive by default on current models; tune depth with
    ``effort``. ``fallbacks="default"`` asks the API to retry a declined request on a
    suitable fallback model inside the same call; pass ``fallbacks=None`` to turn it off.
    """

    def __init__(
        self,
        model: str = "claude-opus-5-5",
        *,
        max_tokens: int = 16000,
        effort: str | None = None,
        fallbacks: str | None = "default",
        client: Any = None,
        price: tuple[float, ...] | None = None,
        retries: int = 2,
        **params: Any,
    ):
        self.name = model
        self.max_tokens = max_tokens
        self.effort = effort
        self.fallbacks = fallbacks
        self.price = price
        self.retries = retries
        self.params = params
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise ImportError('AnthropicModel needs the SDK: pip install "rewindable[anthropic]"') from exc
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, request: dict) -> dict:
        kwargs: dict[str, Any] = {"model": self.name, "max_tokens": self.max_tokens, **self.params, **request}
        if self.effort:
            kwargs.setdefault("output_config", {})["effort"] = self.effort
        if self.fallbacks:
            message = self.client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks=self.fallbacks, **kwargs
            )
        else:
            message = self.client.messages.create(**kwargs)
        data = message.to_dict(mode="json")
        return {
            "content": data.get("content", []),
            "stop_reason": data.get("stop_reason"),
            "usage": data.get("usage") or {},
            "model": data.get("model", self.name),
            "id": data.get("id"),
        }

    def cost(self, response: dict) -> float:
        price = self.price or CLAUDE_PRICES.get(response.get("model") or self.name) or CLAUDE_PRICES.get(self.name)
        return priced_cost(response.get("usage") or {}, price)


# -- OpenAI-compatible (OpenAI, Ollama, vLLM, LM Studio, OpenRouter, ...) ------


class OpenAICompatModel:
    """Any Chat Completions endpoint through the ``openai`` SDK
    (``pip install "rewindable[openai]"``). Point ``base_url`` at a local server such
    as Ollama (``http://localhost:11434/v1``) to run agents for free.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int | None = None,
        client: Any = None,
        price: tuple[float, float] | None = None,
        retries: int = 2,
        **params: Any,
    ):
        self.name = model
        self.base_url = base_url
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.price = price
        self.retries = retries
        self.params = params
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import openai
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise ImportError('OpenAICompatModel needs the SDK: pip install "rewindable[openai]"') from exc
            key = self.api_key or ("not-needed" if self.base_url else None)
            self._client = openai.OpenAI(base_url=self.base_url, api_key=key)
        return self._client

    def complete(self, request: dict) -> dict:
        kwargs: dict[str, Any] = {"model": self.name, "messages": to_openai_messages(request), **self.params}
        if request.get("tools"):
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
                for t in request["tools"]
            ]
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        completion = self.client.chat.completions.create(**kwargs)
        data = completion.model_dump(mode="json") if hasattr(completion, "model_dump") else completion
        return from_openai_response(data, self.name)

    def cost(self, response: dict) -> float:
        return priced_cost(response.get("usage") or {}, self.price)


def to_openai_messages(request: dict) -> list[dict]:
    out: list[dict] = []
    if request.get("system"):
        out.append({"role": "system", "content": request["system"]})
    for message in request["messages"]:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
            continue
        if message["role"] == "assistant":
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))},
                }
                for b in content
                if b.get("type") == "tool_use"
            ]
            entry: dict[str, Any] = {"role": "assistant", "content": text_of(content) or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        text = text_of(content)
        for block in content:
            if block.get("type") == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = text_of(result)
                out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": str(result)})
        if text:
            out.append({"role": "user", "content": text})
    return out


def from_openai_response(data: dict, model: str) -> dict:
    choice = data["choices"][0]
    message = choice.get("message") or {}
    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": call["function"].get("arguments")}
        blocks.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": arguments})
    finish = choice.get("finish_reason")
    stop_reason = {"tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}.get(
        finish, "end_turn"
    )
    if any(b["type"] == "tool_use" for b in blocks):
        stop_reason = "tool_use"
    usage = data.get("usage") or {}
    return {
        "content": blocks,
        "stop_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
        "model": data.get("model", model),
        "id": data.get("id"),
    }


# -- Offline model for tests, demos and CI ------------------------------------

Scripted = str | dict | Callable[[dict], Any]


class ScriptedModel:
    """A fake model that answers from a script, so workflows run offline and for free.

    ``responses`` is either a list (served in order) or a single function
    ``request -> response`` that decides from the conversation so far. A response
    can be a string (a final text answer), a full normalized response dict, or a
    list of blocks. Use :func:`call` to script tool calls.

    Prefer a function in anything you plan to resume from another process: a list
    restarts from the top in a fresh process, while a function answers from the
    conversation it is shown.
    """

    def __init__(
        self,
        responses: Iterable[Scripted] | Callable[[dict], Any],
        *,
        name: str = "scripted",
        tokens_per_call: tuple[int, int] = (1200, 300),
        price: tuple[float, float] | None = (4.00, 20.00),
        delay: float = 0.0,
    ):
        self.name = name
        self.tokens_per_call = tokens_per_call
        self.price = price
        self.delay = delay
        self.retries = 0
        self.calls = 0
        self._lock = threading.Lock()
        if callable(responses):
            self._responder: Callable[[dict], Any] = responses
        else:
            queue = iter(list(responses))

            def next_response(_request: dict) -> Any:
                try:
                    return next(queue)
                except StopIteration:
                    raise RuntimeError("ScriptedModel ran out of scripted responses") from None

            self._responder = next_response

    def complete(self, request: dict) -> dict:
        with self._lock:
            self.calls += 1
            raw = self._responder(request)
        if self.delay:
            import time

            time.sleep(self.delay)
        if callable(raw):
            raw = raw(request)
        return self._normalize(raw)

    def _normalize(self, raw: Any) -> dict:
        if isinstance(raw, str):
            raw = [{"type": "text", "text": raw}]
        if isinstance(raw, list):
            raw = {"content": raw}
        content = raw["content"]
        stop = raw.get("stop_reason") or ("tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn")
        return {
            "content": content,
            "stop_reason": stop,
            "usage": raw.get("usage")
            or {"input_tokens": self.tokens_per_call[0], "output_tokens": self.tokens_per_call[1]},
            "model": self.name,
        }

    def cost(self, response: dict) -> float:
        return priced_cost(response.get("usage") or {}, self.price)


def call(tool_name: str, /, **tool_input: Any) -> list[dict]:
    """Script a model turn that calls tool ``tool_name`` with ``tool_input``."""
    return [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:16]}", "name": tool_name, "input": tool_input}]


def calls(*parts: str | list[dict]) -> list[dict]:
    """Build one scripted model turn from text and :func:`call` parts, e.g. several
    parallel tool calls: ``calls("Checking both.", call("a"), call("b"))``."""
    blocks: list[dict] = []
    for part in parts:
        blocks.extend([{"type": "text", "text": part}] if isinstance(part, str) else part)
    return blocks
