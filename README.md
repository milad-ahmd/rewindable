# ⏪ rewindable

**Crash-proof, resumable, rewindable AI agent workflows. In plain Python, on SQLite, with zero dependencies.**

[![CI](https://github.com/milad-ahmd/rewindable/actions/workflows/ci.yml/badge.svg)](https://github.com/milad-ahmd/rewindable/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/rewindable.svg)](https://pypi.org/project/rewindable/)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](https://github.com/milad-ahmd/rewindable/blob/main/LICENSE)

Agents fail in boring ways. The process dies at step 37 of 40, and you pay for steps 1–36 again. A tool has a bug, and you re-run the whole conversation to test the fix. An agent is about to send an email or move money, and you want a human to say yes first, maybe hours later.

`rewindable` records every model call, tool call and side effect as a **step in a journal**. Then:

- **Crash → resume.** Completed steps come back from the journal instantly and for free. Only the rest runs.
- **Pause for a human.** `ctx.approve("Refund €129?")` saves the run and lets the process exit. Approve from the CLI tomorrow and it continues from that exact line.
- **Rewind to any step.** Fix the bug, then `rewindable rewind <run> --to 7`. Steps 1–6 replay from the journal and step 7 onward runs with your new code.
- **Fan out safely.** `ctx.parallel(...)` and parallel tool calls each journal on their own. One failing branch never re-runs the others.
- **Sleep for days.** `ctx.sleep(3 * DAY)` saves the run with a wake-up time. `rewindable worker` resumes it on time, even after a reboot.
- **Hard budgets.** `Budget(max_cost_usd=2)` is checked before every model call and counts usage across crashes and resumes.
- **One driver per run.** Leases stop two processes from running the same workflow at once, and runs left behind by crashed processes are picked up automatically.
- **Compare and check.** `rewindable diff <run>` shows what a rewind reused, re-ran and changed. `rewindable check` replays unfinished runs against your new code before you deploy it, without running anything.
- **Get pinged.** `--notify-url` posts to a webhook (Slack works) the moment a run starts waiting for a human.
- **See everything.** `rewindable ui` opens a local web view of every run, step and model conversation, with approve, resume and rewind buttons. `rewindable show <run>` gives the same view in the terminal.

![rewindable in a terminal: a research run crashes, resumes without paying for its first 9 steps again, then a currency bug is fixed and the run is rewound to the broken step](https://raw.githubusercontent.com/milad-ahmd/rewindable/main/docs/demo.gif)

![The rewindable web UI: a run waiting for a human to approve a refund](https://raw.githubusercontent.com/milad-ahmd/rewindable/main/docs/ui.jpg)

```text
$ rewindable show run_34abbb562f61        # examples/03_research_team.py, offline mode
run_34abbb562f61  completed  examples/03_research_team.py:brief
input: Should we build an open-source durable agent runtime?

✓ 1      lead:llm                        0.3s  $0.0108  → market, tech, risk
✓ 2      lead:tools                      0.6s           3 branches
  ✓ 2.0.1  market:llm                    0.3s  $0.0108  → market_size
  ✓ 2.0.2  tool:market_size              0.0s           {"segment": "AI agent infrastructure", "tam_usd_bn": 4.2, "…
  ✓ 2.0.3  market:llm                    0.3s  $0.0108  Demand is driven by teams moving agents to production. Data…
  ✓ 2.1.1  tech:llm                      0.3s  $0.0108  The hard parts are durability, observability and cost contr…
  ✓ 2.2.1  risk:llm                      0.3s  $0.0108  Main risk: frameworks commoditize fast; differentiate on re…
✓ 3      lead:llm                        0.3s  $0.0108  ## Brief - Demand is driven by teams moving agents to produ…

8 steps · 6 model calls · 9,000 tokens · $0.0648
```

## Install

```bash
pip install rewindable                  # the core: no dependencies
pip install "rewindable[anthropic]"     # + Claude through the official SDK
pip install "rewindable[openai]"        # + any OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, …)
```

Python 3.10 or newer.

## Try it in 60 seconds (no API key needed)

Every example runs offline with a scripted model, so you can try the whole crash → resume → approve → rewind cycle for free.

```bash
git clone https://github.com/milad-ahmd/rewindable && cd rewindable
pip install -e .

python examples/01_crash_and_resume.py --crash    # 💥 dies before writing the report
rewindable runs                                    # the run is stuck in "running"
rewindable resume <run_id>                         # replayed 9 steps from the journal, executed 1

python examples/02_human_approval.py               # ⏸ pauses before issuing a refund
rewindable ui                                      # approve it in the browser and watch it finish
```

The other examples cover a [research team of sub-agents](https://github.com/milad-ahmd/rewindable/blob/main/examples/03_research_team.py), [rewinding to test a bug fix](https://github.com/milad-ahmd/rewindable/blob/main/examples/04_rewind_and_fix.py), and a [workflow that sleeps for days](https://github.com/milad-ahmd/rewindable/blob/main/examples/05_follow_up_later.py).

To use Claude instead, run `pip install -e ".[anthropic]"`, set `ANTHROPIC_API_KEY`, and add `REWINDABLE_LIVE=1` before any example command.

## The whole API on one screen

```python
from rewindable import Agent, AnthropicModel, Budget, Engine, tool


@tool
def search(query: str, limit: int = 5) -> list[str]:
    """Search the web.

    Args:
        query: What to look for.
    """
    ...


@tool(requires_approval=True)  # a human must approve every call
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email."""
    ...


researcher = Agent("researcher", AnthropicModel("claude-opus-5-5"), tools=[search])
writer = Agent(
    "writer",
    AnthropicModel("claude-opus-5-5"),
    tools=[send_email],
    instructions="Write a crisp brief and email it to the requester.",
)


def brief(ctx, topic: str) -> str:
    sources = ctx.step("pick sources", lambda: ["arxiv", "hn", "blogs"])  # any side effect
    notes = ctx.parallel(
        [lambda c, s=s: researcher.run(c, f"{topic} on {s}").text for s in sources]
    )  # concurrent, journaled
    if not ctx.approve(f"Send a brief built from {len(notes)} notes?"):  # human in the loop
        return "cancelled"
    return writer.run(ctx, "\n".join(notes)).text


result = Engine().run(brief, "durable execution", budget=Budget(max_cost_usd=1.00))
print(result.status, result.output)  # "waiting" until someone runs `rewindable approve …`
```

| Primitive | What it does |
|---|---|
| `ctx.step(name, fn, *args, retries=0)` | Runs `fn` once for this point in the workflow; afterwards it is served from the journal. |
| `ctx.llm(model, messages=…, tools=…)` | A model call as a step, with budget check and token and cost accounting. |
| `ctx.parallel([branch, …])` | Runs `branch(child_ctx)` concurrently; each branch journals its own steps. |
| `ctx.approve(question, timeout=…)` / `ctx.wait_for(signal, timeout=…, default=…)` | Suspends the run until a human or another system answers, optionally with a deadline. |
| `ctx.sleep(seconds)` | A durable timer: short waits sleep in-process, long ones suspend the run until a worker wakes it. |
| `ctx.patched("change-id")` | True for runs that reach this point on new code; False for runs that went past it on the old code. |
| `ctx.now()` / `ctx.uuid()` | Time and randomness, recorded so replays see the same values. |
| `current_step().idempotency_key` | Inside a step: a key that stays the same across retries and resumes, for payment and email APIs. |
| `Agent(name, model, tools=…, instructions=…)` | A tool-use loop where every model turn and tool call is a step. |
| `agent.as_tool()` | Lets one agent delegate to another (supervisor and specialist teams). |
| `Engine.run / submit / resume / rewind / signal / approve / work` | Run lifecycle and a worker loop, also available from the CLI. |
| `Engine.check(run_id)` / `diff_runs(store, a, b)` | Dry-replay a run against the current code; compare two runs step by step. |

## The one rule: same inputs, same steps

Replay works by matching your code's step calls, in order, against the journal. So the workflow body should be **deterministic**. Anything that touches the outside world, costs money, or is random goes through `ctx`:

```python
# ✗ re-runs on every resume, and replays see a different value
started = time.time()
data = requests.get(url).json()

# ✓ recorded once, replayed forever
started = ctx.now()
data = ctx.step("fetch", lambda: requests.get(url).json())
```

If you change the workflow so that step 4 is now a different step, `resume` raises `NonDeterminismError` instead of feeding the wrong cached result to the wrong code. Use `rewind --to 4` to branch from the last step that still matches, run `rewindable check` before deploying to find such runs, and wrap deliberate changes in `ctx.patched` (below). [How it works](https://github.com/milad-ahmd/rewindable/blob/main/docs/how-it-works.md) covers the journal, step keys, parallel branches and the exact guarantees.

## CLI

```text
rewindable ui                               local web UI: runs, steps, conversations, approve/resume/rewind
rewindable runs [--status waiting]          list recent runs, with cost
rewindable show RUN [--json]                step tree: timing, tokens, cost, results
rewindable step RUN KEY                     everything recorded for one step (full model output)
rewindable diff RUN_A [RUN_B] [--json]      what changed between two runs (one run: a rewind vs. its parent)
rewindable check [WORKFLOW] [--run RUN] [--status S] [--json]   replay runs against the current code, run nothing
rewindable pending                          approvals and signals waiting on a human
rewindable approve RUN KEY [--reject --note "…"]
rewindable approval-code set|clear|status   require a code only you hold to approve
rewindable signal RUN KEY '{"json": "payload"}'
rewindable resume RUN [--max-cost 5]        continue a crashed, failed or waiting run
rewindable rewind RUN --to STEP [--input '…']
rewindable start app.py:flow --input '…' [--queue]
rewindable worker [--once]                  drive queued runs, due timers, answered approvals, crashed runs
```

`start`, `resume`, `rewind`, `approve`, `signal`, `worker` and `ui` take `--notify-url URL` (or `REWINDABLE_NOTIFY_URL`). Run ids accept unique prefixes. The journal lives in `.rewindable/runs.db`; override it with `--db` or `REWINDABLE_DB`. Because the journal is one SQLite file in WAL mode, any process on the machine can approve, resume or work on a run.

### Running in production

Queue work with `engine.submit(flow, input)` (or `rewindable start … --queue`) and keep one or more `rewindable worker` processes running, for example under systemd or in a container. A worker drives queued runs, wakes timers that are due, resumes runs whose approval has arrived, and takes over runs whose driver crashed (on the same machine immediately, elsewhere once the 30-second lease lapses). Several workers can share one journal safely.

### What did the rewind change?

`rewindable diff RUN` compares a rewound run with the run it came from (or give two runs). Every step is aligned by key and marked `=` reused (copied by the rewind, not run again), `≈` re-run with the same result, `~` changed, `-` only in A or `+` only in B, followed by both runs' steps, tokens, cost and output. The web UI shows the same comparison on a rewound run's page. Model steps are compared by their content, not by token counts or tool-call ids.

```text
$ rewindable diff run_da0763d578f3          # examples/04_rewind_and_fix.py, after the fix
A run_dcb9c94f1a76  completed   B run_da0763d578f3  completed
B was rewound from A at step 2

= 1      fx:llm                        reused     → convert
~ 2      tool:convert                  changed    231.48 → 270.0
~ 3      fx:llm                        changed    250 EUR is 231.48 USD. → 250 EUR is 270.0 USD.

1 reused · 2 changed

         A                              B
status   completed                      completed
steps    3 (3 executed)                 3 (1 reused, 2 executed)
tokens   3,000                          3,000
cost     $0.0216                        $0.0216  ($0.0108 new)
Δ cost                                  +$0.0000
A output: 250 EUR is 231.48 USD.
B output: 250 EUR is 270.0 USD.
```

### Check new code against runs in flight

Runs waiting for an approval or a timer will resume on whatever code is deployed then. `rewindable check` replays every unfinished run (or `--run`, `--status`, or the runs of one `WORKFLOW`) against the code on disk now, without executing anything: journaled steps return their recorded results, and the first step without one ends the check, because that is where a resume would carry on. No step function, model call, signal or timer runs and the journal is not written. It exits 1 if any run would fail with `NonDeterminismError` (or cannot be imported), so it fits in CI before a deploy. In Python: `Engine().check(run_id)`.

```text
$ rewindable check                            # a run waiting for an approval, same code
run_d50079bc32a1   waiting    ok  replays 1 step, a resume continues at 2 (signal:approve-2)

$ rewindable check --status completed         # after renaming a tool the model had already called
run_d50079bc32a1   completed  diverges at step 2: journal signal signal:approve-2, code llm fx:llm
```

To change a workflow that has runs in flight, branch on `ctx.patched("change-id")`. A run that reaches that point for the first time records a marker step and takes the new path; a run whose journal already has a different step there went past it on the old code and gets `False`. Once those runs are gone, delete the old branch but keep the call, since runs that recorded the marker need it to line up.

```python
if ctx.patched("convert-v2"):
    usd = ctx.step("tool:convert_currency", convert_currency, amount)
else:
    usd = ctx.step("tool:convert", convert, amount)
```

### Get told when a run needs you

Pass `--notify-url` to the processes that drive runs (or set `REWINDABLE_NOTIFY_URL`), and every time a run starts waiting on `ctx.approve` or `ctx.wait_for`, rewindable POSTs:

```json
{"run_id": "run_7f…", "workflow": "/app/flow.py:refund", "key": "approve-3", "prompt": "Refund €129?",
 "approval": true, "requested_at": 1793000000.0, "ui_hint": "rewindable approve run_7f… approve-3"}
```

A `hooks.slack.com` URL gets Slack's `{"text": …}` message instead. Each signal is announced once, however often the run is resumed or by how many workers. A failed or slow (5 s) notification only prints a warning: the run is already saved as waiting. The message never contains step outputs or the approval code. In Python, pass any callable: `Engine(on_waiting=webhook(url))` or `Engine(on_waiting=lambda w: page(w.run_id, w.prompt))`; it receives a `Waiting(run_id, workflow, key, prompt, approval, requested_at)`.

### Approvals your agents can't give themselves

Anything that can reach the journal can answer an approval, and that includes an agent with a shell tool: it can ask for approval and then run `rewindable approve` on itself. Set an approval code to close that path:

```bash
rewindable approval-code set     # interactive only; prints the code once
```

From then on, approving needs the code: `rewindable approve` asks for it (it is never echoed), the web UI shows a code field, and `Engine.approve(..., code=…)` / `Engine.signal(..., code=…)` raise `ApprovalCodeError` without it. Rejecting never needs it. The journal keeps only its SHA-256, an approval that reaches the journal some other way is read as a rejection, and a damaged setting refuses every approval rather than allowing them. Keep the code out of files, environment variables and prompts.

This is a lock on rewindable's own doors, not a sandbox: code running as your user can still edit the journal file. Give tools that run untrusted actions their own sandbox or OS user. The idea comes from the decider code in [life-distributable](https://github.com/mattabate/life-distributable).

## Models

| Adapter | Install | Notes |
|---|---|---|
| `AnthropicModel("claude-opus-5-5")` | `pip install "rewindable[anthropic]"` | Official SDK. Server-side refusal fallbacks are on by default (`fallbacks=None` turns them off); `effort="low"…"max"`. Built-in price table for cost tracking. |
| `OpenAICompatModel("llama3.2", base_url="http://localhost:11434/v1")` | `pip install "rewindable[openai]"` | Any Chat Completions endpoint: OpenAI, Ollama, vLLM, LM Studio, OpenRouter. |
| `ScriptedModel(fn_or_list)` | built in | Deterministic offline model for tests, demos and CI. |

Any object with `name`, `complete(request) -> response` and `cost(response) -> float` works as a model. See [models.py](https://github.com/milad-ahmd/rewindable/blob/main/src/rewindable/models.py) for the request and response format (Claude Messages API shape).

## How is this different from…

- **Agent frameworks** (LangGraph, CrewAI, AutoGen, the vendor agent SDKs) give you agent abstractions, and some can checkpoint state inside their own graph model. `rewindable` is not a framework you build inside. It is a durability layer for plain Python functions, with a deliberately small agent loop on top. You can wrap calls from other libraries in `ctx.step`.
- **Durable execution engines** (Temporal, DBOS, Restate, Inngest) solved this problem for backend workflows and inspired this project. They are the right choice at scale, but they mean running a server or a database and learning a platform. `rewindable` brings the same journal-and-replay idea down to `pip install` and one SQLite file, with LLM-specific features built in: token and cost accounting, budgets, tool approvals, and rewinding to debug an agent.

## Status and roadmap

This is an alpha. The core semantics are tested (a real crash in a subprocess, resume, suspend, parallel, budget, rewind, leases, timers, the worker and the web UI), but the API may still change. Ideas where help is very welcome:

- [x] A local web UI for the run timeline (`rewindable ui`)
- [x] Durable timers, approval timeouts and a background worker
- [x] Run diffs, dry-replay checks before deploys, and waiting notifications
- [ ] `async` workflows and `asyncio`-native parallel branches
- [ ] Postgres journal backend for multi-machine workers
- [ ] OpenTelemetry export of steps as spans
- [ ] MCP servers as tool sources
- [ ] Step memoization across runs (cache identical tool calls)
- [ ] Streaming model responses into the journal
- [ ] A TypeScript port

## Contributing

Issues, ideas and PRs are welcome; see [CONTRIBUTING.md](https://github.com/milad-ahmd/rewindable/blob/main/CONTRIBUTING.md). The test suite runs in a few seconds and needs no API keys.

## License

[MIT](https://github.com/milad-ahmd/rewindable/blob/main/LICENSE)
