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
