# Contributing to llm-spendguard

Thanks for helping. spendguard governs real money, so the bar is: **never break a caller (fail-open),
never report a wrong price, never spend without an estimate.**

## Setup
```bash
git clone https://github.com/llmspendguard/llm-spendguard && cd llm-spendguard
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,openai,anthropic]"
pytest                       # the whole suite (offline; no network, no spend)
```
Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first, then the module map in
[src/spendguard/README.md](src/spendguard/README.md).

## Ground rules
- **Fail-open.** Anything on a gated call path must let only `SpendGateRefused` propagate; wrap the rest
  (see `gate._guard`). A bug in spendguard must never abort a user's legitimate job.
- **Never hardcode a price.** Use `pricing.py` / `prices.json`. `spendguard audit --ci` fails the build on
  any stray gpt-5.5/Opus literal — this is the founding-bug guard; keep it green.
- **Estimate-first + caged for our own LLM use.** Any new feature that calls an LLM must (a) print a
  zero-spend estimate and require an explicit `--run`, and (b) run inside `calls.context(intent="spendguard:*")`
  so it hits the meta budget. No unbounded loops/fan-out.
- **Self-contained.** No new *required* dependencies (SDKs/otel/yaml stay optional + lazy-imported inside
  functions). All state under `$SPENDGUARD_HOME`. No coupling to any host repo; paths are parameters.
- **Be honest in output.** If a signal is a heuristic, say so (see `validate`/`cascade`). Don't present an
  estimate as actual or an unverified quality as verified.

## Tests
Tests are standalone scripts under `tests/` (some self-isolate via `os.execv`); `pytest` runs them all via
`tests/test_runner.py` as subprocesses in a temp `SPENDGUARD_HOME`. To add one: write `tests/test_<x>.py`
that prints `[OK]`/`[FAIL]` lines and exits non-zero on failure (or just `assert`); the runner picks it up.
Keep tests **offline** — stub SDKs (see `test_gate.py`), don't make real API calls. New money-critical
logic (pricing/reconcile/estimate/gate) needs a direct test.

## Adding things
- **A CLI command:** add the function + a `main(argv)`/`cmd(argv)` in its module, wire one line in `cli.py`,
  document it in `README.md`'s command reference.
- **A config knob:** add it to `config_schema.SETTINGS` (one entry) — it then appears in `config`/`init`/SETUP
  and validation automatically.
- **A new SDK surface to gate:** `spendguard.register(module, Class, "method", gate_fn)` + a small estimator;
  add its prices to `pricing.py`.
- **A new provider:** `adapters.register_provider(...)` (OpenAI-compatible) + prices.

## Commits & PRs
- Conventional, imperative subject; body explains *why* + what was verified (tests run, audit clean).
- Run `pytest` and `spendguard audit --ci` before pushing; CI runs both on 3.9–3.12.
- Sign-off / co-author trailers welcome.
