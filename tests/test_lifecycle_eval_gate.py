"""LIFECYCLE GATE — the EVAL checkpoint above the shape-test. estimate → test (parse the shape) → EVAL (an LLM judges
the sample against a STATED bar) → run, and a scale run is authorized ONLY when a fresh PASSING eval exists.

These guards are OFFLINE and prove the GATING logic (block / allow) by recording verdicts directly — the agentic
judge (eval_job → a real LLM) is exercised separately by the staged-validation run. What must never regress:
  • a >=$0.25 multi-unit run with estimate+test but NO eval is still BLOCKED (test proves shape, eval proves quality);
  • a FAILING eval keeps scale blocked until a PASSING one exists (iteration falls out for free);
  • an eval with an EMPTY bar is REFUSED (no empty rubber-stamp) — both record_eval and eval_job;
  • require_eval=false falls back to the estimate+test-only gate (a repo can adopt evals gradually).
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lifecycle-")
os.environ["SPENDGUARD_ENFORCE"] = "block"                 # test the HARD gate, not shadow
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import bulkgate                                                        # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


def _raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


fails = []
MODEL = "gpt-5.5"
SIG = bulkgate.sig(MODEL, template_id="lifecycle-eval-test")
BAR = "every row cites a real source id and a non-empty finding; no invented codes"
COUNT, EST = 1000, 5.00                                     # >= $0.25 AND count > preview_max → the gate engages

print("-- the $-primary trigger: below $0.25 (and small) is an exempt PREVIEW --")
fails += ck("a small, <=$0.25 run is a PREVIEW (the allowed test step), never gated",
            bulkgate.check_bulk(bulkgate.sig(MODEL, template_id="tiny"), MODEL, 5, 0.10) == "preview")

print("\n-- estimate+test WITHOUT an eval does NOT authorize scale (shape != quality) --")
fails += ck("nothing recorded → a >=$0.25 multi-unit run is BLOCKED", _raises(
    lambda: bulkgate.check_bulk(SIG, MODEL, COUNT, EST), bulkgate.GateBlocked))
bulkgate.record_estimate(SIG, MODEL, EST, COUNT)
bulkgate.record_tested(SIG, 5, verified=True)              # the shape test passed
st = bulkgate.status(SIG)
fails += ck("estimate+test recorded but eval MISSING → not fresh", st["fresh"] is False)
fails += ck("...status names the missing eval as the reason", "eval" in (st.get("reason") or "").lower())
fails += ck("...and check_bulk still BLOCKS", _raises(
    lambda: bulkgate.check_bulk(SIG, MODEL, COUNT, EST), bulkgate.GateBlocked))

print("\n-- a FAILING eval keeps scale blocked; a PASSING eval authorizes it (iteration is free) --")
bulkgate.record_eval(SIG, bar=BAR, verdict=False, score=0.2, note="ids hallucinated")
fails += ck("a FAILING eval keeps scale BLOCKED", bulkgate.status(SIG)["fresh"] is False)
bulkgate.record_eval(SIG, bar=BAR, verdict=True, score=0.9, note="clean, cites real ids")
fails += ck("estimate+test+PASSING eval → fresh/authorized", bulkgate.status(SIG)["fresh"] is True)
fails += ck("...and check_bulk now PASSES", bulkgate.check_bulk(SIG, MODEL, COUNT, EST) == "pass")

print("\n-- an eval MUST state a bar (no empty rubber-stamp), on both the record and the agentic paths --")
fails += ck("record_eval refuses an empty bar", _raises(lambda: bulkgate.record_eval(SIG, "   ", True), ValueError))
fails += ck("eval_job refuses an empty bar BEFORE any judge call (no network)",
            _raises(lambda: bulkgate.eval_job(SIG, "", ["anything"]), ValueError))

print("\n-- backward-compat: require_eval=false falls back to the estimate+test-only gate --")
os.environ["SPENDGUARD_REQUIRE_EVAL"] = "0"
SIG2 = bulkgate.sig(MODEL, template_id="lifecycle-noeval")
bulkgate.record_estimate(SIG2, MODEL, EST, COUNT)
bulkgate.record_tested(SIG2, 5, verified=True)
fails += ck("with require_eval OFF, estimate+test alone is fresh", bulkgate.status(SIG2)["fresh"] is True)
os.environ.pop("SPENDGUARD_REQUIRE_EVAL", None)
fails += ck("...and with it back ON, the same sig is NOT fresh (eval now required)",
            bulkgate.status(SIG2)["fresh"] is False)

print(f"\n{'[FAIL]' if fails else 'OK'} test_lifecycle_eval_gate: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
