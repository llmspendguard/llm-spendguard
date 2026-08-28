"""pricing.output_ceiling — the SINGLE per-model OUTPUT-ceiling resolver every call path now shares, so the three
that hand-rolled the authority order (adapters._call_guarded, gate._autotune, zai_exec._output_budget) can no
longer DRIFT. The drift was a real hole: gate omitted the learned-fact tier, so a model known ONLY via a healed
fact was clamped to the backstop and a recommend could be raised OVER its real ceiling → the 400 the system avoids.

Pins, directly on the resolver (offline; the three tier sources are stubbed, no network, no model call):
  (a) authority ORDER — published cache → live /models → learned fact → backstop;
  (b) the provider prefix is stripped INSIDE the resolver, so no caller can forget it and miss the cap;
  (c) learned_floor — a lane (cannot retry-heal) floors a poisoned-low fact; the metered path (floor=0) uses it raw,
      and the floor never lowers a healthy fact nor touches a published/catalog ceiling;
  (d) the GATE-HOLE regression — a model known ONLY via a learned fact resolves to the FACT, not the backstop.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-octr-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import pricing, catalog                                                # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


_orig = (pricing.max_output_tokens, pricing.max_output, catalog.model_ceiling)


def _resolve(model, published, cat, fact, backstop=128000, learned_floor=0, vendor="openai"):
    """Drive the resolver with each tier stubbed to a chosen value; return (result, ids-each-tier-was-queried-with)."""
    seen = {}

    def _pub(m):
        seen["pub_id"] = m
        return published

    def _fact(m):
        seen["fact_id"] = m
        return fact

    def _cat(v, m):
        seen["cat_vendor"] = v
        seen["cat_id"] = m
        return cat

    pricing.max_output_tokens = _pub
    pricing.max_output = _fact
    catalog.model_ceiling = _cat
    val = pricing.output_ceiling(vendor, model, backstop, learned_floor=learned_floor)
    return val, seen


try:
    print("-- (a) authority ORDER: published > live catalog > learned fact > backstop --")
    ck("published wins over everything below it", _resolve("m", 128000, 64000, 2000)[0] == 128000)
    ck("live catalog wins when there is no published ceiling", _resolve("m", None, 64000, 2000)[0] == 64000)
    ck("learned fact wins when no published/catalog", _resolve("m", None, None, 5000)[0] == 5000)
    ck("backstop when NOTHING knows the model", _resolve("m", None, None, None, backstop=128000)[0] == 128000)

    print("\n-- (b) the provider prefix is stripped INSIDE the resolver (no caller can miss the cap) --")
    _, seen = _resolve("openai:gpt-5.5", 128000, None, None)
    ck("published tier is queried with the STRIPPED id", seen.get("pub_id") == "gpt-5.5")
    _, seen2 = _resolve("openai:gpt-5.5", None, None, 5000)
    ck("learned tier is queried with the STRIPPED id too", seen2.get("fact_id") == "gpt-5.5")

    print("\n-- (c) learned_floor: a lane floors a poisoned-low fact; the metered path (floor=0) uses it raw --")
    ck("floor=0 → poisoned 2000 used raw (metered path retries/heals)", _resolve("m", None, None, 2000, learned_floor=0)[0] == 2000)
    ck("floor=16384 → poisoned 2000 floored up (a lane cannot heal)", _resolve("m", None, None, 2000, learned_floor=16384)[0] == 16384)
    ck("floor never LOWERS a healthy fact", _resolve("m", None, None, 40000, learned_floor=16384)[0] == 40000)
    ck("floor does NOT apply to a published/catalog ceiling", _resolve("m", 8000, None, None, learned_floor=16384)[0] == 8000)

    print("\n-- (d) the GATE-HOLE regression: a model known ONLY via a learned fact → the FACT, not the backstop --")
    ck("published None + catalog None + fact 30000 → 30000 (NOT the 128000 backstop gate used to apply)",
       _resolve("healed-only-model", None, None, 30000, backstop=128000)[0] == 30000)

    print("\n-- the catalog tier is queried with the VENDOR the caller passed (zai lane, glm) --")
    _, seen3 = _resolve("glm-4.6", None, None, None, vendor="zai")
    ck("catalog.model_ceiling received vendor='zai'", seen3.get("cat_vendor") == "zai")
finally:
    pricing.max_output_tokens, pricing.max_output, catalog.model_ceiling = _orig

print(f"\n{'[FAIL]' if fails else 'OK'} test_output_ceiling_resolver: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
