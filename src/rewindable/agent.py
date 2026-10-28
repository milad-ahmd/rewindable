"""Tool-using agents whose every model call and tool call is a durable step."""

from __future__ import annotations

import inspect
import json
import re
import time
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

from .errors import BudgetExceeded, NonDeterminismError, Suspended
from .models import Model, text_of
from .runtime import Context

# -- tools ------------------------------------------------------------------


@dataclass
class Tool:
    """A function the model may call.

    ``durable`` tools receive the run's context as their first argument and run
    outside a step, so they can open steps of their own (sub-agents, approvals,
    fan-out). Ordinary tools run inside a single journaled step.
    """

    name: str
    description: str
    input_schema: dict
    fn: Callable[..., Any]
    requires_approval: bool = False
    retries: int = 0
    durable: bool = False

    def definition(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    requires_approval: bool = False,
    retries: int = 0,
) -> Any:
    """Turn a typed Python function into a :class:`Tool`.

    The JSON schema comes from the type hints, the description from the docstring,
    and per-argument descriptions from a Google-style ``Args:`` section::

        @tool(requires_approval=True)
        def send_email(to: str, subject: str, body: str) -> str:
            \"\"\"Send an email.

            Args:
                to: Recipient address.
            \"\"\"
    """

    def wrap(f: Callable[..., Any]) -> Tool:
        summary, arg_docs = _parse_docstring(inspect.getdoc(f) or "")
        return Tool(
            name=name or f.__name__,
            description=description or summary or f.__name__.replace("_", " "),
            input_schema=_schema_for(f, arg_docs),
            fn=f,
            requires_approval=requires_approval,
            retries=retries,
        )

    return wrap(fn) if fn is not None else wrap


def _parse_docstring(doc: str) -> tuple[str, dict[str, str]]:
    summary_lines: list[str] = []
    args: dict[str, str] = {}
    section = "summary"
    current: str | None = None
    for line in doc.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"(Args|Arguments|Parameters):", stripped):
            section = "args"
            continue
        if re.fullmatch(r"[A-Z][a-z]+:", stripped):
            section = "other"
            continue
        if section == "summary":
            summary_lines.append(stripped)
        elif section == "args" and stripped:
            match = re.match(r"(\w+)(?:\s*\(.*?\))?:\s*(.*)", stripped)
            if match:
                current = match.group(1)
                args[current] = match.group(2)
            elif current:
                args[current] += " " + stripped
    return " ".join(" ".join(summary_lines).split()), args


_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _json_type(hint: Any) -> dict:
    if hint is inspect.Parameter.empty or hint is Any:
        return {}
    origin = get_origin(hint)
    if origin is Literal:
        values = list(get_args(hint))
        return {"enum": values, **_json_type(type(values[0]))}
    if origin in (Union, getattr(types, "UnionType", Union)):
        options = [a for a in get_args(hint) if a is not type(None)]
        return _json_type(options[0]) if len(options) == 1 else {"anyOf": [_json_type(a) for a in options]}
    if origin in (list, tuple, set, Sequence):
        item_args = get_args(hint)
        return {"type": "array", **({"items": _json_type(item_args[0])} if item_args else {})}
    if origin is dict:
        return {"type": "object"}
    if hint in _JSON_TYPES:
        return {"type": _JSON_TYPES[hint]}
    return {}


def _schema_for(fn: Callable[..., Any], arg_docs: dict[str, str], skip: int = 0) -> dict:
    try:
        hints = get_type_hints(fn)
    except Exception:  # noqa: BLE001 - unresolvable annotations fall back to untyped
        hints = {}
    properties: dict[str, dict] = {}
    required: list[str] = []
    params = list(inspect.signature(fn).parameters.values())[skip:]
    for p in params:
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        prop = _json_type(hints.get(p.name, p.annotation))
        if p.name in arg_docs:
            prop["description"] = arg_docs[p.name]
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
        else:
            prop.setdefault("default", p.default)
        properties[p.name] = prop
    return {"type": "object", "properties": properties, "required": required}


# -- agent ------------------------------------------------------------------


@dataclass
class AgentResult:
    text: str
    stop_reason: str
    turns: int
    messages: list[dict] = field(default_factory=list, repr=False)


class Agent:
    """A model, instructions and tools, run as a loop of durable steps.

    Each model call is an ``llm`` step and each tool call a ``tool:<name>`` step,
    so a crashed or paused agent resumes mid-conversation without repeating work.
    Several tool calls in one turn run concurrently through ``ctx.parallel``.
    """

    def __init__(
        self,
        name: str,
        model: Model,
        *,
        instructions: str = "",
        tools: Sequence[Tool | Callable[..., Any]] = (),
        max_turns: int = 20,
    ):
        self.name = name
        self.model = model
        self.instructions = instructions
        self.tools = {t.name: t for t in (x if isinstance(x, Tool) else tool(x) for x in tools)}
        self.max_turns = max_turns

    def run(self, ctx: Context, task: str | list[dict]) -> AgentResult:
        messages: list[dict] = [{"role": "user", "content": task}] if isinstance(task, str) else list(task)
        definitions = [t.definition() for t in self.tools.values()] or None
        for turn in range(1, self.max_turns + 1):
            response = ctx.llm(
                self.model,
                messages=messages,
                system=self.instructions or None,
                tools=definitions,
                name=f"{self.name}:llm",
            )
            messages.append({"role": "assistant", "content": response["content"]})
            uses = [b for b in response["content"] if b.get("type") == "tool_use"]
            if response["stop_reason"] == "pause_turn":
                continue
            if not uses or response["stop_reason"] in ("refusal", "max_tokens"):
                return AgentResult(text_of(response), response["stop_reason"] or "end_turn", turn, messages)
            if len(uses) == 1:
                results = [self._call_tool(ctx, uses[0])]
            else:
                results = ctx.parallel(
                    [lambda c, use=use: self._call_tool(c, use) for use in uses],
                    name=f"{self.name}:tools",
                )
            messages.append({"role": "user", "content": results})
        last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
        return AgentResult(text_of(last["content"]) if last else "", "max_turns", self.max_turns, messages)

    def _call_tool(self, ctx: Context, use: dict) -> dict:
        name, arguments = use["name"], use.get("input") or {}
        found = self.tools.get(name)
        if found is None:
            return _tool_result(use, f"Unknown tool {name!r}. Available: {', '.join(self.tools)}", error=True)
        if found.requires_approval:
            args_preview = json.dumps(arguments, ensure_ascii=False)
            decision = ctx.approve(f"{self.name} wants to call {name}({args_preview})")
            if not decision.approved:
                reason = f" Reason: {decision.note}" if decision.note else ""
                return _tool_result(use, f"A human declined this call.{reason}", error=True)
        if found.durable:
            try:
                value = found.fn(ctx, **arguments)
            except (Suspended, BudgetExceeded, NonDeterminismError):
                raise
            except Exception as exc:  # noqa: BLE001 - reported back to the model
                return _tool_result(use, f"{type(exc).__name__}: {exc}", error=True)
            return _tool_result(use, value)
        outcome = ctx.step(f"tool:{name}", _guarded, found, arguments)
        if outcome["ok"]:
            return _tool_result(use, outcome["value"])
        return _tool_result(use, outcome["error"], error=True)

    def as_tool(self, name: str | None = None, description: str | None = None) -> Tool:
        """Expose this agent as a tool another agent can delegate to."""

        def delegate(ctx: Context, task: str) -> str:
            return self.run(ctx, task).text

        return Tool(
            name=name or self.name,
            description=description or f"Hand a self-contained task to the {self.name} agent and get its answer back.",
            input_schema={
                "type": "object",
                "properties": {"task": {"type": "string", "description": "The full task, with any context it needs."}},
                "required": ["task"],
            },
            fn=delegate,
            durable=True,
        )


def _guarded(found: Tool, arguments: dict) -> dict:
    """Run a tool and turn failure into data, so the journal records exactly what the model saw."""
    for attempt in range(found.retries + 1):
        try:
            return {"ok": True, "value": found.fn(**arguments)}
        except Exception as exc:  # noqa: BLE001 - reported back to the model
            if attempt < found.retries:
                time.sleep(0.5 * 2**attempt)
                continue
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    raise AssertionError("unreachable")


def _tool_result(use: dict, value: Any, *, error: bool = False) -> dict:
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    block = {"type": "tool_result", "tool_use_id": use["id"], "content": content}
    if error:
        block["is_error"] = True
    return block
