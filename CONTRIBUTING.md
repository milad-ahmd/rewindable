# Contributing to rewindable

Thanks for helping! Bug reports, docs fixes, examples and new features are all welcome.

## Set up

```bash
git clone https://github.com/milad-ahmd/rewindable && cd rewindable
python -m venv .venv && source .venv/bin/activate     # or: uv venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                                  # < 1 second, no API keys needed
ruff check . && ruff format --check .
```

## Ground rules

- **The core stays dependency-free.** Model SDKs are optional extras, imported lazily.
- **Every behavior change comes with a test.** Most can use `ScriptedModel` and a temporary `Engine(tmp_path / "runs.db")`; see `tests/` for patterns, including a real `os._exit` crash in a subprocess.
- **Journal semantics are the product.** If a change affects step keys, replay, suspension or rewind, update [docs/how-it-works.md](docs/how-it-works.md) in the same PR.
- **Keep the public API small.** Open an issue to discuss a new primitive before building it.

## Good first contributions

- A new example (for example a coding agent, a data pipeline, or a Slack approval bot)
- A model adapter (Gemini, Bedrock, Mistral) following `OpenAICompatModel`
- `rewindable show --json` for scripting
- Pricing for more models in `models.py`

Look for issues labelled `good first issue`.

## Pull requests

1. Fork, branch from `main`, keep the PR focused.
2. Make sure `pytest` and `ruff` pass.
3. Describe what changed and why; link the issue.

By contributing you agree that your contributions are licensed under the MIT License.
