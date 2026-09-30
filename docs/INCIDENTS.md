# Incident management (client)

The client runs on users' machines inside their LLM call path — an "incident" here is a defect class, not
an outage. Severity is judged by the two invariants the gate property-tests enforce:

| Sev | Definition | Response target | Examples |
|---|---|---|---|
| **1** | The gate ALTERS a call's result, RAISES into the caller, or WRONGLY BLOCKS spend · ledger corruption · a security/privacy defect (key or prompt leaves the machine) | fix + release immediately; advisory to users | (none shipped to date — the hypothesis suite + fail-open discipline exist to keep it that way) |
| **2** | Spend misstated ≥2× · a provider adapter drops/double-counts usage · de-id floor bypassed on an egress path | fix within days; note in CHANGELOG | both 2× double-count P0s during the build (Jun 22/25) |
| **3** | Wrong estimate/price for a model · noisy warning · doc drift | next release | price-drift catches (cross-checked vs OpenRouter) |

**Reporting:** security → [SECURITY.md](https://github.com/llmspendguard/llm-spendguard/blob/main/SECURITY.md) (private disclosure); everything else → GitHub
Issues. **Postmortem rule (the anti-amnesia rule):** every Sev-1/2 fix ships WITH the test/lint/assert
that makes recurrence impossible — an incident closed without a gate is not closed. The build's full
20-incident→gate log lives in the maintainer's build history; the guards it produced live in `tests/`.

## Logged incidents

### 2026-09-30 · Sev-2 · one global embed batch size across providers (the idea that failed)

**What failed.** `adapters.embed()` applied ONE global default (`_EMBED_MAX_BATCH`, larger than 100) to EVERY
OpenAI-compatible embedding provider. Embedding batch ceilings are **provider-enforced limits, not tuning knobs**,
and they differ per provider — and because exceeding one 400s the **whole chunk**, every input in that chunk fails.

**Impact.** A 5,768-text run on `gemini-embedding-001` returned **5,760 / 5,768 unembedded** — a consumer
(`lmm/scripts/embedding_vendor_sweep.py`) had to add a local per-model pin to work around it.

**Measured ceilings** (live bisection 2026-09-30, `scripts/reliability/embed_ceiling_probe.py`, provider-stated):
| provider / model | ceiling | provider's 400 |
|---|---|---|
| gemini-embedding-001 | **100** | `BatchEmbedContentsRequest.requests: at most 100 requests can be in one batch` |
| text-embedding-3-small / -large (OpenAI) | **2048** | `Invalid 'input': array length must be 2048 or less.` |
| voyage-3.5 | **1000** | `The batch size limit is 1000. Your batch size is 1001.` |

**The gate (why it cannot recur).** The ceiling is now a catalog SSOT — `capabilities.embed_max_batch` per embed
model (`model_catalog.embed_batch_ceiling`) — and `adapters.embed()` CLAMPS every chunk to `min(requested, ceiling)`
so a caller cannot exceed it by accident. For an *uncurated* provider, a still-rejected batch is BISECTED empirically
(the provider's accept/reject is the oracle — no error text is parsed, so a message stating both a token limit and a
batch limit can't be misread) until the inputs fit — no double-pay (the rejected request billed nothing) — instead of
failing every input. Guard: `tests/test_embed_batch_ceiling.py` (accessor · clamp · empirical bisection recovery).
**The lesson:** a provider-enforced limit lives in the per-provider SSOT, never as one global literal shared across
vendors — and where a limit is unknown, MEASURE it (bisect), never parse it out of prose.
