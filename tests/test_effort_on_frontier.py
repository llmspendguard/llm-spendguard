"""Phase 0 guard — reasoning EFFORT is a first-class axis on the cost×quality frontier.

Before this, `calls` had no effort column, so per-effort rows silently MERGED and "which effort is
cheapest-at-quality for this intent" had zero storage. These tests pin:
  · calls.insert / record_call persist an `effort` column (schema migration holds)
  · advise.ranked(by_effort=False) is UNCHANGED — one row per model, efforts summed (CLI/recommend shape)
  · advise.ranked(by_effort=True) splits per (model, effort) and ranks by $/good, so the cheapest effort
    that HOLDS quality sorts first — the signal the best-value selector reads
  · a legacy row (effort=None) still ranks (as an effort=None group), never dropped
  · id stays 'vendor:model' in BOTH modes (effort rides its own field, never fused into the id)
"""
import os, sys, tempfile
os.environ.setdefault("SPENDGUARD_CALLS", "1")           # exercise the gated record_call path (set even when the
#                                                         runner pre-isolates and the re-exec below is skipped)
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    _self = os.path.realpath(__file__)                   # contain the spawn: re-exec THIS file, validated under tests/
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import calls, advise

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


INTENT = "t:classify"
# Same model, two efforts — BOTH hold quality, but 'low' is 5x cheaper. The whole point: 'low' must win.
calls.insert("openai", "gpt-5.5", "realtime", 0.10, in_tok=500, out_tok=1000, intent=INTENT,
             quality="good", quality_src="judge", quality_conf=0.9, effort="high")
calls.insert("openai", "gpt-5.5", "realtime", 0.02, in_tok=500, out_tok=1000, intent=INTENT,
             quality="good", quality_src="judge", quality_conf=0.9, effort="low")
# A legacy row for a different model — effort was never recorded (None). Must still rank.
calls.insert("openai", "gpt-5-mini", "realtime", 0.01, in_tok=400, out_tok=500, intent=INTENT,
             quality="good", quality_src="judge", quality_conf=0.9, effort=None)

print("-- ranked(by_effort=False): UNCHANGED per-model shape (efforts merged) --")
r0 = advise.ranked(intent=INTENT)
by_id0 = {m["id"]: m for m in r0["models"]}
check("gpt-5.5 appears exactly once (two efforts merged into one model row)",
      sum(1 for m in r0["models"] if m["id"] == "openai:gpt-5.5") == 1)
check("no 'effort' field leaks into the per-model shape", "effort" not in by_id0["openai:gpt-5.5"])
check("merged gpt-5.5 cost is the sum of both efforts ($0.12)",
      abs(by_id0["openai:gpt-5.5"]["cost"] - 0.12) < 1e-9)

print("-- ranked(by_effort=True): split per (model, effort), ranked by $/good --")
r1 = advise.ranked(intent=INTENT, by_effort=True)
g55 = [m for m in r1["models"] if m["id"] == "openai:gpt-5.5"]
check("gpt-5.5 now appears twice — once per effort", len(g55) == 2)
check("both efforts present {high, low}", {m["effort"] for m in g55} == {"high", "low"})
check("id stays 'vendor:model' (effort NOT fused into the id)", all(m["id"] == "openai:gpt-5.5" for m in g55))
_low = next(m for m in g55 if m["effort"] == "low")
_high = next(m for m in g55 if m["effort"] == "high")
check("cheaper effort has the lower $/good", _low["per_good"] < _high["per_good"])
# The cheapest effort that holds quality must sort ahead of the pricier one for the SAME model.
check("'low' sorts before 'high' in the ranking", r1["models"].index(_low) < r1["models"].index(_high))

print("-- legacy row (effort=None) still ranks --")
mini = [m for m in r1["models"] if m["id"] == "openai:gpt-5-mini"]
check("gpt-5-mini present as an effort=None group", len(mini) == 1 and mini[0].get("effort") is None)

print("-- record_call persists effort (schema column + gated write path) --")
cid = calls.record_call("openai", "gpt-5.5", "realtime", 0.03, in_tok=100, out_tok=200,
                        intent="t:other", effort="medium")
check("record_call returned an id (logging enabled)", bool(cid))
with calls._lock:
    row = calls._calls_db().execute("SELECT effort FROM calls WHERE id=?", (cid,)).fetchone()
check("stored effort round-trips as 'medium'", row and row[0] == "medium")

print(f"\n{'[FAIL]' if failures else 'OK'} test_effort_on_frontier: {failures} failure(s)")
sys.exit(1 if failures else 0)
