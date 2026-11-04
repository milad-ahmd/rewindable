# Changelog

## 0.3.0

- First release on PyPI: `pip install rewindable`
- `rewindable diff RUN_A [RUN_B]`: compares two runs step by step (one run: a rewind and its parent) and marks each step reused, re-run with the same result, changed, or only in one run, with steps, tokens and cost per run. Also `diff_runs(store, a, b)`, `--json`, `/api/runs/<a>/diff/<b>` and a "compare with parent" view in the web UI.
- `rewindable check [WORKFLOW]`: replays unfinished runs against the current code without executing anything (no steps, model calls, signals, timers or journal writes) and reports the ones that would fail with `NonDeterminismError`, with the step key and both step names. Exits 1 if any run would break. Also `Engine.check(run_id) -> CheckResult`.
- `ctx.patched("change-id")`: change a workflow without breaking runs in flight; new runs take the new path, runs already past that point keep the old one.
- Waiting notifications: `Engine(on_waiting=...)` is called once per approval or signal a run starts waiting on; `--notify-url URL` (or `REWINDABLE_NOTIFY_URL`) on `start`, `resume`, `rewind`, `approve`, `signal`, `worker` and `ui` POSTs it as JSON, or as a Slack message for Slack webhooks. Failures only print a warning. Journals get a `signals.notified_at` column on open.
- `NonDeterminismError` now carries `key`, `journal` and `code`.
- Approval code: `rewindable approval-code set` makes approving need a code only a human holds, so an agent with a shell tool can no longer approve its own gated calls. The CLI prompts for it, the web UI has a code field, and `Engine.approve`/`signal` take `code=`. Rejecting never needs it; only its SHA-256 is stored; approvals that skip the check are read as rejections. Inspired by the decider code in [life-distributable](https://github.com/mattabate/life-distributable).
- Fix: `rewind --to <parallel group>` replayed branches that finished before the group was resumed, so a tool inside the group was never re-run. Rewinding to a step now always re-runs everything inside it. Reported by [@mattabate](https://github.com/mattabate) in [#1](https://github.com/milad-ahmd/rewindable/issues/1).

## 0.2.0

- Release workflow for PyPI with trusted publishing (the 0.2.0 upload never went out; 0.3.0 is the first on PyPI)
- `rewindable ui`: a local web UI to browse runs, inspect every step and model conversation, approve or reject, resume and rewind. No extra dependencies; binds to localhost and rejects cross-site writes.
- Durable timers: `ctx.sleep(seconds)` suspends long waits and wakes them on time
- Timeouts for `ctx.approve(..., timeout=)` and `ctx.wait_for(..., timeout=, default=)`
- Run leases with heartbeats: one driver per run; `RunLocked` for a second one; leases of dead local processes are taken over at once
- `Engine.submit()` and `Engine.work()`, plus `rewindable start --queue` and `rewindable worker`: queue runs, wake timers, resume answered approvals, recover crashed runs
- `current_step()` with a stable `idempotency_key` for external side effects
- `rewindable step RUN KEY` and `rewindable show --json`
- Journals created by 0.1.0 are migrated automatically
- New example: a follow-up workflow that sleeps for days

## 0.1.0

First public version.

- Durable `Engine` with `run`, `resume`, `rewind`, `signal` and `approve`, on a SQLite journal
- Context primitives: `step` (with retries), `llm`, `parallel`, `wait_for`, `approve`, `now`, `uuid`
- `Agent` with `@tool` schemas from type hints, approval-gated tools, parallel tool calls and sub-agents via `as_tool()`
- Budgets on cost, tokens and model calls that hold across resumes
- Model adapters: Claude (official SDK), OpenAI-compatible endpoints, and an offline `ScriptedModel`
- CLI: `runs`, `show`, `pending`, `approve`, `signal`, `resume`, `rewind`
- Four offline examples: crash and resume, human approval, research team, rewind and fix
