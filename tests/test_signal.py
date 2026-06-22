"""signal.py — the efficiency roll-up (per project · intent · model: cost / good-rate / waste + a recommendation).
Tests the pure recommend() rules + build()'s aggregation math (batch cost counted ONCE per group, good-rate, waste)
with seeded call_io + stubbed provider/conv helpers. Offline, isolated home. Script-style."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-signal-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import signal, callio, backfill, conv

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# recommend() is a closure inside build(); its three rules (low good-rate, opus-heavy, tiny-prompts) are exercised
# through build() below on seeded data.

# ── seed call_io + stub provider batch costs + project mapping, then assert build() math ──
def _seed(intent, model, batch, custom_id, quality, in_tok, out_tok):
    with callio._lock if hasattr(callio, "_lock") else _NullCtx():
        callio._db().execute(
            "INSERT OR REPLACE INTO call_io (id,ts,intent,provider,model,batch,custom_id,prompt,output,in_tok,out_tok,quality,source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{batch}:{custom_id}", "2026-06-10T00:00:00", intent, "openai", model, batch, custom_id,
             "p", "o", in_tok, out_tok, quality, "batch_io"))
        callio._db().commit()

class _NullCtx:
    def __enter__(self): return self
    def __exit__(self, *a): return False

# two calls in batch B1 (intent=extract, gpt-5.5): one good, one bad → good_rate 0.5
_seed("extract", "gpt-5.5", "B1", "c1", "good", 100, 50)
_seed("extract", "gpt-5.5", "B1", "c2", "bad", 100, 50)
# one call in batch B2 (intent=summarize, claude-opus-4-8): unjudged
_seed("summarize", "claude-opus-4-8", "B2", "c3", None, 200, 80)

# stub batch billing: B1 cost $4 (counted ONCE for the extract group), B2 cost $30
backfill._openai_rows = lambda: [("openai", "gpt-5.5", 4.0, 200, 100, "2026-06-10", "B1"),
                                 ("openai", "claude-opus-4-8", 30.0, 200, 80, "2026-06-10", "B2")]
backfill._anthropic_rows = lambda: []
# deterministic project mapping by intent/model
conv._project_of = lambda s: {"extract": "lmm", "summarize": "lmm", "gpt-5.5": "lmm", "claude-opus-4-8": "lmm"}.get((s or "").lower(), "")
# keep it OFFLINE: cancellation_rows() (called inside build() too) must not hit the provider. Stub the batch fetch
# to empty so the test is hermetic + deterministic regardless of any OPENAI_API_KEY in the environment.
from spendguard import reconcile_openai
reconcile_openai.load_key = lambda: ""
reconcile_openai.fetch_batches = lambda _k=None: []

rows = {(r["intent"], r["model"]): r for r in signal.build(since="2026-01-01")}

ex = rows.get(("extract", "gpt-5.5"))
ck("build: extract group present, 2 calls", ex and ex["calls"] == 2)
ck("build: batch cost counted ONCE ($4 = 4_000_000 micros), not per-call", ex and ex["cost_micros"] == 4_000_000)
ck("build: good_rate = good/judged = 0.5", ex and abs(ex["good_rate"] - 0.5) < 1e-9 and ex["judged"] == 2)
ck("build: waste = cost × (1 − good_rate) = $2", ex and ex["waste_micros"] == 2_000_000)
ck("build: recommend fires on low good-rate (0.5 < 0.7)", ex and "low good-rate" in ex["recommendation"])

su = rows.get(("summarize", "claude-opus-4-8"))
ck("build: unjudged group → good_rate None, waste 0", su and su["good_rate"] is None and su["waste_micros"] == 0)
ck("build: opus-heavy spend (>$20) → A/B recommendation", su and "A/B a cheaper model" in su["recommendation"])
ck("build: tokens summed", su and su["tokens_in"] == 200 and su["tokens_out"] == 80)

# cancellation_rows() with no provider key → fetch_batches raises → [] (offline-safe, no crash)
ck("cancellation_rows is [] offline (no provider key)", signal.cancellation_rows() == [])

print(("\n[FAIL] " if fails else "\n[OK] ") + f"signal: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
