# Waivers — consciously-skipped gates (client)

Named gaps with the trigger that re-opens them (monthly review). Anything not shipped, not gated, and not
here is a process bug.

| Waiver | Rationale | Re-open trigger |
|---|---|---|
| `py.typed` ships while public-surface annotations are partial | inference works for the core; full annotation pass is queued | first downstream mypy complaint, or the 0.4 cycle |
| No load/perf tests | a local library on the user's call path — overhead is bounded by design (no network hops added); gate overhead is microseconds vs LLM latency | if the gate ever adds a measurable hot-path cost |
| DR undeclared as a feature | the ledger is REBUILDABLE from provider truth (`spendguard reconcile`) — declared in SPEND_LEDGER.md; no backup product needed | if users store irreplaceable local-only data |
| Presidio NER not exercised in every CI matrix cell | the deterministic floor is always tested; Presidio path tested where installed | a Presidio-specific regression report |
| Client-side crash reporting off by default | doctrine, likely permanent: no vendor phone-home from users' machines | user-initiated opt-in only |

## Reviewed binary-artifact exception

The offline distribution builder deliberately vendors `src/spendguard/distribution_build_wheels/setuptools-84.2.1-py3-none-any.whl`, setuptools **84.2.1**, because Python 3.12 ensurepip does not provide setuptools and fleet installation must work without fetching a backend from PyPI. Removing it would break the offline build contract.

SHA256: `ca140b45d5325a9478f880e351484f62fef15f336462e449ca0c03c873d589c2`.

This remains a real OpenSSF Scorecard Binary-Artifacts finding. It is a deliberate reviewed exception, not a clean upstream score. The readiness manifest owns the artifact identity and expected digest; selfcheck compares the tracked archive set, actual bytes and this recorded digest. Replacement requires reviewing and updating both records. This hash establishes identity, not provenance or a claim of reproducible source correspondence.

Track removal when the offline backend can be provisioned from reviewed source without weakening fleet installation. Ownership: distribution maintainers; review on every backend update.
