"""BEHAVIORAL guards for the 🔴 mission-critical caller-intent fixes (2026-09-28). Each EXECUTES the fixed path and
asserts the real outcome (no substring-presence no-ops — a guard that can't fail on the defect is worse than none):

  P2  claudecode._session_digests returns a LIST on a missing projects dir (was int 0 → TypeError in iterating callers).
  P8  output_contract.as_obj_lenient never raises; bulkgate._eval_verdict_from_result DEGRADES unparseable → FAIL (no crash).
  P11 saas.push_rollup REFUSES to push when the saas connection is disabled (the anonymous-contributor leak).
  P1  pricing.price does NOT return a cross-vendor rate for a NAMED provider (a bare id priced to the wrong vendor).

(P7 unpriced-vision-refusal and P12 de-id-withhold are exercised in their own modules' suites / reviewed inline; a
faithful behavioral setup for them needs live image + DB fixtures out of scope here — not faked with a presence check.)

Isolation: SPENDGUARD_HOME → mkdtemp before importing spendguard.
"""
import os, sys, tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-redtier-")

from spendguard import output_contract, claudecode, bulkgate, pricing, saas   # noqa: E402

# P1 fixture, inserted at MODULE SETUP (not mutated mid-main): a cross-vendor model priced under BOTH vendors at
# DIFFERENT rates, so batch_cost(provider=…) must resolve the PINNED vendor's card, not the other. This test runs as
# its own subprocess in chunked_suite, so the insertion never leaks to another test (the process exits after).
_P1_MODEL = "xbatch-shared-redtier"
pricing.PRICING[f"openai/{_P1_MODEL}"] = {"in_": 1.0, "out": 2.0, "batch_in": 0.5, "batch_out": 1.0, "provider": "openai"}
pricing.PRICING[f"anthropic/{_P1_MODEL}"] = {"in_": 9.0, "out": 9.0, "batch_in": 4.5, "batch_out": 4.5, "provider": "anthropic"}


class C:
    def __init__(self): self.f = 0
    def ck(self, name, cond, extra=""):
        if not cond: self.f += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}{('  — ' + extra) if extra and not cond else ''}")


def main():
    c = C()

    # ── P2: a missing projects dir yields a LIST (an int would raise TypeError in every `for d in _session_digests()`) ──
    os.environ["SPENDGUARD_CC_DIR"] = os.path.join(tempfile.mkdtemp(), "nope")
    r = claudecode._session_digests()
    c.ck("P2 _session_digests → list on missing dir (never int 0)", isinstance(r, list), f"got {type(r).__name__}")
    # and it is actually ITERABLE without raising (the real crash the fix prevents)
    try:
        list(claudecode._session_digests()); iter_ok = True
    except TypeError:
        iter_ok = False
    c.ck("P2 the result is iterable (no TypeError in the caller)", iter_ok)

    # ── P8: as_obj_lenient never raises; the crash-caller degrades to FAIL instead of throwing ──
    c.ck("P8 as_obj_lenient(garbage) → (None, False)", output_contract.as_obj_lenient("not json {[") == (None, False))
    c.ck("P8 as_obj_lenient parses valid JSON", output_contract.as_obj_lenient('{"x":1}')[0] == {"x": 1})
    verdict = bulkgate._eval_verdict_from_result({"text": "the judge rambled, no JSON here at all"})   # would RAISE pre-fix
    c.ck("P8 _eval_verdict_from_result degrades an unparseable judge reply to a FAIL (no crash)",
         isinstance(verdict, dict) and verdict.get("pass") is False, str(verdict))

    # ── P11: a DISABLED saas connection must not push (contributor_ok returns True-as-'n/a' when disabled) ──
    _real_conn = saas.saas_connection
    saas.saas_connection = lambda: {"enabled": False, "visibility": "team", "org": "acme"}   # disabled + NON-private
    try:
        out = saas.push_rollup(dry=True)
    finally:
        saas.saas_connection = _real_conn
    c.ck("P11 push_rollup on a DISABLED connection returns skipped (no anonymous push)",
         isinstance(out, dict) and "skipped" in out and "not enabled" in out.get("skipped", ""), str(out))

    # ── P1: the OpenAI/Anthropic batch estimators pin the vendor (a bare multi-vendor id must price to the RIGHT card).
    # The fixture (_P1_MODEL, priced under both vendors at DIFFERENT rates) is inserted at module setup above. ──
    c.ck("P1 batch_cost(provider='openai') prices the OpenAI card ($0.50), not anthropic's",
         abs(pricing.batch_cost(_P1_MODEL, 1_000_000, 0, provider="openai") - 0.5) < 1e-9,   # 1M in @ batch_in $0.5/M
         str(pricing.batch_cost(_P1_MODEL, 1_000_000, 0, provider="openai")))

    print(f"\n{'[FAIL]' if c.f else '[OK]'} test_red_tier_caller_intent_fixes: {c.f} failure(s)")
    return 1 if c.f else 0


if __name__ == "__main__":
    sys.exit(main())
