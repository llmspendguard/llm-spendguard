# INTENT — llm-spendguard (client)

**What this is.** llm-spendguard is *agentic forensic accounting for LLM and compute spend*. It runs where the
work happens — a CLI, an import-time gate, and a local ledger — and answers three questions with **evidence**,
not numbers you have to take on faith:

- **ATTRIBUTION** — where did every dollar go? (project · org · conversation · model · intent)
- **DISCOVERY** — what spend happened that you didn't know about? (ungated calls, remote/GPU compute, pre-ledger history)
- **CONTEXT** — what was it *for*, and was it worth it? (the job, the outcome, quality-at-cost)

**The mission is that those three are CORRECT.** If attribution/discovery/context are wrong, nothing downstream —
dashboards, org rollups, savings claims — has any value. Every number is cross-checked against **ground truth**
(the provider's actual bill + known repos), never against fixtures rigged to pass.

**What it does.**
1. **Gates every LLM/embeddings call.** `import spendguard; spendguard.require()` fails **closed** — ungoverned code
   can't run silently. Estimate-before-spend on every paid batch; never hardcode a price (only `pricing.py`).
2. **Routes batchable work across $0 subscription lanes** (claude-code · codex · gemini · zai) instead of paying the
   metered API — the biggest cost lever when a plan is capped. Each lane↔metered pair is atomic and reasoning-floor pinned.
3. **Reconciles the local ledger to the provider's real bill** across **batch · realtime · GPU**, and **reconstructs
   realtime spend WITHOUT a provider admin key** — agentically, from conversation token records — so you know your
   realtime spend even when the provider offers no cooperation.
4. **Attributes every charge agentically** to project/org/conversation. Decisions about *meaning* are always an LLM's,
   never regex.

**What it is NOT.** Not a proxy or gateway (it gates in-process). Not the dashboard — that's the **server**
(`llm-spendguard-server`), which aggregates what this client pushes and **never recomputes a cost**. Not a heuristic:
cost is controlled by the *rails* (gate · estimate-first · batch · cache · cheap-lane), never by swapping the LLM for a
keyword hack. A $0 attribution that is wrong is worth less than nothing.

**Non-negotiables** (enforced by tests/lints, not memory): decisions are agentic · estimate before spend · never
hardcode prices · fail closed · never truncate the evidence a judgement reads · unique + semantic names. Full operating
doctrine: `CLAUDE.md`. Architecture: `docs/AGENTIC.md` (agentic attribution/reconcile) + `docs/ARCHITECTURE.md` (gate/rails).

**Boundary with the server.** This client is **measurement + source of truth** (the gate, the reconcile, the forensic
attribution). The server is **aggregation + presentation + billing**. A cost is computed here, once, and pushed up as a
roll-up; the server stores `spend_micros` exactly as attested and never re-derives it.
