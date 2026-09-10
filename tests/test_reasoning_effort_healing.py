"""A reasoning_effort the endpoint REJECTS is healed to one it accepts — discovered empirically, learned, tier kept.

The failure (7thsense batch): gpt-5.4-nano is on the none/low/medium/high/xhigh scale and REJECTS 'minimal', but the
family seed rule (^gpt-5 → 'minimal') is stale for that generation, so normalize sends 'minimal' → 400. The old heal
substituted the family value — which for nano IS 'minimal' (want==sent) — so it gave up and the tier was dropped, and
every call re-laddered. Now heal DISCOVERS what the endpoint accepts (empirical, cached) and picks a valid value that
preserves the tier ('minimal'→'none'), recording it so normalize_reasoning agrees thereafter — which is how a BATCH
builder (no per-request retry) also gets the right value, via resolve_effort. Guards:
  • _pick_effort maps a tier onto the accepted set on the fixed ordinal (floor→floor; named tier→itself/nearest);
  • heal_reasoning heals from the family value when it differs, else DISCOVERS + picks (the nano case), else False;
  • the healed fact propagates to normalize_reasoning;
  • resolve_effort (batch path) corrects a family value the endpoint does not actually accept.
Offline: discover_efforts + the model-fact store are stubbed; no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-heal-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import models, vendor_call

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


class _Err(Exception):
    pass


def _param_err(param):
    e = _Err("rejected")
    e.param = param                                 # the OpenAI-compatible typed field _rejected_param reads
    return e


# in-memory model-fact store (hermetic — no DB bleed between assertions)
_FACTS = {}
models.facts = lambda m: _FACTS.get(m, {})
models.add_fact = lambda m, k, v, **kw: _FACTS.setdefault(m, {}).__setitem__(
    k, (v, kw.get("confidence", 0.9), kw.get("source", ""), kw.get("verified", False)))

# ── _pick_effort: ordinal mapping onto the accepted set ──
ck("_pick_effort floor request → lowest accepted", models._pick_effort("minimal", ["none", "low", "medium", "high"]) == "none")
ck("_pick_effort named tier accepted → itself", models._pick_effort("high", ["none", "low", "medium", "high"]) == "high")
ck("_pick_effort named tier NOT accepted → nearest at-or-below", models._pick_effort("high", ["none", "low"]) == "low")
ck("_pick_effort empty accepted → None", models._pick_effort("minimal", []) is None)

# ── heal_reasoning: family value DIFFERS from what failed → try it, no discovery ──
_probed = []
vendor_call.discover_efforts = lambda prov, model, **kw: (_probed.append((prov, model)), {"accepted": ["none", "low", "medium", "high"]})[1]

kw = {"reasoning_effort": "minimal"}
ck("gpt-5.5 (family 'none') sent 'minimal' → heals to the family value 'none' (no probe)",
   models.heal_reasoning("gpt-5.5", kw, _param_err("reasoning_effort")) is True and kw["reasoning_effort"] == "none" and not _probed)

# ── heal_reasoning: family value IS what failed (gpt-5.4-nano) → DISCOVER + pick a valid one ──
kw = {"reasoning_effort": "minimal"}
_probed.clear()
healed = models.heal_reasoning("gpt-5.4-nano", kw, _param_err("reasoning_effort"))
ck("gpt-5.4-nano sent 'minimal' (== stale family value) → DISCOVERS and heals to 'none'",
   healed is True and kw["reasoning_effort"] == "none" and _probed == [("openai", "gpt-5.4-nano")])
ck("...and records the discovered value as an UNVERIFIED fact", _FACTS.get("gpt-5.4-nano", {}).get("reasoning", ("",))[0] == "none")

# ── the healed fact PROPAGATES: normalize_reasoning now returns the learned value ──
ck("normalize_reasoning('gpt-5.4-nano','minimal') now returns the learned 'none' (fact overrides the stale rule)",
   models.normalize_reasoning("gpt-5.4-nano", "minimal") == "none")

# ── heal_reasoning: a NON-reasoning_effort rejection is not this function's to fix ──
kw = {"reasoning_effort": "minimal"}
ck("a rejection of a DIFFERENT param → heal returns False", models.heal_reasoning("gpt-5.4-nano-x", kw, _param_err("max_tokens")) is False)

# ── heal_reasoning: discovery's only accepted value IS the one that failed → nothing better, False ──
vendor_call.discover_efforts = lambda prov, model, **kw: {"accepted": ["minimal"]}
kw = {"reasoning_effort": "minimal"}
ck("discovery finds only the failed value → heal returns False (no false heal)",
   models.heal_reasoning("gpt-5.4-nano-y", kw, _param_err("reasoning_effort")) is False)

# ── resolve_effort (BATCH path, no per-request retry): correct a family value the endpoint does not accept ──
vendor_call.discover_efforts = lambda prov, model, **kw: {"accepted": ["none", "low", "medium", "high"]}
ck("resolve_effort('gpt-5.4-nano','minimal') → 'none' (family 'minimal' is NOT accepted → discovered + corrected)",
   models.resolve_effort("gpt-5.4-nano-z", "minimal") == "none")
ck("resolve_effort('gpt-5.5','minimal') → 'none' (family 'none' IS accepted → kept, not needlessly changed)",
   models.resolve_effort("gpt-5.5", "minimal") == "none")

print(("[OK]" if not fails else "[FAIL]") + " reasoning effort healing: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
