# Changelog

## 0.1.0 (unreleased)

First public version.

- Durable `Engine` with `run`, `resume`, `rewind`, `signal` and `approve`, on a SQLite journal
- Context primitives: `step` (with retries), `llm`, `parallel`, `wait_for`, `approve`, `now`, `uuid`
- `Agent` with `@tool` schemas from type hints, approval-gated tools, parallel tool calls and sub-agents via `as_tool()`
- Budgets on cost, tokens and model calls that hold across resumes
- Model adapters: Claude (official SDK), OpenAI-compatible endpoints, and an offline `ScriptedModel`
- CLI: `runs`, `show`, `pending`, `approve`, `signal`, `resume`, `rewind`
- Four offline examples: crash and resume, human approval, research team, rewind and fix
