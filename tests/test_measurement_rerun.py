"""Measurement receipts (c): rerun a reading's INSTRUMENT for a comparable number, and FLAG drift.

`compare` is the $0 drift-flag: two readings are comparable iff they share an instrument_id (same judge + sample +
rubric + candidates); otherwise it names the ruler change via STRUCTURED `drift` codes (so 0.88→0.85 that is really
'different judge' is flagged, not read as a quality drop). `rerun` pins the reading's judge + replays its sample
(recovered from the corpus by item_id) + reuses its rubric → a child reading comparable to the baseline;
estimate-first (no budget → 0 spend); if the sample can't be reproduced it REFUSES (reason='sample_unreproducible')
rather than compare across a changed instrument. Offline: adapters/pricing stubbed, corpus populated via callio.
Assertions are on STRUCTURED codes (drift/reason), never substrings of prose.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-rerun-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import measurement, adapters, pricing, bakeoff, callio

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


RUB = {"system": "judge", "schema": {"type": "object"}}


def _reading(judge, val, sample=("s1",), cands=("m1",)):
    return measurement.record_reading(intent="i", kind="bakeoff", judge_mix=[judge], sample_ids=list(sample),
                                      rubric=RUB, candidates=list(cands), values={"m1": {"good_rate": val}})


# ── compare: same instrument → comparable + delta; a changed ruler → NOT comparable, drift code named ──
a = _reading("anthropic:haiku", 0.9)
b = _reading("anthropic:haiku", 0.7)                              # same instrument, later number
cmp_ab = measurement.compare(a, b)
ck("same judge+sample+rubric+candidates → comparable", cmp_ab["comparable"] is True and cmp_ab["drift"] == [])
ck("comparable → per-candidate delta (0.9 → 0.7 = -0.2)", abs(cmp_ab["delta"]["m1"]["delta"] + 0.2) < 1e-9)
ck("comparable → a noise caveat (a single rerun isn't significance)", bool(cmp_ab.get("caveat")))
c_judge = _reading("anthropic:haiku-5", 0.7)                      # DIFFERENT judge
cmp_drift = measurement.compare(a, c_judge)
ck("a different judge → NOT comparable (drift flagged)", cmp_drift["comparable"] is False and cmp_drift["delta"] is None)
ck("...and the drift CODE names 'judge' (structured, not prose)", "judge" in cmp_drift["drift"])
c_rub = measurement.record_reading(intent="i", kind="bakeoff", judge_mix=["anthropic:haiku"], sample_ids=["s1"],
                                   rubric={"system": "DIFFERENT", "schema": {}}, candidates=["m1"], values={"m1": {"good_rate": 0.9}})
ck("a changed rubric → NOT comparable, drift code 'rubric'", "rubric" in measurement.compare(a, c_rub)["drift"])

# ── rerun: pin the judge, replay the sample, child comparable to the baseline ──
def _fake_call(model, prompt, **kw):
    if kw.get("sig") == "spendguard:bakeoff-judge":
        return {"text": '{"good": true}', "cost": 0.0001, "provider": "anthropic", "model": "claude-haiku-4-5"}
    return {"text": "out", "cost": 0.002, "in_tok": 10, "out_tok": 8,
            "provider": model.split(":")[0], "model": model.split(":")[-1]}


adapters.call = _fake_call
pricing.realtime_cost = lambda *a, **k: 0.001
SAMPLE = ["prompt one", "prompt two"]
for p in SAMPLE:                                                  # populate the corpus so the sample is recoverable
    callio.record_io_sample("rr-intent", "openai", "openai:gpt-5-nano", "batch1", "cid-" + p, p, "out")

JUDGE = "anthropic:claude-haiku-4-5"
base = bakeoff.bakeoff("rr-intent", candidates=["openai:gpt-5-nano", "gemini:g-flash"], prompts=SAMPLE,
                       judge_model=JUDGE, run=True, budget_usd=10.0)
bid = base.get("reading_id")
ck("baseline reading records a PINNED judge", bid and measurement.get_reading(bid)["judge_pinned"] is True)

est = measurement.rerun(bid, budget_usd=None)
ck("rerun without budget → estimate only (0 spend), pinned judge named", "estimate" in est and est.get("pinned_judge") == JUDGE)

rr = measurement.rerun(bid, budget_usd=10.0, same_sample=True)
ck("rerun records a CHILD reading linked to the baseline", rr.get("reading_id") and rr.get("baseline") == bid)
child = measurement.get_reading(rr["reading_id"]) if rr.get("reading_id") else {}
ck("the child's parent_id is the baseline (lineage)", child.get("parent_id") == bid)
ck("the child pins the SAME judge", child.get("judge_mix") == [JUDGE] and child.get("judge_pinned") is True)
ck("rerun reproduced the instrument → child is COMPARABLE to the baseline", rr["comparison"]["comparable"] is True)

# ── rerun REFUSES when the sample can't be reproduced (never a false comparison across a changed instrument) ──
orphan = measurement.record_reading(intent="orphan-intent", kind="bakeoff", judge_mix=[JUDGE],
                                    sample_ids=["hash_not_in_corpus"], rubric=RUB, candidates=["m1"],
                                    values={"m1": {"good_rate": 0.5}})
miss = measurement.rerun(orphan, budget_usd=10.0, same_sample=True)
ck("rerun with an unreproducible sample → reason='sample_unreproducible' (structured, no false compare)",
   miss.get("reason") == "sample_unreproducible")

print(("[OK]" if not fails else "[FAIL]") + " measurement rerun/compare: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
