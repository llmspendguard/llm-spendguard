# Security policy — llm-spendguard (client)

`llm-spendguard` runs **on your machine, with your keys**. Its whole design is to keep the sensitive things local:
provider API keys, prompts, and outputs never leave the device, and the optional SaaS push carries only scrubbed
aggregates. So the client's security surface is mostly about **not leaking what it holds** and **failing closed**
when it can't actually enforce. The full system threat model (client + server) lives in the server repo:
[THREAT-MODEL.md](https://github.com/llmspendguard/llm-spendguard-server/blob/main/docs/THREAT-MODEL.md).

## Reporting a vulnerability

**Please report privately — do not open a public issue.**

1. **Preferred:** GitHub private vulnerability reporting — *Security → Report a vulnerability* on this repository.
2. **Email:** security@llmspendguard.com (routes to the maintainers).

Include a description, affected module/command, reproduction steps, and the impact. We support coordinated
disclosure and will credit reporters who want it. Please test only against your own machine/accounts and give us
reasonable time to fix before disclosing.

## Response targets (good-faith, small team)

Acknowledge within **2 business days**; initial assessment within **5**; high/critical fixes as fast as practical.

## What's in scope

The packaged code under `src/spendguard/` — in particular:

- **Secret handling.** Keys are read from the environment / `~/.spendguard` config and must never be logged,
  printed, or passed on argv. The claude.ai chat adapter (opt-in) decrypts its session key **in-process** and
  caches cookies `0600`. A path that leaks any of these is in scope.
- **Fail-closed enforcement.** `spendguard.require()` must raise when the gate isn't actually enforcing in the
  current interpreter (a silent bypass is a vulnerability). `spendguard doctor` must report enforcement honestly.
- **Cost-control integrity.** The caged advisor's meta-budget (`caps.meta`) and the estimate-first protocol must
  hold — a path that spends real money without the cap/estimate is in scope.
- **Prompt-injection resistance.** The advisor/agentic prompts treat tool/observed content as data, not
  instructions; a bypass that makes the client act on injected instructions is in scope.

## Out of scope

The third-party SDKs/providers (OpenAI, Anthropic, claude.ai); a user disabling **their own** gate
(`GATE_DISABLE=1` is a documented kill switch, not a vulnerability); and best-practice reports with no concrete
impact. The SaaS server has its [own policy](https://github.com/llmspendguard/llm-spendguard-server/blob/main/SECURITY.md).

## Supported versions

The latest released version on PyPI is supported. Pin a version for reproducibility; upgrade for fixes.

## Scanner findings — accepted false positives

These static-analysis (Aikido) findings were reviewed by hand and are **confirmed false positives**. They are safe
to mark **Ignore** in the scanner with the rationale below on record. Re-verify if the cited code changes.

- **SQL injection — `calls.py`** (`summary()`, `tested_recently()`). Fully parameterized. The predicate strings are
  STATIC (`"intent=?"`, `"intent NOT LIKE 'spendguard:%'"`) and every value is bound as a `?` parameter via the
  driver; the one `IN (%s)` interpolation fills the placeholder **count** (`",".join("?" * len(kinds))`), never the
  values. No user data is ever concatenated into a query string. (See the in-code comments at both call sites.)

- **SSRF — `saas.py`, `resources.py` (+others making outbound HTTP).** The request host is the operator's OWN
  configured endpoint (`saas.url`, default `https://llmspendguard.com`) plus fixed provider / vast.ai API hosts — not
  attacker-influenced input. `_request()` already enforces an https-only guard (it refuses to send the API key to a
  non-https or raw-IP URL). This is a local CLI acting on its own configuration, not a server proxying untrusted URLs.

- **File inclusion / path traversal — `submit.py`, `share.py`, et al.** (`open(<path>)`). The paths are CLI arguments
  supplied by the operator running the tool on their own machine (e.g. the batch `.jsonl` to cost-estimate and
  submit). Reading a local file the operator explicitly named is the tool's purpose; there is no remote attacker in
  the threat model who controls the path.
