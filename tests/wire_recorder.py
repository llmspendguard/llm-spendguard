"""Capture what actually reached the wire, so tests can assert BEHAVIOUR instead of grepping source.

WHY THIS EXISTS. Several guards here used to assert things like `'reasoning_effort' in inspect.getsource(
adapters.call)` — a substring search standing in for the question "does this call carry the parameter?".
That is a mechanical proxy for a judgement, and it fails in both directions: it passes when someone types
the string in a comment or a dead branch, and it fails when someone renames a local variable without
changing any behaviour at all. Neither failure has anything to do with whether a request is correct.

The honest form of the question is to make the call and look at the request. This module supplies a fake
OpenAI transport that records the kwargs and, on request, refuses a named parameter the way a real endpoint
would — so the drop-and-retry path can be exercised rather than asserted about.

No network, no key, no spend: config.api_key is stubbed for the duration and the transport never leaves the
process.
"""
import types


class _Recorder:
    seen = None
    refuse = None          # a parameter name the endpoint rejects on first sight, e.g. "reasoning_effort"
    calls = 0

    def __init__(self, **_kw):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        _Recorder.calls += 1
        _Recorder.seen = kw
        if _Recorder.refuse and _Recorder.refuse in kw:
            # Shaped like a real 400 so the adapter's own handler decides what to do — the point is to make
            # the production path run, not to simulate its outcome.
            raise Exception(f"400 Unsupported parameter: '{_Recorder.refuse}' is not supported with this model.")
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1))


def record(vendor, model, *, fact=None, refuse=None, **call_kwargs):
    """Make one adapters.call against the fake transport. Returns (request_kwargs, result, n_requests).

    `fact` injects a stored per-model reasoning fact by patching models.facts — never by writing one, so a
    guard cannot leave test models behind in the real fact store. `refuse` names a parameter the endpoint
    rejects on first sight, which is how the drop-and-retry behaviour gets exercised for real."""
    import os
    import sys
    from spendguard import adapters, models

    # PIN THE API PATH. A subscription lane (claude-code / codex) serves the prompt without ever sending the
    # provider's parameters, so the transport below is never reached and every assertion about what is on
    # the wire silently becomes an assertion about nothing. Measured here the first time this file ran: the
    # `openai` vendor made ZERO requests to the fake while every other vendor made one. That is the fourth
    # time in this project a lane has invalidated a measurement by answering it a different way.
    prev_exec = os.environ.get("SPENDGUARD_ADVISOR_EXECUTOR")
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"

    mod = sys.modules.get("openai") or types.ModuleType("openai")
    prev_cls = getattr(mod, "OpenAI", None)
    mod.OpenAI = _Recorder
    sys.modules["openai"] = mod

    prev_facts, prev_key = models.facts, adapters.config.api_key
    if fact is not None:
        models.facts = lambda m: ({"reasoning": (fact, 0.9, "test", True)} if m == model else {})
    adapters.config.api_key = lambda *_a, **_k: "test-key-the-fake-never-reads"
    _Recorder.seen, _Recorder.refuse, _Recorder.calls = None, refuse, 0
    try:
        result = adapters.call(f"{vendor}:{model}", "hi", max_tokens=8, **call_kwargs)
    finally:
        models.facts, adapters.config.api_key = prev_facts, prev_key
        _Recorder.refuse = None
        if prev_cls is not None:
            mod.OpenAI = prev_cls
        if prev_exec is None:
            os.environ.pop("SPENDGUARD_ADVISOR_EXECUTOR", None)
        else:
            os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = prev_exec
    return (_Recorder.seen or {}), result, _Recorder.calls


def openai_shaped_vendors(limit=None):
    """Every vendor whose request shape is OpenAI Chat Completions, read from the provider table.

    Enumerated, never listed: a hand-written list of vendor names is the same defect as the model allow-list
    these guards exist to prevent — it goes stale the moment a provider is added, and stale in the direction
    that looks like everything passing."""
    from spendguard import adapters
    vs = [v for v, spec in adapters.PROVIDERS.items() if (spec or {}).get("kind") == "openai"]
    return sorted(vs)[:limit] if limit else sorted(vs)
