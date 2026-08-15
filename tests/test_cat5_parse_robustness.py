"""Cat-5 parse/value robustness — one bad input no longer aborts the whole job:

  * refresh.diff — an LLM-returned NON-NUMERIC price (a string/null) is surfaced as unparseable instead of
    float()-crashing the whole price diff and losing every other model's real change.
  * estimate_divergence.adjudicate_grounding — a NON-JSON judge response (_as_obj RAISES on it) records the
    pair as UNJUDGED (grounded=None), never crashing the estimate-divergence run.
  * scan.collect — a source (plugin) that RETURNS a malformed record (not just one that raises) is normalized
    to the shape render() reads, so a bad plugin can't KeyError/TypeError the whole scan.

Offline, isolated home; the adapter + sources are stubbed in-process.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cat5-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import refresh, estimate_divergence, scan     # noqa: E402
import spendguard.adapters as _ad                              # noqa: E402
import spendguard.sources as _sources                          # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── refresh.diff: a non-numeric LLM price is surfaced, not crashed ────────────────────────────────────────────
refresh.pricing.PRICING = {"m": {"in_": 1.0, "out": 2.0}}
refresh.pricing.normalize = lambda x: x
res = refresh.diff({"m": {"in_": "N/A", "out": 2.0}})          # in_ unparseable; out identical → no diff
fields = {r[1] for r in res}
ck("a non-numeric found price is surfaced as unparseable (diff does not crash)",
   any("unparseable" in f for f in fields) and "out" not in fields, f"got {res}")

# ── estimate_divergence: a non-JSON verdict is UNJUDGED, never a crash ─────────────────────────────────────────
_ad.call = lambda *a, **k: {"text": "the estimate looks fine to me", "error": None}   # not JSON
v = estimate_divergence.adjudicate_grounding({"estimate": 1.0, "actual": 2.0}, model="x")
ck("a non-JSON judge verdict → grounded=None (UNJUDGED, not a crash)",
   v.get("grounded") is None and "unparseable" in v.get("why", ""), f"got {v}")

# ── scan.collect: a source returning a malformed record is normalized ──────────────────────────────────────────
class _FakeSrc:
    NAME = "fake"

    def read(self, days=None):
        return {"total_usd": "not-a-number", "days": ["2026-08-01"], "sessions": 3, "error": None}  # malformed total


_sources.transcript_sources = lambda: [("fake", _FakeSrc())]
data = scan.collect()
rec = data["fake"]
ck("a malformed source record is normalized (total coerced, keys present)",
   rec["total_usd"] == 0.0 and rec["days"] == ["2026-08-01"] and rec["sessions"] == 3
   and rec["projects"] == {} and rec["models"] == {}, f"got {rec}")
try:
    scan.render(data)
    rendered = True
except Exception:
    rendered = False
ck("scan.render over a normalized malformed record does not raise", rendered)

print(f"\n{'[FAIL]' if fails else 'OK'} test_cat5_parse_robustness: {fails} failure(s)")
sys.exit(1 if fails else 0)
