"""Command line: inspect, resume, approve and rewind runs.

rewindable runs                         list recent runs
rewindable show RUN [--json]            step-by-step timeline of a run
rewindable step RUN KEY                 everything recorded for one step
rewindable diff RUN_A [RUN_B]           what changed between two runs (default: a rewind and its parent)
rewindable check [WORKFLOW]             replay unfinished runs against the current code, run nothing
rewindable ui                           local web UI: browse, approve, resume, rewind
rewindable start FILE.py:FN [--input J] start a workflow (or --queue it for a worker)
rewindable worker                       drive queued, due and crashed runs
rewindable pending                      approvals and signals waiting on a human
rewindable approve RUN KEY [--reject]   answer an approval and resume the run
rewindable signal RUN KEY JSON          deliver any signal and resume the run
rewindable approval-code set|clear|status   require a code only you hold to approve
rewindable resume RUN                   continue a crashed, failed or waiting run
rewindable rewind RUN --to STEP         branch a new run that re-executes from STEP
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import notify
from .diff import CHANGED, ONLY_A, ONLY_B, REUSED, SAME, RunTotals, diff_runs
from .errors import ApprovalCodeError, RunNotFound
from .runtime import Budget, CheckResult, Engine, RunResult, load_workflow
from .store import Run, StepRecord, iter_tree

_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_STATUS_COLOR = {"completed": "32", "running": "36", "waiting": "33", "failed": "31"}


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def status(text: str) -> str:
    return paint(text, _STATUS_COLOR.get(text, "0"))


def dim(text: str) -> str:
    return paint(text, "2")


def ago(ts: float) -> str:
    seconds = max(0, time.time() - ts)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds)}s ago"


def money(value: float) -> str:
    return f"${value:.4f}" if value < 1 else f"${value:.2f}"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def preview(value: Any, width: int = 60) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def step_summary(step: StepRecord) -> str:
    if step.status == "failed":
        return paint((step.error or "").strip().splitlines()[-1] if step.error else "failed", "31")
    if step.status == "running":
        if step.kind == "timer" and isinstance(step.output, dict):
            return dim(f"sleeping, wakes in {max(0, int(step.output['wake_at'] - time.time()))}s")
        return dim("in progress")
    if step.kind == "timer":
        return "slept"
    if step.kind == "patch":
        return "new code path"
    if step.kind == "llm" and isinstance(step.output, dict):
        uses = [b["name"] for b in step.output.get("content", []) if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in step.output.get("content", []) if b.get("type") == "text")
        return f"→ {', '.join(uses)}" if uses else preview(text)
    if step.kind == "parallel" and isinstance(step.output, list):
        return f"{len(step.output)} branches"
    if step.kind == "signal" and isinstance(step.output, dict) and "approved" in step.output:
        verdict = "approved" if step.output["approved"] else "rejected"
        note = step.output.get("note")
        return verdict + (f" ({note})" if note else "")
    if isinstance(step.output, dict) and set(step.output) >= {"ok"} and step.name.startswith("tool:"):
        return preview(step.output.get("value") if step.output["ok"] else "error: " + step.output["error"])
    return preview(step.output)


def print_result(result: RunResult) -> None:
    print(f"{result.run_id}  {status(result.status)}")
    if result.replayed_steps:
        print(
            dim(f"  replayed {plural(result.replayed_steps, 'step')} from the journal, saved {money(result.saved_usd)}")
        )
    if result.executed_steps:
        print(dim(f"  executed {plural(result.executed_steps, 'new step')}"))
    if result.waiting_for:
        print(f"  waiting for: {', '.join(result.waiting_for)}  (see `rewindable pending`)")
    if result.wake_at:
        wake = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.wake_at))
        print(f"  sleeping until {wake}  (`rewindable worker` wakes it)")
    if result.status == "failed" and result.error:
        print(paint("  " + result.error.strip().splitlines()[-1], "31"))
    if result.status == "completed":
        print("  output: " + preview(result.output, 200))


def cmd_runs(engine: Engine, args: argparse.Namespace) -> int:
    runs = engine.store.list_runs(status=args.status, limit=args.limit)
    if not runs:
        print("No runs yet.")
        return 0
    print(dim(f"{'RUN':18} {'STATUS':10} {'STEPS':>5} {'COST':>9}  {'STARTED':9}  WORKFLOW"))
    for run in runs:
        steps = engine.store.steps(run.id)
        cost = engine.store.usage(run.id)["cost_usd"]
        workflow = run.workflow.rsplit("/", 1)[-1]
        fork = dim(f"  ⑂ {run.parent_run_id}@{run.forked_at}") if run.parent_run_id else ""
        print(
            f"{run.id:18} {status(f'{run.status:10}')} {len(steps):>5} {money(cost):>9}  "
            f"{ago(run.created_at):9}  {workflow}{fork}"
        )
    return 0


def cmd_show(engine: Engine, args: argparse.Namespace) -> int:
    if args.json:
        from .ui import run_detail

        print(json.dumps(run_detail(engine, args.run), ensure_ascii=False, indent=2, default=str))
        return 0
    run: Run = engine.store.get_run(args.run)
    steps = engine.store.steps(run.id)
    usage = engine.store.usage(run.id)
    print(f"{paint(run.id, '1')}  {status(run.status)}  {dim(run.workflow)}")
    if run.parent_run_id:
        print(dim(f"rewound from {run.parent_run_id} at step {run.forked_at}"))
    print(dim(f"input: {preview(run.input, 100)}"))
    print()
    for step in iter_tree(steps):
        indent = "  " * step.depth
        took = f"{step.duration:.1f}s" if step.duration is not None else "…"
        cost = money(step.cost_usd) if step.cost_usd else ""
        marker = {"completed": "✓", "failed": "✗", "running": "…"}.get(step.status, "?")
        label = f"{indent}{marker} {step.key:<6} {step.name}"
        line = f"{label:<38} {dim(f'{took:>6}')} {dim(f'{cost:>8}')}  {step_summary(step)}"
        print(paint(line, "31") if step.status == "failed" else line)
    for signal in engine.store.signals(run.id, pending_only=True):
        print(paint(f"⏸ waiting for {signal.key}: {signal.prompt or ''}", "33"))
    if run.status == "waiting" and run.wake_at:
        print(paint(f"⏰ sleeping, wakes in {max(0, int(run.wake_at - time.time()))}s", "33"))
    print()
    tokens = usage["input_tokens"] + usage["output_tokens"]
    print(
        dim(f"{len(steps)} steps · {usage['llm_calls']} model calls · {tokens:,} tokens · {money(usage['cost_usd'])}")
    )
    if run.status == "completed":
        print("output: " + preview(run.output, 400))
    elif run.status == "failed" and run.error:
        print(paint(run.error.strip().splitlines()[-1], "31"))
        print(dim(f"fix the cause, then: rewindable resume {run.id}"))
    return 0


def cmd_step(engine: Engine, args: argparse.Namespace) -> int:
    run = engine.store.get_run(args.run)
    step = engine.store.get_step(run.id, args.key)
    if step is None:
        print(f"run {run.id} has no step {args.key!r}", file=sys.stderr)
        return 1
    print(f"{paint(step.key, '1')}  {step.kind} {step.name}  {status(step.status)}  attempts={step.attempts}")
    if step.duration is not None:
        print(
            dim(
                f"{step.duration:.2f}s · {step.input_tokens:,} in / {step.output_tokens:,} out · {money(step.cost_usd)}"
            )
        )
    if step.error:
        print(paint(step.error, "31"))
    if step.kind == "llm" and isinstance(step.output, dict):
        for block in step.output.get("content", []):
            if block.get("type") == "text":
                print(f"\n{dim('[text]')}\n{block['text']}")
            elif block.get("type") == "tool_use":
                print(f"\n{dim('[tool call] ' + block['name'])}\n{json.dumps(block.get('input'), indent=2)}")
            elif block.get("type") != "thinking" or block.get("thinking"):
                print(f"\n{dim('[' + str(block.get('type')) + ']')}\n{json.dumps(block, indent=2)}")
    elif step.output is not None:
        out = step.output
        print(out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=2))
    return 0


_DIFF_MARK = {REUSED: "=", SAME: "≈", CHANGED: "~", ONLY_A: "-", ONLY_B: "+"}
_DIFF_WORD = {REUSED: "reused", SAME: "same", CHANGED: "changed", ONLY_A: "only in A", ONLY_B: "only in B"}
_DIFF_COLOR = {REUSED: "2", SAME: "0", CHANGED: "33", ONLY_A: "31", ONLY_B: "32"}


def cmd_diff(engine: Engine, args: argparse.Namespace) -> int:
    try:
        first = engine.store.get_run(args.run_a)
        second = engine.store.get_run(args.run_b) if args.run_b else None
    except RunNotFound as exc:
        print(f"error: no run {exc}", file=sys.stderr)
        return 2
    if second is None:
        if not first.parent_run_id:
            print(f"error: {first.id} was not rewound from another run; give two runs to compare", file=sys.stderr)
            return 2
        first, second = engine.store.get_run(first.parent_run_id), first
    diff = diff_runs(engine.store, first.id, second.id)
    if args.json:
        print(json.dumps(diff.to_dict(), ensure_ascii=False, indent=2, default=str))
        return 0

    a, b = diff.a.run, diff.b.run
    print(f"{paint('A', '1')} {a.id}  {status(a.status)}   {paint('B', '1')} {b.id}  {status(b.status)}")
    if b.parent_run_id == a.id:
        print(dim(f"B was rewound from A at step {b.forked_at}"))
    elif a.parent_run_id == b.id:
        print(dim(f"A was rewound from B at step {a.forked_at}"))
    if a.workflow != b.workflow:
        print(dim(f"workflows differ: {a.workflow} vs {b.workflow}"))
    print()
    for step in diff.steps:
        shown = step.b or step.a
        label = f"{'  ' * step.depth}{_DIFF_MARK[step.change]} {step.key:<6} {shown.name}"
        word = _DIFF_WORD[step.change]
        if step.change == CHANGED:
            detail = _change(step.a, step.b)
        elif step.change == ONLY_A:
            detail = step_summary(step.a)
        else:
            detail = step_summary(step.b)
        line = f"{label:<38} {word:<10} {detail}"
        print(paint(line, _DIFF_COLOR[step.change]) if step.change != SAME else line)
    print()
    counts = diff.counts()
    print(dim(" · ".join(f"{counts[c]} {_DIFF_WORD[c]}" for c in _DIFF_WORD if counts[c])))
    print()
    rows = [
        ("status", status(a.status), status(b.status)),
        ("steps", _ran(diff.a), _ran(diff.b)),
        ("tokens", f"{diff.a.tokens:,}", f"{diff.b.tokens:,}"),
        ("cost", money(diff.a.cost_usd), f"{money(diff.b.cost_usd)}  ({money(diff.b.new_cost_usd)} new)"),
    ]
    rows.append(("Δ cost", "", f"{'+' if diff.cost_delta >= 0 else '-'}{money(abs(diff.cost_delta))}"))
    print(dim(f"{'':8} {'A':30} B"))
    for name, left, right in rows:
        print(f"{dim(f'{name:8}')} {left}{' ' * (30 - len(_plain(left)))} {right}")
    for side, run in (("A", a), ("B", b)):
        if run.status == "completed":
            print(f"{side} output: {preview(run.output, 160)}")
        elif run.status == "failed" and run.error:
            print(paint(f"{side} error: {run.error.strip().splitlines()[-1]}", "31"))
    return 0


def _change(a: StepRecord, b: StepRecord) -> str:
    if (a.name, a.kind) != (b.name, b.kind):
        return f"{a.kind} {a.name} → {b.kind} {b.name}"
    before, after = preview(_plain(step_summary(a)), 36), preview(_plain(step_summary(b)), 36)
    if a.status != b.status:
        before, after = f"{a.status}: {before}", f"{b.status}: {after}"
    if before == after:  # the summaries match; the difference is further in
        return f"{before}  {dim(f'(rewindable step RUN {a.key} shows each in full)')}"
    return f"{before} → {after}"


def _ran(totals: RunTotals) -> str:
    if not totals.reused:
        return f"{totals.steps} ({totals.executed} executed)"
    return f"{totals.steps} ({totals.reused} reused, {totals.executed} executed)"


def _plain(text: str) -> str:
    """``text`` without color codes."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def cmd_check(engine: Engine, args: argparse.Namespace) -> int:
    try:
        runs = [engine.store.get_run(r) for r in args.run or []]
    except RunNotFound as exc:
        print(f"error: no run {exc}", file=sys.stderr)
        return 2
    if not runs:
        for state in args.status or ["queued", "running", "waiting", "failed"]:
            runs.extend(engine.store.list_runs(status=state, limit=100_000))
        runs.sort(key=lambda r: r.created_at)
    if args.workflow:
        runs = [r for r in runs if _matches(r.workflow, args.workflow)]
    results = [engine.check(r.id) for r in runs]
    if args.json:
        print(json.dumps([{**asdict(r), "ok": r.ok} for r in results], ensure_ascii=False, indent=2))
    elif not results:
        print("No runs to check.")
    else:
        for result in results:
            print(f"{result.run_id:18} {status(f'{result.status:10}')} {_verdict(result)}")
        bad = [r for r in results if not r.ok]
        print()
        print(dim(f"{plural(len(results), 'run')} checked: {len(results) - len(bad)} ok, {len(bad)} not"))
        for result in bad:
            if result.outcome == "diverged":
                print(dim(f"  {result.run_id}: rewind it to step {result.key} to re-run from there with the new code"))
    return 1 if any(not r.ok for r in results) else 0


def _verdict(result: CheckResult) -> str:
    if result.outcome == "ok":
        replayed = plural(result.replayed_steps, "step")
        if result.resumes_at is None:
            return f"{paint('ok', '32')}  replays all {replayed} to the end"
        where = f"{result.resumes_at} ({result.next_step})"
        return f"{paint('ok', '32')}  replays {replayed}, a resume continues at {where}"
    if result.outcome == "diverged":
        return paint(f"diverges at step {result.key}: journal {result.journal}, code {result.code}", "31")
    if result.outcome == "unloadable":
        return paint(f"can't import workflow: {result.error}", "31")
    return paint(f"fails while replaying: {result.error}", "31")


def _matches(recorded: str, wanted: str) -> bool:
    """A run's workflow against ``app.py:flow``, ``app.py``, ``pkg.mod:flow`` or ``pkg.mod``."""
    path, colon, name = wanted.rpartition(":") if ":" in wanted else (wanted, "", "")
    if path.endswith(".py"):
        path = str(Path(path).resolve())
    module, _, qualname = recorded.rpartition(":")
    return module == path and (not colon or qualname == name)


def cmd_ui(engine: Engine, args: argparse.Namespace) -> int:
    from .ui import serve

    serve(engine, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_worker(engine: Engine, args: argparse.Namespace) -> int:
    if not args.once:
        print(dim(f"worker {engine.owner} watching {engine.store.path} (Ctrl+C to stop)"))
    try:
        count = engine.work(once=args.once, poll=args.poll, on_result=print_result)
    except KeyboardInterrupt:
        return 0
    if args.once:
        print(dim(f"drove {plural(count, 'run')}"))
    return 0


def cmd_start(engine: Engine, args: argparse.Namespace) -> int:
    ref = args.workflow
    path, _, name = ref.rpartition(":")
    if path.endswith(".py"):
        ref = f"{Path(path).resolve()}:{name}"
    payload = None
    if args.input is not None:
        try:
            payload = json.loads(args.input)
        except json.JSONDecodeError:
            payload = args.input
    budget = _budget(args)
    if args.queue:
        print(engine.submit(ref, payload, budget=budget))
        return 0
    workflow = load_workflow(ref)
    run = engine.store.create_run(ref, payload, budget=asdict(budget) if budget else None)
    result = engine._drive(run, workflow)
    print_result(result)
    return 0 if result.status != "failed" else 1


def cmd_pending(engine: Engine, args: argparse.Namespace) -> int:
    signals = engine.store.signals(args.run, pending_only=True)
    if not signals:
        print("Nothing is waiting on you.")
        return 0
    for s in signals:
        print(f"{s.run_id}  {paint(s.key, '33')}  {ago(s.requested_at or time.time())}")
        if s.prompt:
            print(f"    {s.prompt}")
        print(dim(f"    rewindable approve {s.run_id} {s.key}   |   --reject --note '...'"))
    return 0


def _with_code(deliver: Any) -> None:
    """Deliver; if that needs the approval code, ask for it (never echoed) and retry."""
    try:
        deliver(None)
    except ApprovalCodeError:
        deliver(getpass.getpass("Approval code: "))


def cmd_approve(engine: Engine, args: argparse.Namespace) -> int:
    run = engine.store.get_run(args.run)
    _with_code(lambda code: engine.approve(run.id, args.key, approved=not args.reject, note=args.note or "", code=code))
    print(f"{'Rejected' if args.reject else 'Approved'} {args.key} on {run.id}.")
    if args.no_resume:
        return 0
    print_result(engine.resume(run.id))
    return 0


def cmd_signal(engine: Engine, args: argparse.Namespace) -> int:
    run = engine.store.get_run(args.run)
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError:
        payload = args.payload
    _with_code(lambda code: engine.signal(run.id, args.key, payload, code=code))
    print(f"Delivered {args.key} to {run.id}.")
    if not args.no_resume:
        print_result(engine.resume(run.id))
    return 0


def cmd_approval_code(engine: Engine, args: argparse.Namespace) -> int:
    on = engine.approval_code_required
    if args.action == "status":
        print(
            "Approving needs the approval code."
            if on
            else "No approval code is set: anyone who can reach the journal can approve."
        )
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # The code has to be seen by a person and by nothing else, so no script,
        # pipe or agent tool call gets to set or clear it.
        print("approval-code set/clear only runs in an interactive terminal.", file=sys.stderr)
        return 2
    current = getpass.getpass("Current approval code: ") if on else None
    if args.action == "clear":
        if not on:
            print("No approval code is set.")
            return 0
        engine.clear_approval_code(current or "")
        print("Approval code removed. Approving no longer needs it.")
        return 0
    code = engine.set_approval_code(current=current)
    print(f"\n    {paint(code, '1')}\n")
    print("This is shown once. Keep it where your agents cannot read it: not in a file, an")
    print("environment variable or a prompt. Approving now needs it; rejecting does not.")
    print(dim("Lost it? Delete the approval_code row from the journal's settings table by hand."))
    return 0


def _budget(args: argparse.Namespace) -> Budget | None:
    if args.max_cost is None and args.max_tokens is None:
        return None
    return Budget(max_cost_usd=args.max_cost, max_tokens=args.max_tokens)


def cmd_resume(engine: Engine, args: argparse.Namespace) -> int:
    result = engine.resume(args.run, budget=_budget(args))
    print_result(result)
    return 0 if result.status != "failed" else 1


def cmd_rewind(engine: Engine, args: argparse.Namespace) -> int:
    kwargs: dict[str, Any] = {"budget": _budget(args)}
    if args.input is not None:
        kwargs["input"] = json.loads(args.input)
    result = engine.rewind(args.run, args.to, **kwargs)
    print(dim(f"rewound {args.run} to step {args.to}"))
    print_result(result)
    return 0 if result.status != "failed" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rewindable", description="Inspect, resume, approve and rewind durable agent runs."
    )
    parser.add_argument("--db", help="journal path (default: $REWINDABLE_DB or .rewindable/runs.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("runs", help="list recent runs")
    p.add_argument("--status", choices=["queued", "running", "waiting", "completed", "failed"])
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_runs)

    p = sub.add_parser("show", help="show a run's step timeline")
    p.add_argument("run", help="run id (a unique prefix is enough)")
    p.add_argument("--json", action="store_true", help="print the run and every step as JSON")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("step", help="print everything recorded for one step")
    p.add_argument("run")
    p.add_argument("key", help="step key, as shown by `show`")
    p.set_defaults(fn=cmd_step)

    p = sub.add_parser("diff", help="compare two runs step by step (one run: compare a rewind with its parent)")
    p.add_argument("run_a", metavar="RUN_A")
    p.add_argument("run_b", metavar="RUN_B", nargs="?", help="default: the run RUN_A was rewound from, then RUN_A")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser("check", help="replay runs against the current code without running anything new")
    p.add_argument("workflow", nargs="?", help="only runs of this workflow: file.py[:fn] or module[:fn]")
    p.add_argument("--run", action="append", help="check this run (repeatable); default: every unfinished run")
    p.add_argument(
        "--status",
        action="append",
        choices=["queued", "running", "waiting", "failed", "completed"],
        help="check runs with this status (repeatable; default: all but completed)",
    )
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("ui", help="open the local web UI")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    _notify_arg(p)
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("worker", help="drive queued runs, due timers, answered approvals and crashed runs")
    p.add_argument("--once", action="store_true", help="drive what is ready now, then exit")
    p.add_argument("--poll", type=float, default=1.0, help="seconds between checks")
    _notify_arg(p)
    p.set_defaults(fn=cmd_worker)

    p = sub.add_parser("start", help="start a workflow: path/to/file.py:function or package.module:function")
    p.add_argument("workflow")
    p.add_argument("--input", help="input as JSON (plain text is passed as a string)")
    p.add_argument("--queue", action="store_true", help="only queue it; a worker will run it")
    p.add_argument("--max-cost", type=float)
    p.add_argument("--max-tokens", type=int)
    _notify_arg(p)
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("pending", help="list approvals and signals waiting on a human")
    p.add_argument("run", nargs="?")
    p.set_defaults(fn=cmd_pending)

    p = sub.add_parser("approve", help="approve (or --reject) a pending approval and resume")
    p.add_argument("run")
    p.add_argument("key")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note", help="explanation passed back to the agent")
    p.add_argument("--no-resume", action="store_true", help="record the answer without resuming")
    _notify_arg(p)
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("signal", help="deliver a signal payload (JSON) and resume")
    p.add_argument("run")
    p.add_argument("key")
    p.add_argument("payload")
    p.add_argument("--no-resume", action="store_true")
    _notify_arg(p)
    p.set_defaults(fn=cmd_signal)

    p = sub.add_parser("approval-code", help="require a code only you hold to approve (set, clear, status)")
    p.add_argument("action", choices=["set", "clear", "status"])
    p.set_defaults(fn=cmd_approval_code)

    for name, fn, helptext in (
        ("resume", cmd_resume, "continue a crashed, failed or waiting run"),
        ("rewind", cmd_rewind, "branch a new run that re-executes from a step"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("run")
        if name == "rewind":
            p.add_argument("--to", required=True, help="step key to re-execute from, as shown by `show`")
            p.add_argument("--input", help="replace the run's input (JSON)")
        p.add_argument("--max-cost", type=float, help="new cost budget in USD")
        p.add_argument("--max-tokens", type=int, help="new token budget")
        _notify_arg(p)
        p.set_defaults(fn=fn)
    return parser


def _notify_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--notify-url",
        help=f"POST to this URL when a run starts waiting for a human (default: ${notify.ENV}; Slack webhooks work)",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = Engine(args.db)
    if hasattr(args, "notify_url"):  # only commands that drive runs take it
        url = args.notify_url or os.environ.get(notify.ENV)
        engine.on_waiting = notify.webhook(url) if url else None
    try:
        return args.fn(engine, args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(paint(f"error: {exc}", "31"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
