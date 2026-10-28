# How rewindable works

`rewindable` uses the same idea as durable-execution engines such as Temporal and DBOS, cut down to one SQLite file and tuned for LLM agents: **record the result of every step, and on re-execution return recorded results instead of running the steps again.**

## The journal

Everything lives in one SQLite database (`.rewindable/runs.db` by default) in WAL mode, so several processes can read and write it at once.

| Table | One row per | Holds |
|---|---|---|
| `runs` | workflow run | workflow reference, input, status (`queued` / `running` / `waiting` / `completed` / `failed`), output or error, budget, the run it was rewound from, timer wake-up time, and the current driver's lease |
| `steps` | step of a run | key, name, kind (`step` / `llm` / `parallel` / `signal`), status, JSON output or error, attempts, tokens, cost, timings |
| `signals` | signal a run waited for | the prompt shown to the human, the delivered payload, and when it was requested, delivered and consumed |

A run's workflow is stored as an importable reference: `package.module:function`, or `/abs/path/script.py:function` for scripts. That is how `rewindable resume` in a fresh process finds your code again. The script's folder goes on `sys.path` the way `python script.py` does it, and the script is imported under a module name other than `__main__`, so its `if __name__ == "__main__":` block does not run again.

## Step keys

Every call to `ctx.step`, `ctx.llm`, `ctx.parallel`, `ctx.wait_for` or `ctx.approve` takes the next key from a counter: `1`, `2`, `3`, … Replay is just "re-run the function, and at key *n*, if the journal has a completed step, return its output."

Parallel branches get their own counters under the group's key. Branch *i* of group `3` produces `3.i.1`, `3.i.2`, … so concurrent branches never compete for keys, and the keys are the same no matter which thread finishes first. Groups nest: `3.1.2.0.1` is the first step of branch 0 of a group that is step 2 of branch 1 of group 3.

On replay each completed step is checked by **name and kind**. If the journal says key 4 was `tool:search` and the code now asks for `llm:writer` at key 4, the run fails with `NonDeterminismError`. That is better than handing the writer the search result.

## What happens on…

**A normal run.** A row in `steps` is inserted as `running` before the function is called and set to `completed` with its JSON output after it returns. Outputs go through a JSON round trip on the first execution too, so your code sees the same types (for example, lists, never tuples) on first execution and on replay.

**An exception in a step.** The step is retried `retries` times with exponential backoff. If it still fails, the step is marked `failed`, the exception propagates, and the run is marked `failed`. `resume` replays every completed step and runs the failed one again.

**A hard crash** (`kill -9`, OOM, power loss). The run stays `running` with one step stuck in `running`. `resume` treats that step as never finished and runs it again.

**`ctx.approve` / `ctx.wait_for`.** If the signal has been delivered, its payload is recorded as a completed `signal` step and returned. If not, the request is written to `signals`, the run is marked `waiting`, and control returns to the caller. Inside `ctx.parallel`, the other branches still run to completion and are journaled before the run suspends.

**`resume`.** The workflow function is called again from the top. Completed steps return instantly from the journal, and the first unfinished step runs for real. `RunResult.replayed_steps` and `saved_usd` tell you how much was skipped.

**`rewind --to K`.** A new run is created with the same workflow and input (or a new `--input`). Every completed step that **started before K** is copied into it, except steps that *enclose* K (the parallel group around K is re-run, so that K runs again). The new run then executes normally. The original run is never modified, and `rewindable runs` shows the new one as `⑂ parent@K`.

**`ctx.sleep(seconds)`.** The wake-up time is written into the timer step the first time it is reached, so it does not move when the run is replayed. If the time is more than a second away, the run is saved as `waiting` with `wake_at` set and control returns. `rewindable worker` resumes it once `wake_at` has passed. Approval and signal timeouts work the same way: the deadline counts from when the run first asked, and on expiry the step is completed with the default (for `approve`, a rejection).

**Budgets.** Before each model call, the run's completed usage is summed from the journal and checked against the budget. Replayed steps count as well, so a crash-loop cannot spend past the limit. `resume --max-cost` raises the limit for one run.

## The worker

`Engine.work()` (`rewindable worker`) loops over `Store.ready_runs()`. It picks up:

- `queued` runs, created by `engine.submit()` or `rewindable start --queue`
- `waiting` runs whose `wake_at` has passed
- `waiting` runs with a signal that was requested, has been delivered, and has not yet been consumed by its `wait_for` step. Tracking consumption (not timestamps) means an answer that arrives while other parallel branches are still running is never missed.
- `running` runs whose lease expired, or whose lease holder was a now-dead process on this machine

Each one is driven under a lease, so any number of workers can share a journal.

## Guarantees and limits

- **Steps are exactly-once in the journal, at-least-once in the world.** If the process dies *after* a side effect but *before* the result is committed, the step runs again on resume. This is true of every durable-execution system. For side effects that must not repeat (payments, emails), pass an idempotency key the downstream system understands, for example `f"{ctx.run_id}:refund:{order_id}"`.
- **Step outputs must be JSON-serializable.** Dataclasses and pydantic models are converted automatically.
- **Steps cannot be nested.** The body of a step is opaque: when it is replayed its function does not run, so steps opened inside it would vanish and shift every later key. Open steps from the workflow body or a `ctx.parallel` branch. `NestedStepError` enforces this.
- **Agent tool errors are data.** A tool that raises is recorded as a completed step whose output is the error, because that error is exactly what the model saw. On replay the model's later turns stay consistent with it.
- **One process drives a run at a time.** Before driving a run, a process takes a lease on it (`lease_owner`, `lease_expires`) with a conditional `UPDATE`, and a heartbeat thread renews it every `lease_ttl / 3` seconds (30 s TTL by default). A second driver gets `RunLocked`. If the holder was a process on the same machine that no longer exists, the lease is taken over immediately. Otherwise it is taken over once it expires. `Engine.resume(..., force=True)` takes over anyway; the CLI deliberately does not offer that, because waiting 30 seconds is safer than two drivers.
