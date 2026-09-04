"""Item completeness (ARITY) for packed id-keyed envelopes — the check per-item shape validation CANNOT do.

warden PACKS N assets into ONE request and demuxes ONE {results:[{id,…}]} envelope back by id; its measured failure
(2026-06-26) is that packed batches silently OMIT ~7% of ids — an envelope of N−1 shape-perfect items passes every
per-item check, so a lane returning it was accepted as a $0 success and the items lost. This guards the fix:
output_contract.check_envelope (the owned arity primitive) + bulk_delegate(expect_ids=…), which turns an incomplete
envelope into a retried MISS rather than a silent success. Offline: adapters.call + dispatch stubbed, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-arity-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import output_contract as oc, lane_balance, lane_catalog, lane_bandit, adapters, dispatch, lane_economics

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── check_envelope: the owned arity primitive (counts ids — FORMAT, not meaning) ──
EXP = ["a1", "a2", "a3"]
full = '{"results":[{"id":"a1"},{"id":"a2"},{"id":"a3"}]}'
ck("a COMPLETE envelope → ok", oc.check_envelope(full, EXP)[0] is True)
ok, d = oc.check_envelope('{"results":[{"id":"a1"},{"id":"a3"}]}', EXP)
ck("a DROPPED id → not ok, names the missing id", ok is False and d["missing"] == ["a2"] and d["n_got"] == 2)
ck("a DUPLICATED id → not ok", oc.check_envelope('{"results":[{"id":"a1"},{"id":"a1"},{"id":"a2"},{"id":"a3"}]}', EXP)[1]["dupes"] == ["a1"])
ck("a HALLUCINATED id → not ok (extra)", oc.check_envelope('{"results":[{"id":"a1"},{"id":"a2"},{"id":"a3"},{"id":"zz"}]}', EXP)[1]["extra"] == ["zz"])
ck("an UNPARSEABLE envelope → not ok, never a crash", oc.check_envelope("not json at all", EXP)[0] is False)
ck("a fenced envelope is salvaged then arity-checked", oc.check_envelope("```json\n" + full + "\n```", EXP)[0] is True)

# ── bulk_delegate(expect_ids=…): a dropped-item lane envelope becomes a retried MISS, never a silent $0 success ──
lane_catalog.arms = lambda flt=None: [("gemini", "g-high")]
lane_catalog.lane_provider = lambda l: {"gemini": "gemini"}.get(l)
lane_bandit._arm_cooling = lambda l, u: False
lane_bandit.arm_stats = lambda intent: {("gemini", "g-high"): {"winrate": 1.0, "trials": 2}}
lane_economics.prompt_lane_reserved = lambda lane: False
adapters._lane_cooling = lambda ln: False
dispatch.acquire = lambda *a, **k: 0.0
dispatch.release = lambda *a, **k: None


def _envelope(ids):
    return '{"results":[' + ",".join('{"id":"%s"}' % x for x in ids) + ']}'


# a lane that DROPS the last id of every packed task (the tasks encode their ids as "a1|a2|a3")
adapters.call = lambda model, prompt, **kw: {"text": _envelope(prompt.split("|")[:-1]), "cost": 0}
tasks = ["a1|a2|a3"]

res = lane_balance.bulk_delegate(tasks, "warden:pack", expect_ids=lambda t: t.split("|"))
ck("a lane envelope that DROPS an id is NOT a silent success (text cleared, error row)",
   res[0].get("text") is None and res[0].get("error") and "INCOMPLETE" in res[0]["error"])
ck("the miss carries the arity detail (which id was missing)", res[0].get("arity_miss", {}).get("missing") == ["a3"])

# WITHOUT expect_ids the same dropped-item envelope is accepted — this IS the hole expect_ids closes
res2 = lane_balance.bulk_delegate(tasks, "warden:pack")
ck("without expect_ids the incomplete envelope is accepted (the hole the guard closes)",
   res2[0].get("text") is not None and not res2[0].get("error"))

# resume RETRIES the arity miss and, once the lane answers completely, it finishes (never silently 'done')
ckp = os.path.join(os.environ["SPENDGUARD_HOME"], "arity.jsonl")
lane_balance.bulk_delegate(tasks, "warden:pack", expect_ids=lambda t: t.split("|"), checkpoint=ckp)   # miss → error line
adapters.call = lambda model, prompt, **kw: {"text": _envelope(prompt.split("|")), "cost": 0}          # now complete
r3 = lane_balance.bulk_delegate(tasks, "warden:pack", expect_ids=lambda t: t.split("|"), checkpoint=ckp)
ck("an arity miss is RETRIED on resume and now completes (never counted 'done')",
   r3[0].get("text") is not None and not r3[0].get("error"))

print(("[OK]" if not fails else "[FAIL]") + " item completeness: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
