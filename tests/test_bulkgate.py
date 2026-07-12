"""Bulk test-first gate (bulkgate) — the enforcement contract.

Makes it structurally impossible to run a BULK paid LLM job without a zero-spend estimate + a verified small-sample
test. These lock the spec's contract: block w/o flags · pass w/ fresh estimate+test · preview allowed w/o flags ·
re-block on stale flags · re-block when the sig changes (model / template-version) · unverified test doesn't unblock ·
force + warn + off behave + log. Offline, isolated home, zero spend.
"""
import os, sys, tempfile, time, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-bg-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

os.environ["SPENDGUARD_ENFORCE"] = "block"   # strict mode (raises) — set ALWAYS (the runner pre-sets ISOLATED, which
                                             # skips the block above, so this must be unconditional)

from spendguard import bulkgate, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def raised(fn):
    try:
        fn(); return False
    except bulkgate.GateBlocked:
        return True

OPUS, HAIKU = "claude-opus-4-8", "claude-haiku-4-5"
sig_o = bulkgate.sig(OPUS, template_id="judge", template_version="v1", schema_name="verdict")
sig_h = bulkgate.sig(HAIKU, template_id="judge", template_version="v1", schema_name="verdict")
sig_o_v2 = bulkgate.sig(OPUS, template_id="judge", template_version="v2", schema_name="verdict")

# ── 1. preview is always allowed (it IS the test step): small count + trivial $ ──
ck("preview (<=preview_max, <=bulk_min_usd) allowed without any flags",
   bulkgate.check_bulk(sig_o, OPUS, count=10, est_usd=0.10) == "preview")

# ── 2. bulk blocked without flags (block mode) ──
ck("bulk run with NO estimate/test → GateBlocked", raised(lambda: bulkgate.check_bulk(sig_o, OPUS, 5000, 12.0)))

# ── 3. estimate + verified test unblocks ──
bulkgate.record_estimate(sig_o, OPUS, est_usd=12.0, est_count=5000)
ck("estimate alone does NOT unblock (test still missing)", raised(lambda: bulkgate.check_bulk(sig_o, OPUS, 5000, 12.0)))
bulkgate.record_tested(sig_o, test_n=20, verified=True)
ck("fresh estimate + verified test → passes", bulkgate.check_bulk(sig_o, OPUS, 5000, 12.0) == "pass")

# ── 4. sig includes MODEL — testing Haiku does NOT authorize Opus (and vice-versa) ──
ck("sig is model-specific (haiku != opus)", sig_h != sig_o)
ck("a different model's flags don't authorize this one", raised(lambda: bulkgate.check_bulk(sig_h, HAIKU, 5000, 8.0)))

# ── 5. sig includes TEMPLATE VERSION — changing it re-blocks (no 'tested v1, ran v2') ──
ck("template-version change → new sig", sig_o_v2 != sig_o)
ck("v2 (untested) blocks even though v1 is tested", raised(lambda: bulkgate.check_bulk(sig_o_v2, OPUS, 5000, 12.0)))

# ── 6. unverified test does NOT unblock ──
sig_u = bulkgate.sig("gpt-5.5", template_id="x", template_version="v1", schema_name="s")
bulkgate.record_estimate(sig_u, "gpt-5.5", 3.0, 4000)
bulkgate.record_tested(sig_u, test_n=10, verified=False)
ck("estimate + UNVERIFIED test still blocks", raised(lambda: bulkgate.check_bulk(sig_u, "gpt-5.5", 4000, 3.0)))

# ── 7. STALE flags re-block (freshness) ──
old = time.time() - (bulkgate.freshness_hours() * 3600 + 600)   # older than the freshness window
import sqlite3
db = sqlite3.connect(config.db_path())
db.execute("UPDATE gate_ledger SET estimated_at=?, tested_at=? WHERE sig=?", (old, old, sig_o)); db.commit(); db.close()
ck("stale estimate+test → re-blocks", raised(lambda: bulkgate.check_bulk(sig_o, OPUS, 5000, 12.0)))

# ── 8. force overrides AND logs (never silent) ──
blocks_path = os.path.join(os.path.dirname(config.db_path()), "gate_blocks.jsonl")
before = os.path.getsize(blocks_path) if os.path.exists(blocks_path) else 0
ck("GATE_FORCE=1 overrides the block", bulkgate.check_bulk(sig_o, OPUS, 5000, 12.0, force=True) == "allow:force")
after = os.path.getsize(blocks_path) if os.path.exists(blocks_path) else 0
ck("...and the override is LOGGED (not silent)", after > before)

# ── 9. warn mode allows but logs 'would-block'; off mode allows silently ──
os.environ["SPENDGUARD_ENFORCE"] = "warn"
ck("warn mode: bulk without flags is allowed (would-block)", bulkgate.check_bulk(sig_h, HAIKU, 5000, 8.0) == "allow:warn")
os.environ["SPENDGUARD_ENFORCE"] = "off"
ck("off mode: enforcement disabled", bulkgate.check_bulk(sig_h, HAIKU, 5000, 8.0) == "allow:off")
os.environ["SPENDGUARD_ENFORCE"] = "block"

# ── 10. gated_batch enforces the order (can't run before estimate+test) ──
ran = {"submitted": False}
with bulkgate.gated_batch(sig_n := bulkgate.sig("opus", template_id="flow", template_version="v1", schema_name="s"), "opus") as job:
    ck("gated_batch.run BEFORE estimate/test → blocks", raised(lambda: job.run(9000, 20.0, lambda: ran.__setitem__("submitted", True))))
    job.estimate(20.0, 9000)
    job.test(15, run_fn=lambda n: [1] * n, verify_fn=lambda out: len(out) == 15)
    job.run(9000, 20.0, lambda: ran.__setitem__("submitted", True))
ck("gated_batch.run AFTER estimate+verified test → submits", ran["submitted"] is True)

# ── 11. realtime BURST gate: first preview_max calls allowed (the test sample), beyond → estimate+test enforced ──
os.environ["SPENDGUARD_ENFORCE"] = "block"
sig_rt = bulkgate.sig(OPUS, template_id="rtloop", template_version="v1", schema_name="s")
pm = bulkgate.preview_max()
burst_ok = all(bulkgate.check_realtime(sig_rt, OPUS, est_usd=0.05) == "preview" for _ in range(pm))
ck("realtime burst within preview_max → allowed (the test sample)", burst_ok)
ck("realtime burst BEYOND preview_max, no flags → blocks", raised(lambda: bulkgate.check_realtime(sig_rt, OPUS, est_usd=0.05)))
bulkgate.record_estimate(sig_rt, OPUS, 5.0, 200)
bulkgate.record_tested(sig_rt, pm, verified=True)
ck("realtime burst with fresh estimate+test → passes", bulkgate.check_realtime(sig_rt, OPUS, est_usd=0.05) == "pass")

# ── 12. max_tokens TRUNCATION detection (the API states it — a fact, not a guess) + data-driven bounds ──
ck("is_truncated: anthropic stop_reason=max_tokens", bulkgate.is_truncated("max_tokens"))
ck("is_truncated: openai finish_reason=length", bulkgate.is_truncated("length"))
ck("is_truncated: out_tok hitting the cap exactly", bulkgate.is_truncated("stop", out_tok=240, max_tokens=240))
ck("is_truncated: a clean stop is NOT truncated", not bulkgate.is_truncated("stop", out_tok=100, max_tokens=240))

sig_mt = bulkgate.sig(OPUS, template_id="cards", template_version="v1", schema_name="card")
for o in (250, 256, 300, 331, 362, 478):                  # observed clean outputs (a real describe-card job's shape)
    bulkgate.note_response(sig_mt, OPUS, o, max_tokens=550, finish_reason="stop")
bulkgate.note_response(sig_mt, OPUS, 240, max_tokens=240, finish_reason="length")    # one truncation
mt = bulkgate.maxtokens(sig_mt, current_max=240)
ck("maxtokens: counts the truncation", mt["truncations"] == 1)
ck("maxtokens: recommends ~p99*1.5 (measured, not guessed)", mt["recommend"] >= mt["p99"] and mt["recommend"] > 240)
ck("maxtokens: warns max_tokens 240 < p95 (TRUNCATION RISK)", "TRUNCATION RISK" in (mt["warn"] or ""))

# ── 13. a TRUNCATED test sample → verified=False → the bulk run still BLOCKS (the killer integration) ──
sig_tt = bulkgate.sig(OPUS, template_id="trunc", template_version="v1", schema_name="s")
bulkgate.record_estimate(sig_tt, OPUS, 10.0, 5000)
def _trunc_run(k):
    for _ in range(k):
        bulkgate.note_response(sig_tt, OPUS, 240, max_tokens=240, finish_reason="length")   # every sample truncates
    return [1] * k
bulkgate.test_job(sig_tt, _trunc_run, n=5)
ck("truncated sample → verified=False → bulk still BLOCKS", raised(lambda: bulkgate.check_bulk(sig_tt, OPUS, 5000, 10.0)))

# ── 14. REMOTE-COMPUTE gate (same estimate+test rule, on the compute-$ axis) ──
sig_c = bulkgate.sig("gpu:a100", template_id="render", template_version="v1", schema_name="frames")
ck("compute: trivial $ → allowed", bulkgate.check_compute(sig_c, est_usd=0.20) == "trivial")
ck("compute: big launch, no estimate/test → blocks", raised(lambda: bulkgate.check_compute(sig_c, est_usd=50.0, hours=8)))
bulkgate.record_estimate(sig_c, "compute", 50.0, 1)
bulkgate.record_tested(sig_c, 1, verified=True)
ck("compute: big launch with estimate+test → passes", bulkgate.check_compute(sig_c, est_usd=50.0, hours=8) == "pass")

# ── 15. warn-mode realtime burst logs ONCE per sig (dedup — a big un-adopted loop must not spam) ──
os.environ["SPENDGUARD_ENFORCE"] = "warn"
sig_wd = bulkgate.sig(OPUS, template_id="warnloop", template_version="v1", schema_name="s")
for _ in range(bulkgate.preview_max() + 5):                # well past the test sample, in warn mode
    bulkgate.check_realtime(sig_wd, OPUS, est_usd=0.05)
blocks_path = os.path.join(os.path.dirname(config.db_path()), "gate_blocks.jsonl")
wb = 0
if os.path.exists(blocks_path):
    for ln in open(blocks_path):
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("sig") == sig_wd and o.get("decision") == "would-block":
            wb += 1
ck("warn-mode burst logged ONCE despite preview_max+5 calls (dedup)", wb == 1)
os.environ["SPENDGUARD_ENFORCE"] = "block"

print(("[OK]" if not fails else "[FAIL]") + " bulkgate: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
