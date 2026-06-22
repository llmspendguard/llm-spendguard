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
| `test_attribution.py` | shared classifier: `iso_period` buckets (incl. the `ytd` regression), `project_team_map`, `classify_items` parse + confidence + tolerant recovery (caged call stubbed). |
| `test_chat_value.py` | claude.ai chat **value math**: all-content token accounting (text/tool/thinking/tool_result/image), caching-aware per-turn model, per-day attribution, allocation split. |
| `test_resources_gpu.py` | vast.ai GPU reconstruction: per-UTC-day cost split, snapshot→history, live∪history merge so destroyed instances stay reconstructable, empty default label map. |
| `test_claudecode.py` | `~/.claude` transcript digest, per-session classification, day totals. |
| `test_workdone.py` | work-done rollup shaping (project/team, chats vs code sessions). |
| `test_saas.py`, `test_saas_rollup.py` | `/v1` push contract: scrubbed rollups, channel/kind/billed split, taxonomy pull/push, command queue. |
| `test_ledger_sync.py` | reconcile gap spread across actual usage days (not lumped on the reconcile day). |
| `test_reconcile_anthropic.py` | provider-billing reconciliation (OpenAI + Anthropic) vs the local ledger. |
| `test_reconcile_core.py` | the shared reconcile loop: owner-anchor guard, residual math, direction-aware warnings, the `Source` adapter, both real adapters (LLM + GPU) stubbed, None-truth (fetch-failed) safety. |
| `test_reconcile_e2e.py` | end-to-end **"does it all add up"**: a known dated ledger trimmed by date → trim exactness/monotonicity/pivot-closure, per-source loop residual constant under trim, org-rollup closes, the **portfolio grand total reconciles across LLM+GPU**, and UNKNOWN-truth never reads as $0/100%-covered. |
| `test_schedule.py` | the installable scheduler: macOS launchd (daily=clock-anchored vs hourly=interval), Windows schtasks `/tr` quoting (python path with spaces), Linux crontab marker idempotency + safe removal, unsupported-platform fallback. |
| `test_runner.py` | the pytest entry that runs all of the above as subprocesses. |

This table is representative, not exhaustive — every `test_*.py` in this directory is collected and run by
`test_runner.py`. Adding a `test_*.py` file is enough; no registration needed.
