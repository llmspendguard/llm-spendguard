"""Phase-1 guard: `claude-code ingest` writes the app's OWN turns into spend_events as est-VALUE, never as billed $,
and never double-counts. This pins the exact bugs found while building it:

  1. est_chat is PLAN-COVERED value → billed=0. charge_to_event hardcoded billed=1 (built for real charges); an
     est_chat row inheriting billed=1 would falsely read as real money. The whole point of the feature is that these
     turns are est-value UNTIL the weekly cap overflow (Phase 2) — so billed MUST be 0 here.
  2. The money lands in est_chat_usd (the value axis), stays OUT of the billed total (spent_dec), and conv_id/cache
     tokens are carried per turn (Phase 3 reads them).
  3. IDEMPOTENT on a stable dedup_key ("cc:<message.id>"): a replay of the same message in another file books ONCE,
     and a full re-scan with the watermark cleared does NOT double-count — the dedup_key, not the watermark, is the
     correctness mechanism.
  4. An unpriceable turn (the '<synthetic>' marker conv-synth writes, which pricing RAISES on) is skipped PER TURN,
     never dropping the rest of the session.

Hermetic: isolated SPENDGUARD_HOME + a fabricated transcript dir, pricing monkeypatched deterministic. Zero spend.
"""
import os, sys, tempfile, json
from decimal import Decimal

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-ccingest-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

# CC transcript dir derived from the isolated HOME — set HERE (not only inside the block above) so it holds whether
# this test self-re-execs OR the pytest runner pre-isolated it via SPENDGUARD_TEST_ISOLATED (which skips the block).
os.environ["SPENDGUARD_CC_DIR"] = os.path.join(os.environ["SPENDGUARD_HOME"], "projects")

from spendguard import claudecode, budget
from spendguard import ledger as L

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── deterministic, hermetic pricing: cost = (in+out)/1000; '<synthetic>' is unpriceable (RAISES, like the real one) ──
def _fake_price(model, in_tok, out_tok, cached=0):
    if model == "<synthetic>":
        raise KeyError("no canonical price for '<synthetic>'")
    return round((in_tok + out_tok) / 1000.0, 6)
claudecode.pricing.realtime_cost = _fake_price

CC = os.environ["SPENDGUARD_CC_DIR"]
os.makedirs(os.path.join(CC, "proj-slug"), exist_ok=True)
UUID = "11111111-2222-3333-4444-555555555555"

def turn(mid, model, i, o, cr, cc, ts):
    return {"type": "assistant", "timestamp": ts, "cwd": "/x/proj-slug",
            "message": {"id": mid, "model": model, "role": "assistant",
                        "usage": {"input_tokens": i, "output_tokens": o,
                                  "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}}}

# transcript 1: a user line + 2 priced turns + 1 unpriceable synthetic turn
p1 = os.path.join(CC, "proj-slug", UUID + ".jsonl")
with open(p1, "w") as f:
    f.write(json.dumps({"type": "user", "cwd": "/x/proj-slug", "message": {"role": "user", "content": "hi"}}) + "\n")
    f.write(json.dumps(turn("m1", "claude-haiku-4-5", 1000, 200, 5000, 300, "2026-08-30T10:00:00Z")) + "\n")
    f.write(json.dumps(turn("m2", "claude-opus-4-8", 2000, 400, 8000, 600, "2026-08-30T10:05:00Z")) + "\n")
    f.write(json.dumps(turn("m3", "<synthetic>", 10, 10, 0, 0, "2026-08-30T10:06:00Z")) + "\n")

# transcript 2 (a resume/branch): REPLAYS m1 with a later mtime → must still book m1 ONCE
p2 = os.path.join(CC, "proj-slug", UUID.replace("1", "9") + ".jsonl")
with open(p2, "w") as f:
    f.write(json.dumps(turn("m1", "claude-haiku-4-5", 1000, 200, 5000, 300, "2026-08-30T10:00:00Z")) + "\n")

claudecode.ingest_events()

led = budget._ledger()
SINCE = "2026-01-01"
rows = [r for r in led.query(since=SINCE) if r.get("source") == "claude-code"]
by_key = {r["dedup_key"]: r for r in rows}

# fake price = (in_tok+out_tok)/1000, and ingest passes in_tok = input+cache_write+cache_read (the FULL context re-read):
M1, M2 = Decimal("6.5"), Decimal("11.0")                             # (1000+300+5000+200)/1e3, (2000+600+8000+400)/1e3
ck("2 priced turns booked (m1,m2); the '<synthetic>' turn is skipped, session not dropped", len(rows) == 2)
ck("dedup_keys are exactly cc:m1 and cc:m2", set(by_key) == {"cc:m1", "cc:m2"})
ck("m1 (replayed in a 2nd file) is booked ONCE — cross-file dedup by message.id",
   sum(1 for r in rows if r["dedup_key"] == "cc:m1") == 1)
ck("value lands in est_chat_usd (the VALUE axis), on every row", all(L.to_dec(r["est_chat_usd"]) > 0 for r in rows))
ck("cost_type derived = est_chat", all(r["cost_type"] == "est_chat" for r in rows))
ck("EVERY claude-code row is billed=0 (plan-covered VALUE, not real $) — the charge_to_event bug",
   all(int(r["billed"]) == 0 for r in rows))
ck("conv_id = the transcript uuid on every row", all(r["conv_id"] == UUID for r in rows))
ck("per-turn cache tokens carried (m2: cache_read=8000, cache_write=600) — Phase-3 signal",
   int(by_key["cc:m2"]["cache_read_tok"]) == 8000 and int(by_key["cc:m2"]["cache_write_tok"]) == 600)

# the SPLIT: est_chat is est-VALUE (est_value_dec), and is EXCLUDED from the billed total (spent_dec)
ck("est_value_dec == m1+m2 = $17.50 (the value axis)", Decimal(led.est_value_dec(since=SINCE)) == M1 + M2)
ck("spent_dec == $0 — est_chat NEVER enters the real billed total", Decimal(led.spent_dec(since=SINCE)) == 0)

# IDEMPOTENCY — the correctness mechanism is the dedup_key, not the watermark. Clear the watermark and re-scan:
n1 = len(rows)
st = claudecode._load_state(); st["ledger_sessions"] = {}; claudecode._save_state(st)
claudecode.ingest_events()
rows2 = [r for r in led.query(since=SINCE) if r.get("source") == "claude-code"]
ck("re-scan with the watermark CLEARED does NOT double-count — dedup_key holds", len(rows2) == n1)
ck("rows == distinct dedup_key (no duplicate identities)",
   len(rows2) == len({r["dedup_key"] for r in rows2}))

print(("[OK]" if not fails else "[FAIL]") + " claude-code-ledger-ingest: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
