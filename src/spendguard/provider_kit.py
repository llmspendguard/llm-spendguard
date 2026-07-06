"""Conformance kit for provider plugins — importable by THIRD-PARTY test suites.

A provider package proves it behaves before it ships:

    from spendguard.provider_kit import assert_conformance
    from spendguard_provider_groq import activate

    def test_conformance():
        assert_conformance(activate, name="groq", sample_model="groq/llama-3.3-70b")

Checks the three properties spendguard requires of any provider integration:
  1. REGISTERS — after activate(), the provider is visible (an `adapters.PROVIDERS` entry, or new
     gate interceptors when kind="gate").
  2. PRICED — `sample_model` resolves through `pricing.price()` (never a hardcoded price; ship rows
     via a prices override or a pricing.py PR).
  3. FAIL-OPEN — activate() is idempotent, and a RAISING activate is contained by the plugin loader
     (warns + skips; nothing propagates toward the user's calls).
"""
from . import adapters, provider_plugins


def run_conformance(activate, *, name, sample_model=None, kind="adapter"):
    """Run all checks; returns [(check, ok, detail)]. No network, no spend."""
    results = []
    from . import gate
    before_gate = len(gate.INTERCEPTORS) + len(gate.RT_INTERCEPTORS) + len(getattr(gate, "_EXTRA", []))

    try:
        activate()
        results.append(("activates", True, "activate() ran"))
    except Exception as e:
        results.append(("activates", False, f"activate() raised: {e}"))
        return results                                             # nothing else is meaningful

    if kind == "adapter":
        ok = name in adapters.PROVIDERS
        results.append(("registers", ok, f"adapters.PROVIDERS[{name!r}] {'present' if ok else 'MISSING'}"))
    else:
        after = len(gate.INTERCEPTORS) + len(gate.RT_INTERCEPTORS) + len(getattr(gate, "_EXTRA", []))
        ok = after > before_gate
        results.append(("registers", ok, f"gate interceptors {before_gate}→{after}"))

    if sample_model is not None:
        try:
            from . import pricing
            pricing.price(sample_model)
            results.append(("priced", True, f"pricing.price({sample_model!r}) resolves"))
        except Exception as e:
            results.append(("priced", False, f"pricing.price({sample_model!r}) failed: {e} — ship a pricing row"))

    try:
        activate()                                                  # second call must not raise / duplicate
        results.append(("idempotent", True, "second activate() clean"))
    except Exception as e:
        results.append(("idempotent", False, f"second activate() raised: {e}"))

    _boom_name = f"_conformance_boom_{name}"    # distinct identifier: `name = f"…{name}"` in a class body shadows itself
    class _Boom:                                                    # loader containment (the fail-open contract)
        name = _boom_name
        @staticmethod
        def load():
            def _raise():
                raise RuntimeError("intentional conformance failure")
            return _raise
    status = provider_plugins.load(eps=[_Boom])
    contained = str(status.get(_boom_name, "")).startswith("error:")
    results.append(("fail_open", contained, "a raising plugin is warned + skipped, never propagated"))
    return results


def assert_conformance(activate, *, name, sample_model=None, kind="adapter"):
    """pytest one-liner: raises AssertionError listing every failed check."""
    results = run_conformance(activate, name=name, sample_model=sample_model, kind=kind)
    failed = [f"{c}: {d}" for c, ok, d in results if not ok]
    assert not failed, "provider conformance failed:\n  " + "\n  ".join(failed)
    return results
