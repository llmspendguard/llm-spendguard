"""Measurement receipts (b): a judged number carries a reproducible provenance — judge mix, sample, rubric —
addressable by reading_id, and comparable via instrument_id.

Grounds the fix: bakeoff's judge was RETURN-ONLY (never persisted), so a past score had no recoverable judge mix.
Now bakeoff STAMPS a receipt; `inspect` reads it; `reconstruct` rebuilds a best-effort reading for a PAST run from
the calls corpus with the judge marked 'unknown' (never guessed). Offline: adapters/pricing stubbed, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-meas-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import measurement, calls

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


RUBRIC = {"system": "judge good/bad", "schema": {"type": "object", "required": ["good"]}}
CANDS = ["openai:gpt-5-nano", "gemini:g-flash"]

# ── instrument_id: the RULER's identity — same inputs same id; a different judge/rubric/sample is a DIFFERENT ruler ──
base = dict(intent="i", kind="bakeoff", judge_mix=["anthropic:haiku"], sample_ids=["a", "b"], rubric=RUBRIC,
            candidates=CANDS, values={"openai:gpt-5-nano": 0.9})


def _rec(**over):
    kw = dict(base)
    kw.update(over)
    return measurement.get_reading(measurement.record_reading(**kw))


r1 = _rec()
r2 = _rec(values={"openai:gpt-5-nano": 0.7})                      # same instrument, different number (a time series)
ck("two readings with the same judge+sample+rubric+candidates share an instrument_id (comparable)",
   r1["instrument_id"] == r2["instrument_id"])
ck("...but have distinct reading_ids (two points in the series)", r1["reading_id"] != r2["reading_id"])
r_judge = _rec(judge_mix=["anthropic:haiku-5"])
ck("a DIFFERENT judge → a different instrument_id (NOT comparable)", r_judge["instrument_id"] != r1["instrument_id"])
r_rub = _rec(rubric={"system": "different question", "schema": {}})
ck("a DIFFERENT rubric → a different instrument_id", r_rub["instrument_id"] != r1["instrument_id"])
r_samp = _rec(sample_ids=["a", "b", "c"])
ck("a DIFFERENT sample → a different instrument_id", r_samp["instrument_id"] != r1["instrument_id"])

# ── the receipt round-trips with its provenance intact ──
ck("reading carries the judge mix", r1["judge_mix"] == ["anthropic:haiku"])
ck("reading carries judge_basis (configured vs served) — a reader is never misled about what ran",
   "judge_basis" in r1)
ck("reading carries the rubric hash + values", r1["rubric_hash"] and r1["values"]["openai:gpt-5-nano"] == 0.9)
ck("inspect(unknown id) → None (never invents a judge)", measurement.inspect("rd_nope") is None)

# ── reconstruct: a PAST bakeoff (rows in the corpus) → good_rate per model, judge UNKNOWN (not guessed) ──
for q in ("good", "good", "bad"):
    calls.insert("openai", "openai:gpt-5-nano", "realtime", 0.001, intent="recon-intent", quality=q, who="bakeoff")
for q in ("good", "bad", "bad"):
    calls.insert("gemini", "gemini:g-flash", "realtime", 0.001, intent="recon-intent", quality=q, who="bakeoff")
rec = measurement.reconstruct_reading("recon-intent")
ck("reconstruct recovers per-candidate good_rate from the corpus",
   rec and abs(rec["values"]["openai:gpt-5-nano"]["good_rate"] - 2 / 3) < 1e-9)
ck("reconstruct marks the judge UNKNOWN (never guessed)", rec["judge_mix"] is None and "unknown" in rec["judge_note"])
ck("reconstruct of an intent with no bakeoff rows → None", measurement.reconstruct_reading("no-such-intent") is None)

# ── bakeoff EMITS a receipt (integration, stubbed): the number is now reproducible ──
from spendguard import adapters, pricing, bakeoff


def _fake_call(model, prompt, **kw):
    if kw.get("sig") == "spendguard:bakeoff-judge":
        return {"text": '{"good": true}', "cost": 0.0001, "in_tok": 5, "out_tok": 2,
                "provider": "anthropic", "model": "claude-haiku-4-5"}
    return {"text": "candidate output", "cost": 0.002, "in_tok": 10, "out_tok": 8,
            "provider": model.split(":")[0], "model": model.split(":")[-1]}


adapters.call = _fake_call
pricing.realtime_cost = lambda *a, **k: 0.001
res = bakeoff.bakeoff("bake-intent", candidates=CANDS, prompts=["p1", "p2"], run=True, budget_usd=10.0)
ck("bakeoff returns a reading_id", bool(res.get("reading_id")))
reading = measurement.inspect(res["reading_id"])
ck("the emitted receipt records the configured judge + basis",
   reading and reading["judge_mix"] and reading["judge_basis"] == "configured")
ck("the emitted receipt records the candidates + rubric hash + a good_rate value",
   reading and reading["candidates"] == sorted(CANDS) and reading["rubric_hash"] and reading["values"])

print(("[OK]" if not fails else "[FAIL]") + " measurement receipt: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
