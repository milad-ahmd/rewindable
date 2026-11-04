"""Compare two runs step by step: what a rewind kept, re-ran and changed."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .store import Run, StepRecord, Store

# How each step key of run B relates to run A.
REUSED = "reused"  # copied into B from A by a rewind: one execution, shared
SAME = "same"  # executed again in B, with the same result
CHANGED = "changed"  # executed again, and the result, status or step differs
ONLY_A = "only_a"
ONLY_B = "only_b"
CHANGES = (REUSED, SAME, CHANGED, ONLY_A, ONLY_B)


@dataclass(frozen=True)
class StepDiff:
    key: str
    change: str  # one of CHANGES
    a: StepRecord | None
    b: StepRecord | None

    @property
    def depth(self) -> int:
        return self.key.count(".") // 2


@dataclass(frozen=True)
class RunTotals:
    """One side of a diff. ``reused`` steps were copied in by a rewind (they started
    before the run was created); ``executed`` steps ran in this run."""

    run: Run
    steps: int
    executed: int
    reused: int
    tokens: int
    cost_usd: float
    new_cost_usd: float  # cost of the executed steps only


@dataclass
class RunDiff:
    a: RunTotals
    b: RunTotals
    steps: list[StepDiff] = field(default_factory=list)

    @property
    def cost_delta(self) -> float:
        return self.b.cost_usd - self.a.cost_usd

    def counts(self) -> dict[str, int]:
        found = {change: 0 for change in CHANGES}
        for step in self.steps:
            found[step.change] += 1
        return found

    def to_dict(self) -> dict:
        return {
            "a": _totals_dict(self.a),
            "b": _totals_dict(self.b),
            "cost_delta_usd": self.cost_delta,
            "counts": self.counts(),
            "steps": [
                {
                    "key": s.key,
                    "change": s.change,
                    "depth": s.depth,
                    "a": _step_dict(s.a),
                    "b": _step_dict(s.b),
                }
                for s in self.steps
            ],
        }


def diff_runs(store: Store, a: str, b: str) -> RunDiff:
    """Align the steps of runs ``a`` and ``b`` by key and classify each one.

    Typical use: ``a`` is a run and ``b`` the run rewound from it. Steps the rewind
    copied are ``reused``; steps that ran again are ``same`` or ``changed``. Model
    outputs are compared by content and stop reason, ignoring token counts and the
    random tool-call ids a model hands out.
    """
    run_a, run_b = store.get_run(a), store.get_run(b)
    steps_a = {s.key: s for s in store.steps(run_a.id)}
    steps_b = {s.key: s for s in store.steps(run_b.id)}
    keys = sorted(set(steps_a) | set(steps_b), key=_key_order)
    steps = [StepDiff(k, _classify(steps_a.get(k), steps_b.get(k)), steps_a.get(k), steps_b.get(k)) for k in keys]
    return RunDiff(_totals(store, run_a, steps_a), _totals(store, run_b, steps_b), steps)


def _classify(a: StepRecord | None, b: StepRecord | None) -> str:
    if b is None:
        return ONLY_A
    if a is None:
        return ONLY_B
    if (a.name, a.kind, a.status) != (b.name, b.kind, b.status):
        return CHANGED
    if comparable(a) != comparable(b):
        return CHANGED
    # Store.fork copies a row as is, start time included: same start, same execution.
    return REUSED if a.started_at == b.started_at else SAME


def comparable(step: StepRecord) -> Any:
    """The part of a step's result that says what happened."""
    if step.status == "failed":
        return (step.error or "").strip().splitlines()[-1:]
    output = step.output
    if step.kind == "llm" and isinstance(output, dict):
        output = {"content": output.get("content"), "stop_reason": output.get("stop_reason")}
    return _without_ids(output)


def _without_ids(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_ids(v) for v in value]
    if isinstance(value, dict):
        drop = {"tool_use": "id", "tool_result": "tool_use_id"}.get(value.get("type"))
        return {k: _without_ids(v) for k, v in value.items() if k != drop}
    return value


def _totals(store: Store, run: Run, steps: dict[str, StepRecord]) -> RunTotals:
    usage = store.usage(run.id)
    copied = [s for s in steps.values() if s.started_at < run.created_at]
    return RunTotals(
        run=run,
        steps=len(steps),
        executed=len(steps) - len(copied),
        reused=len(copied),
        tokens=usage["input_tokens"] + usage["output_tokens"],
        cost_usd=usage["cost_usd"],
        new_cost_usd=usage["cost_usd"] - sum(s.cost_usd for s in copied),
    )


def _totals_dict(t: RunTotals) -> dict:
    return {
        "id": t.run.id,
        "workflow": t.run.workflow,
        "status": t.run.status,
        "parent_run_id": t.run.parent_run_id,
        "forked_at": t.run.forked_at,
        "output": t.run.output,
        "error": t.run.error,
        "steps": t.steps,
        "executed": t.executed,
        "reused": t.reused,
        "tokens": t.tokens,
        "cost_usd": t.cost_usd,
        "new_cost_usd": t.new_cost_usd,
    }


def _step_dict(s: StepRecord | None) -> dict | None:
    if s is None:
        return None
    return {
        "name": s.name,
        "kind": s.kind,
        "status": s.status,
        "output": s.output,
        "error": s.error,
        "cost_usd": s.cost_usd,
        "started_at": s.started_at,
    }


def _key_order(key: str) -> tuple[int, ...]:
    return tuple(int(p) for p in key.split("."))
