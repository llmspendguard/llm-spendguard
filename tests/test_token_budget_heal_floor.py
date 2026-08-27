"""The output-budget auto-heal must NEVER learn an implausibly-small max_output. A 400 from a NON-budget cause (a
malformed request, a transient error) also drives the budget-halving, and recording the tiny value where it happened
to pass POISONS the model's max_output for every future call — MEASURED: gpt-5-mini learned max_output=7, clamping
the whole class to a ~7-token budget (51% truncation) until the fact was cleared. `_heal_token_budget` stops at
`_MIN_LEARNED_MAX_OUTPUT` and never records below it; `clear_fact` removes a poisoned learning. Offline: fakes the
create() at the boundary and captures models.add_fact — no OpenAI, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-healfloor-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, models                                                # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
FLOOR = adapters._MIN_LEARNED_MAX_OUTPUT
learned = []
_orig_add = models.add_fact
try:
    models.add_fact = lambda model, key, value, **k: learned.append((model, key, value))

    print("-- a NON-budget 400 that persists at every budget: heal gives up, learns NOTHING (no poison) --")
    learned.clear()

    def _always_400(b):
        raise RuntimeError("bad request")              # a 400 that never resolves (not really about the budget)
    r = adapters._heal_token_budget(_always_400, 32_000, "gpt-5-mini")
    fails += ck("returns None (no budget worked)", r is None)
    fails += ck("★ learned NOTHING — no poisoned max_output written (the gpt-5-mini=7 bug can't recur)",
                not any(k == "max_output_tokens" for _m, k, _v in learned))

    print("\n-- a REAL budget limit (accepts at 8000): heal learns 8000 (>= floor), returns the response --")
    learned.clear()

    def _accepts_at_8000(b):
        if b > 8000:
            raise RuntimeError("max_completion_tokens too large")
        return {"ok": b}
    r2 = adapters._heal_token_budget(_accepts_at_8000, 32_000, "some-model")
    fails += ck("returns the accepted response", r2 == {"ok": 8000})
    fails += ck("learns max_output_tokens = 8000 (a plausible ceiling)", ("some-model", "max_output_tokens", 8000) in learned)
    fails += ck("everything it learns is >= the floor",
                all(v >= FLOOR for _m, k, v in learned if k == "max_output_tokens"))

    print("\n-- PLAUSIBILITY GUARD: when the PUBLISHED ceiling is known, a heal artifact must NOT overwrite it --")
    learned.clear()
    from spendguard import pricing
    _orig_pub = pricing.max_output_tokens
    pricing.max_output_tokens = lambda m: 128_000 if m == "published-model" else _orig_pub(m)
    try:
        r_pub = adapters._heal_token_budget(_accepts_at_8000, 32_000, "published-model")   # a transient 400 clears at 8000
        fails += ck("the call still recovers (returns the accepted response)", r_pub == {"ok": 8000})
        fails += ck("★ learns NOTHING — a published ceiling is authoritative, never overwritten by a mid-range heal artifact",
                    not any(k == "max_output_tokens" for _m, k, _v in learned))
    finally:
        pricing.max_output_tokens = _orig_pub

    print("\n-- a request that only ever passes BELOW the floor (a non-budget 400): learns NOTHING --")
    learned.clear()

    def _accepts_only_tiny(b):
        if b >= FLOOR:
            raise RuntimeError("bad request")          # fails at every plausible budget; would 'pass' only sub-floor
        return {"ok": b}
    r3 = adapters._heal_token_budget(_accepts_only_tiny, 32_000, "gpt-5-mini")
    fails += ck("returns None rather than a sub-floor 'success'", r3 is None)
    fails += ck("still learns NOTHING (a sub-floor pass is not a real ceiling)",
                not any(k == "max_output_tokens" for _m, k, _v in learned))
finally:
    models.add_fact = _orig_add

print("\n-- clear_fact removes a poisoned learning so the model falls back + self-heals --")
models.add_fact("poison-model", "max_output_tokens", 7, source="test")
fails += ck("fact present before clear", "max_output_tokens" in models.facts("poison-model"))
n = models.clear_fact("poison-model", "max_output_tokens")
fails += ck("clear_fact removed 1 row", n == 1)
fails += ck("fact gone after clear", "max_output_tokens" not in models.facts("poison-model"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_token_budget_heal_floor: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
