# Incident management (client)

The client runs on users' machines inside their LLM call path — an "incident" here is a defect class, not
an outage. Severity is judged by the two invariants the gate property-tests enforce:

| Sev | Definition | Response target | Examples |
|---|---|---|---|
| **1** | The gate ALTERS a call's result, RAISES into the caller, or WRONGLY BLOCKS spend · ledger corruption · a security/privacy defect (key or prompt leaves the machine) | fix + release immediately; advisory to users | (none shipped to date — the hypothesis suite + fail-open discipline exist to keep it that way) |
| **2** | Spend misstated ≥2× · a provider adapter drops/double-counts usage · de-id floor bypassed on an egress path | fix within days; note in CHANGELOG | both 2× double-count P0s during the build (Jun 22/25) |
| **3** | Wrong estimate/price for a model · noisy warning · doc drift | next release | price-drift catches (cross-checked vs OpenRouter) |

**Reporting:** security → [SECURITY.md](../SECURITY.md) (private disclosure); everything else → GitHub
Issues. **Postmortem rule (the anti-amnesia rule):** every Sev-1/2 fix ships WITH the test/lint/assert
that makes recurrence impossible — an incident closed without a gate is not closed. The build's full
20-incident→gate log lives in the maintainer's build history; the guards it produced live in `tests/`.
