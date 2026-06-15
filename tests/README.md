# `tests/` — test suite

Run everything from the repo root:
```
pytest                       # or: python -m pytest
```
`pytest` collects only `test_runner.py`, which **subprocess-runs every `test_*.py` script** in its own
temp `SPENDGUARD_HOME` (so nothing touches your real `~/.spendguard`). The tests are written as standalone
scripts — some self-isolate via `os.execv` — so running them as subprocesses is what keeps them working
under pytest without rewriting them. Each can also be run directly: `python tests/test_gate.py`.

All tests are **offline** (no network, no real API calls): the gate test stubs the SDK methods; the rest
exercise pure logic. None spend money.

| test | covers |
|---|---|
| `test_gate.py` | the gate end-to-end (stubbed SDKs): per-batch cap refuse/allow, kill switch, real-time budget, **meta-budget cage** (advisor spend segregated from workload). 13 cases. |
| `test_advisor.py` | robust insight JSON parsing (fenced / truncated / non-JSON). |
| `test_history.py` | batch-id artifact extraction, stem cleaning, dir→intent. |
| `test_conv.py` | transcript text extraction, event scoring, prompt formatting. |
| `test_learn.py` | insight scrub (abstract, keep context / drop $+intent) + token-bounded model match. |
| `test_cacheaudit.py` | common-prefix (cacheable block) detection. |
| `test_experiment.py` | graded equivalence + scalar flatten. |
| `test_equivalence.py` | the equivalence ladder (exact/scalar/text) + structural format check. |
| `test_models.py` | per-model family rules (verified reasoning literals, cache mins) + self-heal decision. |
| `test_semcache.py` | exact response cache (per-model, savings) + batch dedup (within-batch + already-cached). |
| `test_cascade.py` | routing (cheap-first, escalate-on-fail) + default verifier. |
| `test_ledger.py` | local ledger `by_day` / kind filters / `ledger_start` / workload-excludes-meta. |
| `test_brief.py` | slugify + scale-from-task. |
| `test_runner.py` | the pytest entry that runs all of the above as subprocesses. |
