"""Per-model call parameters are decided in ONE place, and that place is on the path real calls take.

WHY THIS GUARD EXISTS. models.py opens with "a fact that exists but isn't applied is useless" and names the
exact failure it was written to prevent: "'gpt-5 wants reasoning=none' sitting in memory while a call burns
its whole budget on reasoning and returns empty". That is precisely what then happened — gpt-5.5 returned
EMPTY on 2 of 53 validation calls — because `models.apply_call_params` was called by experiment.py and
cascade.py (the probes) and NOT by adapters.call (every real call).

So the fact store was right, the fact was right, and the bug shipped anyway. A single place that is not on
the path is not a single place; it is a second opinion nobody asks for.

THREE COPIES existed at once:
    models.apply_call_params()              the designed home, called only by probes
    adapters.call                           an inlined copy of the same lookup, on the real path
    vendor_call.record_effort/effort_policy a second REGISTRY, called by nothing at all

The dead pair is the most dangerous of the three, not the least: it is one plausible import away from being
where a future measurement gets written and then never read, and that failure is silent in whichever half
you did not use.

WHAT THIS FILE PINS
  1. the fact BINDS at the real chokepoint, for OpenAI-compatible endpoints that are not OpenAI
  2. adapters DELEGATES rather than re-inlining
  3. an explicit caller argument still beats the registry
  4. '?' — the family-rule miss marker — is never sent as if it were a tier
  5. no policy registry regrows in vendor_call

CHECK 1 IS THE ONE THAT WOULD HAVE CAUGHT TODAY'S NEAR-MISS. `apply_call_params` gated on
`provider == "openai"`, inferred from the model NAME. kimi-k3 and glm-5.2 match no family rule, so they
infer provider='?' — and both have MEASURED reasoning facts written by an A/B, and both speak the OpenAI
request shape. Routing adapters through the shared helper without passing an explicit dialect would have
dropped exactly the facts the A/B was paid to establish, while looking like a cleanup.
"""
import inspect
import sys
import types

from spendguard import adapters, models, vendor_call

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


class _Recorder:
    """A fake OpenAI client. Records the request kwargs; never touches the network."""
    seen = None

    def __init__(self, **_kw):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        _Recorder.seen = kw
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def _fake_openai():
    mod = sys.modules.get("openai") or types.ModuleType("openai")
    prev = getattr(mod, "OpenAI", None)
    mod.OpenAI = _Recorder
    sys.modules["openai"] = mod
    return mod, prev


def call_with_fact(vendor, model, fact_value, **kwargs):
    """Run adapters.call for a model whose stored fact is `fact_value`, and return the request kwargs.

    The fact is injected by patching models.facts rather than by writing one, so the guard cannot pollute
    the real fact store with test models — a store that accumulates fixtures stops being evidence."""
    mod, prev_cls = _fake_openai()
    prev_facts, prev_key = models.facts, adapters.config.api_key
    models.facts = lambda m: ({"reasoning": (fact_value, 0.9, "test", True)} if m == model else {})
    adapters.config.api_key = lambda *_a, **_k: "test-key-not-used-by-the-fake"
    _Recorder.seen = None
    try:
        adapters.call(f"{vendor}:{model}", "hi", max_tokens=8, **kwargs)
    finally:
        models.facts, adapters.config.api_key = prev_facts, prev_key
        if prev_cls is not None:
            mod.OpenAI = prev_cls
    return _Recorder.seen or {}


# 1. THE CASE THAT NAME-INFERENCE BREAKS: an OpenAI-shaped endpoint that is not OpenAI.
for vendor, model in (("moonshot", "kimi-k3"), ("zai", "glm-5.2")):
    if vendor not in adapters.PROVIDERS:
        continue
    seen = call_with_fact(vendor, model, "high")
    check(f"a measured fact for {vendor}/{model} reaches the request",
          seen.get("reasoning_effort") == "high",
          f"got {seen.get('reasoning_effort')!r} — these match no family rule, so provider infers '?' and "
          "a dialect gate keyed on the model NAME silently drops the A/B's verdict")

# 2. DELEGATION, not a fourth copy. Behavioural: the shared helper must actually be invoked.
_seen_calls = []
_prev_apply = models.apply_call_params


def _counting_apply(model, kw, **kwargs):
    _seen_calls.append(model)
    return _prev_apply(model, kw, **kwargs)


models.apply_call_params = _counting_apply
try:
    call_with_fact("moonshot", "kimi-k3", "high")
finally:
    models.apply_call_params = _prev_apply
check("adapters.call delegates to models.apply_call_params",
      bool(_seen_calls),
      "the lookup was re-inlined; a copy on the real path is how the fact store came to apply to some "
      "calls and not others")

# 3. An explicit argument from the caller still wins. The registry FILLS IN; it does not override.
seen = call_with_fact("moonshot", "kimi-k3", "high", reasoning="low")
check("an explicit reasoning= argument beats the stored fact",
      seen.get("reasoning_effort") == "low",
      f"got {seen.get('reasoning_effort')!r} — a probe that asks for a specific tier must get it, or the "
      "A/B measuring tiers cannot set them")

# 4. '?' IS A MISS MARKER, NOT A TIER. _family() returns it for unmatched models; sending it is a 400.
seen = call_with_fact("moonshot", "kimi-k3", "?")
check("the family-miss marker '?' is never sent as a tier",
      seen.get("reasoning_effort") != "?",
      "'?' means no rule matched — absence, not a value")

# 5. NO SECOND REGISTRY. Structural, because the failure mode of the dead pair was that nothing called it:
#    a behavioural test cannot see a store that is never read.
for gone in ("record_effort", "effort_policy"):
    check(f"vendor_call has no {gone}() policy store",
          not hasattr(vendor_call, gone),
          "per-model POLICY lives in models.py; the caps registry holds measured LIMITS (output_cap, "
          "input_limit, which tiers an endpoint ACCEPTS). Two stores for one fact is worse than either")

# discover_efforts stays — it answers "what does this endpoint accept", which is a limit, not a policy.
check("discover_efforts() still lives with the other measured limits",
      hasattr(vendor_call, "discover_efforts"),
      "capability discovery is not policy and does not move")

check("apply_call_params takes an explicit dialect",
      "dialect" in inspect.signature(models.apply_call_params).parameters,
      "without it the request shape is guessed from the model name, which fails silently for every model "
      "no family rule matches")

print(f"\n{'PASS' if not failures else 'FAIL'} — one place decides per-model call params ({failures} failed)")
sys.exit(1 if failures else 0)
