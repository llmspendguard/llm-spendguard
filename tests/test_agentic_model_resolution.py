"""A requested-but-UNSERVED model id is resolved AGENTICALLY to the best served model — transparently, once, cached.

The gap: a caller (or a cross-vendor panel) asks for `gpt-5.6`, which the vendor does not serve — only gpt-5.6-sol/
luna/terra are. closest_served returns None (those are DISTINCT models, not renames), so dispatch used to error and
the caller had to substitute by hand. served_substitute answers the OTHER question — 'the best SERVED model for what
you asked' — via an LLM, records it, and dispatch swaps to it while recording resolved_from so it is never silent.
This guards:
  • served_substitute maps an unserved id → a served id (agentic call mocked), CACHES it (no second call), and
    leaves an already-served id untouched (no call at all);
  • call() respells the id before dispatch so budget+guards+dispatch use the SERVED id, and records
    resolved_from/resolution on the result;
  • the recursion guard (_resolve_guard.on) makes the resolver's own advisor call skip resolution.
Offline: served_check / catalog / the agentic call / model-facts are stubbed; no network, no spend.
"""
import os
import sys
import tempfile
import contextlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-resolve-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, vendor_call, catalog, calls, config, models

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── shared stubs ──
SERVED = {"gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5"}      # what the vendor actually serves
vendor_call.served_check = lambda v, m: "served" if m in SERVED else "stale"
catalog.live_model_ids = lambda v: sorted(SERVED)
catalog.lane_model_ids = lambda v: None
config.advisor_model = lambda: "openai:gpt-5.5"
calls.context = lambda **k: contextlib.nullcontext()

_FACTS = {}
models.facts = lambda m: _FACTS.get(m, {})
models.add_fact = lambda m, k, v, **kw: _FACTS.setdefault(m, {}).__setitem__(
    k, (v, kw.get("confidence", 0.9), kw.get("source", ""), kw.get("verified", False)))

_REAL_CALL = adapters.call          # the real dispatch entry — restored for Part B (Part A stubs it as the resolver call)

# the AGENTIC resolver call: count invocations so we can prove the cache stops the second one
_agentic_calls = []


def _fake_resolver(model, prompt, **kw):
    _agentic_calls.append((model, prompt))
    return {"text": '{"id": "gpt-5.6-sol", "reason": "closest served 5.6-class model"}', "error": None}


adapters.call = _fake_resolver

# ── served_substitute: an UNSERVED id resolves agentically, and the judgement is cached ──
_id, _reason = vendor_call.served_substitute("openai", "gpt-5.6")
ck("served_substitute maps an unserved id to the agentic pick", _id == "gpt-5.6-sol" and _reason == "closest served 5.6-class model")
ck("...and records it as a model fact", _FACTS.get("gpt-5.6", {}).get("served_substitute", ("",))[0] == "gpt-5.6-sol")

_before = len(_agentic_calls)
_id2, _reason2 = vendor_call.served_substitute("openai", "gpt-5.6")
ck("a second call hits the CACHE — the agentic resolver is NOT re-invoked", len(_agentic_calls) == _before)
ck("...and returns the cached id + reason", _id2 == "gpt-5.6-sol" and _reason2 == "closest served 5.6-class model")

# ── an already-SERVED id resolves to itself with NO agentic call ──
_before = len(_agentic_calls)
_sid, _sreason = vendor_call.served_substitute("openai", "gpt-5.5")
ck("a served id → (itself, None), no agentic call", _sid == "gpt-5.5" and _sreason is None and len(_agentic_calls) == _before)

# ── a pick the model INVENTS (not in the served set) is rejected → no substitution ──
adapters.call = lambda model, prompt, **kw: {"text": '{"id": "gpt-9-imaginary", "reason": "x"}', "error": None}
_bid, _breason = vendor_call.served_substitute("openai", "gpt-7.0")
ck("an invented (unserved) pick is NOT accepted → (requested, None)", _bid == "gpt-7.0" and _breason is None)

# ── call() dispatch: the id is respelled BEFORE dispatch and the swap is recorded on the result ──
_seen_models = []


def _fake_guarded(model, prompt, **kw):
    _seen_models.append(model)
    prov, mid = model.split(":", 1)
    return {"text": "ok", "executor": "api", "cost": 0.01, "provider": prov, "model": mid, "error": None}


adapters.call = _REAL_CALL          # test the REAL dispatch entry now (Part A had stubbed it as the resolver call)
adapters._call_guarded = _fake_guarded
adapters._book_substitution = lambda *a, **k: None
vendor_call.served_substitute = lambda v, m: ("gpt-5.6-sol", "closest served 5.6-class model") if m == "gpt-5.6" else (m, None)

_r = adapters.call("openai:gpt-5.6", "p", max_tokens=8)
ck("dispatch used the RESOLVED id (budget/guards/call all see the served id)", _seen_models[-1] == "openai:gpt-5.6-sol")
ck("the result records resolved_from (transparent, not silent)", _r.get("resolved_from") == "openai:gpt-5.6")
ck("the result records the resolution reason", _r.get("resolution") == "closest served 5.6-class model")

_r2 = adapters.call("openai:gpt-5.5", "p", max_tokens=8)
ck("a SERVED id is untouched — no resolved_from", _seen_models[-1] == "openai:gpt-5.5" and "resolved_from" not in _r2)

# ── recursion guard: while resolving, dispatch must NOT re-resolve ──
adapters._resolve_guard.on = True
_r3 = adapters.call("openai:gpt-5.6", "p", max_tokens=8)
adapters._resolve_guard.on = False
ck("with _resolve_guard.on, dispatch skips resolution (the resolver's own call cannot recurse)",
   _seen_models[-1] == "openai:gpt-5.6" and "resolved_from" not in _r3)

print(("[OK]" if not fails else "[FAIL]") + " agentic model resolution: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
