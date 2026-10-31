# Changelog

## Unreleased

- Approval code: `rewindable approval-code set` makes approving need a code only a human holds, so an agent with a shell tool can no longer approve its own gated calls. The CLI prompts for it, the web UI has a code field, and `Engine.approve`/`signal` take `code=`. Rejecting never needs it; only its SHA-256 is stored; approvals that skip the check are read as rejections. Inspired by the decider code in [life-distributable](https://github.com/mattabate/life-distributable).
- Fix: `rewind --to <parallel group>` replayed branches that finished before the group was resumed, so a tool inside the group was never re-run. Rewinding to a step now always re-runs everything inside it. Reported by [@mattabate](https://github.com/mattabate) in [#1](https://github.com/milad-ahmd/rewindable/issues/1).

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
