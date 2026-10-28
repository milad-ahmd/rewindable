# Changelog

## 0.2.0

- First release on PyPI: `pip install rewindable`
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
