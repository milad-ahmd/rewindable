"""The durable execution engine: run, replay, suspend, resume and rewind workflows."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import (
    BudgetExceeded,
    NestedStepError,
    NonDeterminismError,
    Suspended,
    WorkflowNotFound,
)
from .store import Run, Store, dumps

if TYPE_CHECKING:
    from .models import Model

Workflow = Callable[..., Any]


@dataclass
class Budget:
    """Hard limits for one run, checked before every model call.

    Usage is counted across the whole run, including steps replayed from the
    journal, so a run cannot dodge its budget by crashing and resuming.
    """

    max_cost_usd: float | None = None
    max_tokens: int | None = None
    max_llm_calls: int | None = None

    def check(self, usage: dict) -> None:
        if self.max_cost_usd is not None and usage["cost_usd"] >= self.max_cost_usd:
            raise BudgetExceeded(f"cost ${usage['cost_usd']:.4f} reached the ${self.max_cost_usd:.4f} budget")
        tokens = usage["input_tokens"] + usage["output_tokens"]
        if self.max_tokens is not None and tokens >= self.max_tokens:
            raise BudgetExceeded(f"{tokens} tokens reached the {self.max_tokens}-token budget")
        if self.max_llm_calls is not None and usage["llm_calls"] >= self.max_llm_calls:
            raise BudgetExceeded(f"{usage['llm_calls']} model calls reached the limit of {self.max_llm_calls}")


@dataclass(frozen=True)
class Approval:
    approved: bool
    note: str = ""

    def __bool__(self) -> bool:
        return self.approved


@dataclass
class RunResult:
    run_id: str
    status: str
    output: Any = None
    error: str | None = None
    waiting_for: list[str] = field(default_factory=list)
    executed_steps: int = 0
    replayed_steps: int = 0
    saved_usd: float = 0.0
    cost_usd: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def __repr__(self) -> str:
        bits = [f"run_id={self.run_id!r}", f"status={self.status!r}"]
        if self.waiting_for:
            bits.append(f"waiting_for={self.waiting_for!r}")
        if self.error:
            bits.append(f"error={self.error.strip().splitlines()[-1]!r}")
        bits.append(f"executed={self.executed_steps} replayed={self.replayed_steps}")
        return f"RunResult({', '.join(bits)})"


class _Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.executed = 0
        self.replayed = 0
        self.saved_usd = 0.0


class Context:
    """Handed to every workflow. Every call that has a side effect or costs money
    should go through it, so that it is journaled and replayed instead of repeated."""

    def __init__(self, store: Store, run: Run, *, prefix: str = "", stats: _Stats | None = None):
        self._store = store
        self._run = run
        self._prefix = prefix
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._local = threading.local()
        self._stats = stats or _Stats()

    @property
    def run_id(self) -> str:
        return self._run.id

    @property
    def input(self) -> Any:
        return self._run.input

    # -- core step primitive -----------------------------------------------

    def _next_key(self) -> str:
        with self._counter_lock:
            self._counter += 1
            return f"{self._prefix}{self._counter}"

    def _replay(self, key: str, name: str, kind: str):
        """Return the journaled record for ``key`` if it completed, else None."""
        rec = self._store.get_step(self.run_id, key)
        if rec is None or rec.status != "completed":
            return None
        if rec.name != name or rec.kind != kind:
            raise NonDeterminismError(
                f"step {key} was {rec.kind} {rec.name!r} in the journal but the code now calls"
                f" {kind} {name!r}. If you changed the workflow on purpose, rewind to step {key}."
            )
        count, cost = 1, rec.cost_usd
        if kind == "parallel":  # the group's branches are skipped along with it
            nested = self._store.subtree_usage(self.run_id, key)
            count, cost = count + nested["steps"], cost + nested["cost_usd"]
        with self._stats.lock:
            self._stats.replayed += count
            self._stats.saved_usd += cost
        return rec

    def step(
        self,
        name: str,
        fn: Callable[..., Any],
        *args: Any,
        retries: int = 0,
        backoff: float = 0.5,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn(*args, **kwargs)`` once, ever, for this position in the workflow.

        The return value must be JSON-serializable; it is stored in the journal and
        returned as-is (after a JSON round trip) on every replay.
        """
        return self._execute(name, "step", fn, args, kwargs, retries=retries, backoff=backoff)

    def _execute(
        self,
        name: str,
        kind: str,
        fn: Callable[..., Any],
        args: Sequence[Any] = (),
        kwargs: dict | None = None,
        *,
        retries: int = 0,
        backoff: float = 0.5,
        accounting: Callable[[Any], dict] | None = None,
    ) -> Any:
        if getattr(self._local, "in_step", False):
            raise NestedStepError(f"step {name!r} was opened inside another step")
        key = self._next_key()
        rec = self._replay(key, name, kind)
        if rec is not None:
            return rec.output

        if kind == "llm":
            self.budget_check()
        self._store.begin_step(self.run_id, key, name, kind)
        attempt = 0
        while True:
            attempt += 1
            self._local.in_step = True
            try:
                value = fn(*args, **(kwargs or {}))
                output = json.loads(dumps(value))
            except Exception as exc:
                if attempt <= retries:
                    time.sleep(backoff * 2 ** (attempt - 1))
                    continue
                self._store.fail_step(self.run_id, key, _format_exc(exc), attempts=attempt)
                raise
            finally:
                self._local.in_step = False
            break

        extra = accounting(output) if accounting else {}
        self._store.complete_step(self.run_id, key, output, attempts=attempt, **extra)
        with self._stats.lock:
            self._stats.executed += 1
        return output

    # -- model calls -------------------------------------------------------

    def llm(
        self,
        model: Model,
        *,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        name: str | None = None,
        **params: Any,
    ) -> dict:
        """Call a model as a journaled step. Returns the normalized response dict:
        ``{"content": [...blocks], "stop_reason": str, "usage": {...}, "model": str}``.
        Checks the run's budget first and records tokens and cost.
        """
        request: dict[str, Any] = {"messages": messages, **params}
        if system:
            request["system"] = system
        if tools:
            request["tools"] = tools

        def accounting(response: dict) -> dict:
            usage = response.get("usage") or {}
            return {
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "cost_usd": model.cost(response),
            }

        return self._execute(
            name or f"llm:{model.name}",
            "llm",
            model.complete,
            (request,),
            retries=getattr(model, "retries", 2),
            accounting=accounting,
        )

    def budget_check(self) -> None:
        budget = self._store.get_run(self.run_id).budget
        if budget:
            Budget(**budget).check(self._store.usage(self.run_id))

    # -- signals and humans -------------------------------------------------

    def wait_for(self, signal: str, prompt: str | None = None) -> Any:
        """Pause the run until ``signal`` is delivered, then return its payload.

        The first time this is reached without a payload, the run is saved as
        ``waiting`` and the process is free to exit. Deliver the payload with
        ``engine.signal(...)`` or ``rewindable signal`` and resume the run.
        """
        name = f"signal:{signal}"
        key = self._next_key()
        rec = self._replay(key, name, "signal")
        if rec is not None:
            return rec.output
        delivered = self._store.get_signal(self.run_id, signal)
        if delivered is not None and delivered.delivered_at is not None:
            self._store.begin_step(self.run_id, key, name, "signal")
            self._store.complete_step(self.run_id, key, delivered.payload)
            with self._stats.lock:
                self._stats.executed += 1
            return delivered.payload
        self._store.request_signal(self.run_id, signal, prompt)
        raise Suspended([signal])

    def approve(self, question: str, *, key: str | None = None) -> Approval:
        """Ask a human to approve something. Suspends the run until they answer.

        ``key`` defaults to one derived from this call's position in the
        workflow, which is stable across replays.
        """
        with self._counter_lock:
            position = f"{self._prefix}{self._counter + 1}"
        payload = self.wait_for(key or f"approve-{position}", prompt=question)
        if isinstance(payload, dict):
            return Approval(bool(payload.get("approved")), str(payload.get("note") or ""))
        return Approval(bool(payload))

    # -- fan-out -----------------------------------------------------------

    def parallel(
        self,
        branches: Sequence[Callable[[Context], Any]],
        *,
        name: str = "parallel",
        max_workers: int | None = None,
    ) -> list[Any]:
        """Run each ``branch(ctx)`` concurrently and return their results in order.

        Every branch gets its own child context, so its steps are journaled under
        their own keys and replay independently. If one branch waits on a human,
        the others still finish and are saved before the run suspends.
        """
        if getattr(self._local, "in_step", False):
            raise NestedStepError(f"parallel {name!r} was opened inside a step")
        key = self._next_key()
        rec = self._replay(key, name, "parallel")
        if rec is not None:
            return rec.output
        self._store.begin_step(self.run_id, key, name, "parallel")
        if not branches:
            self._store.complete_step(self.run_id, key, [])
            return []

        def run_branch(index: int, branch: Callable[[Context], Any]) -> Any:
            child = Context(self._store, self._run, prefix=f"{key}.{index}.", stats=self._stats)
            return json.loads(dumps(branch(child)))

        results: list[Any] = [None] * len(branches)
        waiting: list[str] = []
        errors: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=max_workers or min(8, len(branches))) as pool:
            futures = [pool.submit(run_branch, i, b) for i, b in enumerate(branches)]
            for i, future in enumerate(futures):
                try:
                    results[i] = future.result()
                except Suspended as s:
                    waiting.extend(s.keys)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)
        if errors:
            self._store.fail_step(self.run_id, key, _format_exc(errors[0]), attempts=1)
            raise errors[0]
        if waiting:
            raise Suspended(waiting)
        self._store.complete_step(self.run_id, key, results)
        with self._stats.lock:
            self._stats.executed += 1
        return results

    # -- deterministic helpers ---------------------------------------------

    def now(self) -> float:
        """Wall-clock time, recorded once so replays see the same value."""
        return self._execute("now", "step", time.time)

    def uuid(self) -> str:
        """A random id, recorded once so replays see the same value."""
        return self._execute("uuid", "step", lambda: uuid.uuid4().hex)


class Engine:
    """Entry point: starts runs and brings them back."""

    def __init__(self, db: str | Path | None = None, *, store: Store | None = None):
        self.store = store or Store(db)

    # -- lifecycle ---------------------------------------------------------

    def run(
        self,
        workflow: Workflow,
        input: Any = None,
        *,
        run_id: str | None = None,
        budget: Budget | None = None,
    ) -> RunResult:
        """Start a new run of ``workflow`` and drive it until it completes, fails or waits."""
        run = self.store.create_run(
            workflow_ref(workflow), input, run_id=run_id, budget=asdict(budget) if budget else None
        )
        return self._drive(run, workflow)

    def resume(self, run_id: str, workflow: Workflow | None = None, *, budget: Budget | None = None) -> RunResult:
        """Continue a run that crashed, failed or was waiting. Completed steps are
        replayed from the journal; everything after them runs for real."""
        run = self.store.get_run(run_id)
        if run.status == "completed":
            return self._result(run, _Stats())
        if budget is not None:
            self.store.set_budget(run.id, asdict(budget))
            run = self.store.get_run(run.id)
        return self._drive(run, workflow or load_workflow(run.workflow))

    def rewind(
        self,
        run_id: str,
        to: str,
        workflow: Workflow | None = None,
        *,
        input: Any = ...,
        budget: Budget | None = None,
    ) -> RunResult:
        """Branch a new run that keeps every step before ``to`` and re-executes from there.

        The original run is left untouched. Typical use: a tool had a bug at step 7;
        fix the code and rewind to step 7 without paying for steps 1-6 again.
        """
        child = self.store.fork(run_id, to, input=input, budget=asdict(budget) if budget else ...)
        return self._drive(child, workflow or load_workflow(child.workflow))

    def signal(self, run_id: str, key: str, payload: Any = None) -> None:
        run = self.store.get_run(run_id)
        self.store.deliver_signal(run.id, key, payload)

    def approve(self, run_id: str, key: str, *, approved: bool = True, note: str = "") -> None:
        self.signal(run_id, key, {"approved": approved, "note": note})

    def _drive(self, run: Run, workflow: Workflow) -> RunResult:
        stats = _Stats()
        ctx = Context(self.store, run, stats=stats)
        self.store.set_status(run.id, "running")
        try:
            value = workflow(ctx, run.input) if _takes_input(workflow) else workflow(ctx)
            output = json.loads(dumps(value))
        except Suspended:
            self.store.set_status(run.id, "waiting")
        except Exception as exc:  # noqa: BLE001 - recorded in the journal
            self.store.set_status(run.id, "failed", error=_format_exc(exc))
        else:
            self.store.set_status(run.id, "completed", output=output)
        return self._result(self.store.get_run(run.id), stats)

    def _result(self, run: Run, stats: _Stats) -> RunResult:
        return RunResult(
            run_id=run.id,
            status=run.status,
            output=run.output,
            error=run.error,
            waiting_for=[s.key for s in self.store.signals(run.id, pending_only=True)],
            executed_steps=stats.executed,
            replayed_steps=stats.replayed,
            saved_usd=stats.saved_usd,
            cost_usd=self.store.usage(run.id)["cost_usd"],
        )


# -- workflow references ------------------------------------------------------


def workflow_ref(fn: Workflow) -> str:
    """A string that lets another process import ``fn`` again: ``module:qualname``,
    or ``/path/to/script.py:qualname`` for workflows defined in a script."""
    module = getattr(fn, "__module__", None) or "__main__"
    qualname = getattr(fn, "__qualname__", getattr(fn, "__name__", "workflow"))
    if module == "__main__":
        main_file = getattr(sys.modules["__main__"], "__file__", None)
        if main_file:
            return f"{Path(main_file).resolve()}:{qualname}"
    return f"{module}:{qualname}"


def load_workflow(ref: str) -> Workflow:
    path_or_module, _, qualname = ref.rpartition(":")
    if "<locals>" in qualname:
        raise WorkflowNotFound(f"{ref} is defined inside a function; pass the workflow to resume() explicitly")
    try:
        if path_or_module.endswith(".py"):
            path = Path(path_or_module)
            module_name = "rewindable_wf_" + hashlib.sha1(str(path).encode()).hexdigest()[:10]
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise WorkflowNotFound(f"cannot load {path}")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                sys.path.insert(0, str(path.parent))
                spec.loader.exec_module(module)
        else:
            if "" not in sys.path:
                sys.path.insert(0, "")
            module = importlib.import_module(path_or_module)
        obj: Any = module
        for part in qualname.split("."):
            obj = getattr(obj, part)
        return obj
    except WorkflowNotFound:
        raise
    except Exception as exc:
        raise WorkflowNotFound(f"cannot import workflow {ref}: {exc}") from exc


def _takes_input(fn: Workflow) -> bool:
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return True
    positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 2 or any(p.kind == p.VAR_POSITIONAL for p in params)


def _format_exc(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
