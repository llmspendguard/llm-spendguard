"""Offline test for the deterministic advisor (Layer 1) — _rows / evidence / advise / main — isolated home.

NO network, NO LLM. advise() is the DETERMINISTIC evidence layer: it reads the local `calls` corpus
(seeded here via calls.insert) and ranks models by cost-efficiency (or, when quality is labeled,
$/good-result). The caged reasoning advisor (Layer 2) is a SEPARATE module and is never touched here.
We pass as_of/plan args + a seeded corpus so everything stays on the deterministic path.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-advise-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import advise, calls

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


# ─────────────────────────── empty corpus path ───────────────────────────
print("-- empty corpus: no data, returns 0 --")
check("advise on empty corpus returns 0", advise.advise(intent="nope") == 0)
check("evidence on empty corpus is empty dict", advise.evidence(intent="nope") == {})
check("_rows on empty corpus is empty", advise._rows(intent="nope") == [])


# ─────────────────────────── seed the calls corpus ───────────────────────────
# Two models for intent 'loinc-typing'. gpt-5.5 is cheaper per output token AND per good result;
# opus is pricier. Some calls quality-labeled so the LABELED ($/good) ranking path runs.
print("-- seed calls corpus (cost + quality labels) --")

# gpt-5.5: 3 jobs, total $3, 600K out tok; 2 labeled good (conf 0.95), so cheap per good
calls.insert("openai", "gpt-5.5", "batch", 1.0, in_tok=100_000, out_tok=200_000,
             ts="2026-06-10T10:00:00", intent="loinc-typing", quality="good", quality_conf=0.95)
calls.insert("openai", "gpt-5.5", "batch", 1.0, in_tok=100_000, out_tok=200_000,
             ts="2026-06-11T10:00:00", intent="loinc-typing", quality="good", quality_conf=0.95)
calls.insert("openai", "gpt-5.5", "batch", 1.0, in_tok=100_000, out_tok=200_000,
             ts="2026-06-12T10:00:00", intent="loinc-typing")          # unlabeled

# opus: 2 jobs, total $6, 200K out tok; 1 labeled good → far pricier per good result
calls.insert("anthropic", "claude-opus-4-8", "batch", 3.0, in_tok=50_000, out_tok=100_000,
             ts="2026-06-10T12:00:00", intent="loinc-typing", quality="good", quality_conf=0.9)
calls.insert("anthropic", "claude-opus-4-8", "batch", 3.0, in_tok=50_000, out_tok=100_000,
             ts="2026-06-13T12:00:00", intent="loinc-typing", quality="bad", quality_conf=0.9)

# A spendguard meta call — MUST be excluded from analysis (intent LIKE 'spendguard:%')
calls.insert("anthropic", "claude-opus-4-8", "realtime", 99.0, in_tok=10, out_tok=10,
             ts="2026-06-10T01:00:00", intent="spendguard:advise")

# A different intent, so 'all intents' aggregation has more than one bucket
calls.insert("openai", "gpt-5.5", "batch", 0.5, in_tok=10_000, out_tok=20_000,
             ts="2026-06-10T15:00:00", intent="ddx-rank")


# ─────────────────────────── _rows: filters ───────────────────────────
print("-- _rows: meta excluded, intent + as_of filters --")
all_rows = advise._rows()
check("_rows excludes spendguard:* meta calls",
      all(r[2] != "spendguard:advise" for r in all_rows))
check("_rows all-intents sees both intents",
      {r[2] for r in all_rows} == {"loinc-typing", "ddx-rank"})

intent_rows = advise._rows(intent="loinc-typing")
check("_rows intent filter → only loinc-typing", all(r[2] == "loinc-typing" for r in intent_rows))
check("_rows intent filter → 5 loinc rows (meta excluded)", len(intent_rows) == 5)

# as_of cutoff: only loinc rows with ts date <= 2026-06-11 → gpt(06-10,06-11) + opus(06-10) = 3
asof_rows = advise._rows(as_of="2026-06-11", intent="loinc-typing")
check("_rows as_of cutoff drops later calls", len(asof_rows) == 3)


# ─────────────────────────── evidence: aggregation ───────────────────────────
print("-- evidence: per-model jobs / cost / labeled / good --")
agg = advise.evidence(intent="loinc-typing")
check("evidence has both models", set(agg) == {"gpt-5.5", "claude-opus-4-8"})
check("gpt-5.5 jobs == 3", agg["gpt-5.5"]["jobs"] == 3)
check("gpt-5.5 cost == 3.0", abs(agg["gpt-5.5"]["cost"] - 3.0) < 1e-9)
check("gpt-5.5 outtok == 600K", agg["gpt-5.5"]["outtok"] == 600_000)
# gpt-5.5: 2 good @0.95 → good == 1.9, labeled == 1.9
check("gpt-5.5 weighted good == 1.9", abs(agg["gpt-5.5"]["good"] - 1.9) < 1e-9)
check("gpt-5.5 labeled == 1.9", abs(agg["gpt-5.5"]["labeled"] - 1.9) < 1e-9)
# opus: 1 good + 1 bad, both @0.9 → labeled 1.8, good 0.9
check("opus jobs == 2", agg["claude-opus-4-8"]["jobs"] == 2)
check("opus labeled == 1.8 (good+bad)", abs(agg["claude-opus-4-8"]["labeled"] - 1.8) < 1e-9)
check("opus weighted good == 0.9", abs(agg["claude-opus-4-8"]["good"] - 0.9) < 1e-9)


# ─────────────────────────── advise: labeled ranking ($/good) ───────────────────────────
print("-- advise: labeled path ranks by $/good-result, flags the plan delta --")
# gpt-5.5 $/good = 3.0 / 1.9 ≈ 1.58 ; opus $/good = 6.0 / 0.9 ≈ 6.67 → gpt-5.5 wins
rc = advise.advise(intent="loinc-typing", plan="claude-opus-4-8")
check("advise (labeled, with plan) returns 0", rc == 0)
# plan == best → no delta line, but still returns 0
rc_best = advise.advise(intent="loinc-typing", plan="gpt-5.5")
check("advise with plan==best returns 0", rc_best == 0)
# plan not in the corpus → skips the delta branch, still 0
rc_unknown = advise.advise(intent="loinc-typing", plan="model-not-present")
check("advise with unknown plan returns 0", rc_unknown == 0)


# plan present but UNLABELED while another model IS labeled → delta falls back to $/M-out (advise.py 74-75)
print("-- advise: labeled-overall but plan has no good labels → $/M-out delta fallback --")
# intent 'mix': haiku is labeled good (so labeled_any True + haiku wins on $/good);
# gpt-5.5 present but has ZERO good labels → pr[4] is None → elif pr[2] and br[2] branch.
calls.insert("anthropic", "claude-haiku-4-5", "batch", 0.2, in_tok=10_000, out_tok=20_000,
             ts="2026-06-10T10:00:00", intent="mix", quality="good", quality_conf=0.95)
calls.insert("openai", "gpt-5.5", "batch", 2.0, in_tok=10_000, out_tok=20_000,
             ts="2026-06-10T11:00:00", intent="mix")          # unlabeled, pricier per out tok
rc_mixdelta = advise.advise(intent="mix", plan="gpt-5.5")
check("advise plan-unlabeled delta ($/M-out fallback) returns 0", rc_mixdelta == 0)


# ─────────────────────────── advise: cost-only (unlabeled) path ───────────────────────────
print("-- advise: unlabeled intent ranks by $/M out only --")
# ddx-rank has NO quality labels → labeled_any is False → cost-only ranking branch + the warning
rc_cost = advise.advise(intent="ddx-rank")
check("advise on unlabeled intent returns 0", rc_cost == 0)

# all-intents view (no filter) → mixes labeled + unlabeled; labeled_any True overall
rc_all = advise.advise()
check("advise all-intents returns 0", rc_all == 0)


# ─────────────────────────── advise: as_of backtest path ───────────────────────────
print("-- advise: as_of replay (deterministic backtest) --")
rc_asof = advise.advise(intent="loinc-typing", as_of="2026-06-11")
check("advise as_of returns 0", rc_asof == 0)
rc_asof_empty = advise.advise(intent="loinc-typing", as_of="2020-01-01")
check("advise as_of before any data returns 0 (empty path)", rc_asof_empty == 0)


# ─────────────────────────── main (CLI entrypoint) ───────────────────────────
print("-- main: CLI arg parsing → advise --")
check("main --intent returns 0", advise.main(["--intent", "loinc-typing"]) == 0)
check("main --intent --plan --as-of returns 0",
      advise.main(["--intent", "loinc-typing", "--plan", "claude-opus-4-8", "--as-of", "2026-06-13"]) == 0)
check("main no args (all intents) returns 0", advise.main([]) == 0)


print(f"\n{'[FAIL]' if failures else 'OK'} test_advise: {failures} failure(s)")
sys.exit(1 if failures else 0)
