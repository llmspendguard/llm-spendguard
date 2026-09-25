"""GUARD — tier-3 PROVIDER BASE-MODEL fallback (the last hop of lane -> metered -> base).

When a chosen model's $0 lane AND its metered API both fail, a caller that opted in (base_fallback=True) drops to
the SAME provider's configured RELIABLE base model (advisor.provider_base_model[provider]) and gets a usable,
clearly-LABELLED answer instead of an error — so a vendor slot yields SOME answer. ON by default for a normal call,
OFF for a pinned/measurement call (no_substitution/metered_only), and a no-op when the provider has no configured base
(config/catalog-gated), so it can never silently swap a pinned model. Pins:
  (a) base_fallback=True + a chosen-model error -> returns the BASE model's answer, labelled (substituted_from + base_fallback);
  (b) a PINNED call (no_substitution) -> base_fallback auto-OFF, the chosen error stands (the model is the measurement);
  (c) no provider_base_model entry for the provider -> tier-3 is a no-op even with base_fallback (config-gated).
Hermetic: adapters._call_guarded (the lane->metered core) + config._cfg_get are stubbed; no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-tier3-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, config   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


CHOSEN = "acme:chosen"


def _guarded(model, prompt, **kw):
    # the lane->metered core: the chosen model fails (lane down AND metered failed); the provider base model serves.
    if model.endswith("base-model"):
        return {"text": "base answer", "provider": "acme", "model": "base-model", "cost": 0.001,
                "in_tok": 5, "out_tok": 3, "error": None, "executor": "api"}
    return {"text": None, "provider": "acme", "model": "chosen", "cost": 0.0, "in_tok": 0, "out_tok": 0,
            "error": "lane down AND metered failed", "executor": None}


_saved = (adapters._call_guarded, config._cfg_get)
adapters._call_guarded = _guarded
_orig_cfg = config._cfg_get


def _cfg_with_base(section, key, default=None):
    if section == "advisor" and key == "provider_base_model":
        return {"acme": "base-model"}
    return _orig_cfg(section, key, default)


try:
    config._cfg_get = _cfg_with_base

    print("-- (a) base_fallback=True + chosen error -> drops to the provider base model, labelled --")
    r = adapters.call(CHOSEN, "p", intent="t", base_fallback=True)
    ck("chosen lane+metered failed + base_fallback -> returns the BASE model's answer (not an error)",
       r.get("text") == "base answer" and not r.get("error"))
    ck("the base fallback is LABELLED (substituted_from=chosen, base_fallback=True)",
       r.get("substituted_from") == CHOSEN and r.get("base_fallback") is True)

    print("\n-- (b) a PINNED call (no_substitution) -> base_fallback auto-OFF, the chosen error stands --")
    r2 = adapters.call(CHOSEN, "p", intent="t", no_substitution=True)
    ck("a pinned/measurement call does NOT base-fallback (the model is the measurement); the error stands",
       r2.get("text") is None and bool(r2.get("error")) and not r2.get("base_fallback"))

    print("\n-- (c) no provider_base_model entry -> tier-3 is a no-op even with base_fallback (config-gated) --")
    config._cfg_get = lambda s, k, d=None: ({} if (s == "advisor" and k == "provider_base_model") else _orig_cfg(s, k, d))
    r3 = adapters.call(CHOSEN, "p", intent="t", base_fallback=True)
    ck("no provider_base_model entry -> tier-3 no-op (chain ends at the metered error)",
       r3.get("text") is None and not r3.get("base_fallback"))
finally:
    adapters._call_guarded, config._cfg_get = _saved

print(f"\n{'[FAIL]' if fails else 'OK'} test_tier3_base_fallback: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
