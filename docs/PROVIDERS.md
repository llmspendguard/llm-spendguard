# Adding an LLM provider

Three integration levels — pick the deepest your provider needs. Built-ins cover OpenAI, Anthropic, Azure
OpenAI, Gemini, DeepSeek, Qwen, z.ai/GLM + Bedrock/Vertex/LiteLLM adapters; everything else is a plugin
away, **without forking spendguard**.

## Level 1 — pricing only (OpenAI-compatible endpoints)
If the provider speaks the OpenAI wire format through a gated SDK, the gate already intercepts it — you
only need prices. Ship rows via `SPENDGUARD_PRICES=/path/to/prices.json` (highest-precedence override) or
a pricing.py PR. **Never hardcode a price anywhere else** — `pricing.price(model)` is the single source.

## Level 2 — realtime/compare adapter
```python
from spendguard.adapters import register_provider
register_provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", ("groq/", "llama-"))
```
Registers the provider for `spendguard compare` and realtime calls; the gate accounts them like any
built-in. `prefixes` route model names to the provider.

## Level 3 — full SDK interception
For a provider with its OWN SDK, register gate interceptors (see `bedrock_adapter.py` /
`vertex_adapter.py` as the worked examples): patch the SDK's call method via `gate.register(...)`, feed
usage into `gate._record_rt(model, kw, in_tokens, out_tokens, cost=..., provider=...)`. Rules that make
it a spendguard provider: **fail-OPEN** (your wrapper may never alter the call's result or raise into the
caller), degrade unpriced models to $0-plus-warn (never guess, never drop), and capture usage AT the
source (forward-capture) rather than reconstructing later.

## Packaging: the `spendguard.providers` entry point
Publish as `spendguard-provider-<name>`. Installing it is ALL a user does — `spendguard.install()`
discovers and activates it (fail-open per plugin: a broken plugin warns and is skipped, never breaking
the gate or other plugins).
```toml
# pyproject.toml of spendguard-provider-groq
[project.entry-points."spendguard.providers"]
groq = "spendguard_provider_groq:activate"
```
```python
# spendguard_provider_groq/__init__.py — activate() MUST be zero-arg and idempotent
def activate():
    from spendguard.adapters import register_provider
    register_provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", ("groq/",))
```

## Conformance — prove it before you publish
```python
from spendguard.provider_kit import assert_conformance
from spendguard_provider_groq import activate

def test_conformance():
    assert_conformance(activate, name="groq", sample_model="groq/llama-3.3-70b")
    # kind="gate" instead when you register interceptors rather than an adapter
```
The kit checks: activation runs and is **idempotent**; the provider actually **registers**; the sample
model is **priced** via `pricing.price()`; and a raising plugin is **contained** by the loader (the
fail-open contract). Guard in this repo: `tests/test_provider_plugin.py`.

## Checklist before publishing
- [ ] `activate()` zero-arg, idempotent, no network/imports of heavy deps at module top level
- [ ] pricing rows shipped (override file or pricing.py PR) — kit's `priced` check passes
- [ ] env key named `<PROVIDER>_API_KEY`; documented in your README + suggested for `keys.env`
- [ ] conformance test green in YOUR CI against the current spendguard release
- [ ] name the package `spendguard-provider-<name>` so users can find it

---

# Adding a GPU / remote-compute provider

Remote-compute spend rides the same reconcile loop as LLM spend. The seam is **`gpu_port.py`**:

- **The port** — `GPUProvider`: `name`, `configured()` (key material in the environment; `keys.env` is loaded
  into `os.environ` on import, same as every LLM key), and `instances()` returning NORMALIZED rows:
  `id · label · gpu · dph_usd · start_ts · end_ts` (+ optional `usd` for provider-billed per-day rows, and the
  honesty markers `unpriced` / `untimed`). `instances()` **never raises** — any API failure returns `[]` so a
  transient outage can't zero a ledger or error the reconcile (the vast.ai doctrine).
- **One splitting math** — `gpu_port.day_slices` / `cost_by_day` implement the per-UTC-day dph×hours split
  extracted from the vast.ai implementation; `resources.py` calls the same helper, so every provider's $ lands
  on the same days. Costs come **only** from the provider's own dph/billing fields — never a local $/hr table.
- **Honesty rules** — a row the provider doesn't price is returned `{"unpriced": true}`; a row whose runtime
  the API doesn't expose is `{"untimed": true}`. Both stay VISIBLE and contribute nothing to $ math — UNKNOWN
  never reads as $0. A provider with no billing-total endpoint has no `account_total` → reconcile truth shows
  `unknown`, never "fully covered".
- **Attribution** — instance label → project via config `resources.<provider>.label_map`
  (`{substring: project}`), exactly like vast.ai labels. Empty by default on purpose: an opinionated default
  would silently mis-attribute someone's instance.
- **The registry** — `gpu_port.register_source(key, factory)` is the SAME registry `reconcile.all_sources`
  iterates for vast.ai (`"gpu"`), so a registered source rides `spendguard reconcile all` with zero
  special-casing. The factory returns a `reconcile.Source` (wrap your provider in
  `gpu_port.ProviderGPUSource`) or `None` when unconfigured → silently skipped.

## Built-in adapters
| provider | module | key(s) | what the documented API gives us |
|---|---|---|---|
| vast.ai | `resources.py` | `VAST_API_KEY` | instances + dph, snapshot→history for destroyed boxes, invoice truth (reference impl) |
| RunPod | `runpod_adapter.py` | `RUNPOD_API_KEY` | GraphQL `myself{pods}`: RunPod's own `costPerHr`; a RUNNING pod's current session (`runtime.uptimeInSeconds`); a stopped pod's past runtime is NOT exposed → `untimed` |
| Modal | `modal_adapter.py` | `MODAL_TOKEN_ID` + `MODAL_TOKEN_SECRET` | the SDK's `modal.billing.workspace_billing_report` (Modal's documented usage surface — there is no public REST usage endpoint): per-app per-UTC-day BILLED $ (`usd` rows), which is also the account truth |
| Lambda | `lambda_adapter.py` | `LAMBDA_API_KEY` | `GET /api/v1/instances`: Lambda's own `price_cents_per_hour`; the listing exposes NO launch timestamp → rows are `untimed` (visible rate, no fabricated hours) until a first-seen snapshot cadence lands |

Adapters are **offline-tested against the providers' documented response shapes** (`tests/test_gpu_port.py`,
fixture doc-URLs cited inline), not live-verified against provider accounts.

## Third-party GPU provider (same plugin recipe as LLM providers)
```python
# spendguard_provider_acmegpu/__init__.py — activate() zero-arg + idempotent, registered via the
# [project.entry-points."spendguard.providers"] group exactly like an LLM provider plugin
def activate():
    from spendguard.gpu_port import register_source, ProviderGPUSource
    from .provider import AcmeGPUProvider          # implements gpu_port.GPUProvider
    p = AcmeGPUProvider()
    register_source(f"gpu:{p.name}", lambda: ProviderGPUSource(p) if p.configured() else None)
```
Checklist: key named `<PROVIDER>_API_KEY` (declare it in your README for `keys.env`); `instances()` never
raises; $ only from the provider's own fields; `unpriced`/`untimed` for what it doesn't expose; offline tests
against the documented payload shape.
