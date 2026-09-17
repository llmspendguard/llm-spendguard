"""A cross-vendor PANEL built via vendor_call can NEVER be collapsed to one vendor — every arm is pinned.

The 2026-08-29 incident: the lane bandit ran gpt-5.5 as anthropic/gemini/zai/moonshot while a 5-vendor panel
printed all-ok — a "consensus" that was one model agreeing with itself. The structural guarantee that prevents it:
`vendor_call.call` pins the named vendor (`no_substitution=True`, so the bandit can't swap it), and `fan_out` /
`first_ok` route EVERY arm through `call`. That guarantee EXISTS in the code but was untested — a refactor could
silently re-open the collapse. This guard LOCKS it. Offline + hermetic: `adapters.call` is stubbed to capture what
each arm dispatched — no network, no bandit, no spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-panel-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import vendor_call, adapters   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_seen = []


def _stub_call(model, prompt, **kw):
    _seen.append({"model": model, **kw})
    return {"text": "ok", "model": model, "cost": 0.0, "executor": "stub", "in_tok": 3, "out_tok": 1, "error": None}


adapters.call = _stub_call
vendor_call.served_check = lambda prov, raw: "unchecked"   # skip the live-catalog preflight (no network in the test)

print("-- vendor_call.call PINS the named vendor (no_substitution=True) --")
_seen.clear()
vendor_call.call("openai", "gpt-5.5", "hi", deadline_s=10, purpose="panel", max_tokens=64)
ck("the dispatch happened", len(_seen) == 1)
ck("no_substitution=True — the bandit can NEVER swap a vendor_call-named vendor",
   bool(_seen) and _seen[0].get("no_substitution") is True)

print("-- fan_out PINS EVERY arm → an N-vendor panel cannot collapse to one vendor --")
_seen.clear()
vendor_call.fan_out([("openai", "gpt-5.5"), ("anthropic", "claude-opus-4-8"), ("gemini", "gemini-3.8-flash")],
                    "hi", deadline_s=10, purpose="panel", max_tokens=64)
ck("all three arms dispatched", len(_seen) == 3)
ck("EVERY arm pinned no_substitution=True (no arm is bandit-substitutable → the panel cannot collapse)",
   len(_seen) == 3 and all(s.get("no_substitution") is True for s in _seen))
ck("each arm named a DISTINCT vendor namespace (openai/anthropic/gemini) — a real cross-vendor panel",
   {s["model"].split(":", 1)[0] for s in _seen} == {"openai", "anthropic", "gemini"})

print(f"\n{'[FAIL]' if fails else 'OK'} test_panel_never_collapses: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
