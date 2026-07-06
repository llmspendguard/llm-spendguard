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
