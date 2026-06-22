"""AGENTIC per-subconversation attribution — the regression guard.

The 2026-06 incident: batch spend silently fell to 'unattributed' because attribution used a REGEX keyword matcher
(genericized to nlp/vision EXAMPLES) instead of the LLM classifier + cwd prior — and every test passed because the
fixtures were rigged to match the regex. These tests are the never-again guard:

  • fixtures are deliberately NOT regex-shaped (the old _PROJECT_RULES would resolve them to "" / unattributed),
  • the classifier is MOCKED (offline, zero spend) — we assert the AGENTIC RESULT flows to the spend,
  • the core invariant is explicit: EVIDENCED spend (we know the repo) is NEVER '' / 'unattributed'.

Offline, isolated home, zero spend.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-seg-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import conv

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── 1. segment_records: a session in cwd=.../lmm that submits a batch (prompt is NOT regex-shaped) ──
recs = [
    {"type": "user", "cwd": "/Users/x/Documents/claude/lmm", "timestamp": "2026-06-01T10:00:00Z",
     "message": {"role": "user", "content": "kick off the nightly rollup job"}},
    {"type": "assistant", "cwd": "/Users/x/Documents/claude/lmm", "timestamp": "2026-06-01T10:01:00Z",
     "message": {"role": "assistant", "content": "submitted batch_0123456789abcdef0123"}},
]
segs = conv.segment_records(recs, sid="sess-A")
ck("one user ask → one segment", len(segs) == 1)
ck("segment cwd PRIOR = lmm (from the record's cwd, not the regex)", segs[0]["project_prior"] == "lmm")
ck("segment captured the batch id", "batch_0123456789abcdef0123" in segs[0]["batch_ids"])
ck("seg_id is stable + present", bool(segs[0]["seg_id"]))

# ── 2. per-subconversation: ONE session, two asks, two repos → two segments ──
recs2 = recs + [
    {"type": "user", "cwd": "/Users/x/Documents/animepipe/manga2anime", "timestamp": "2026-06-01T11:00:00Z",
     "message": {"role": "user", "content": "clean up the corpus catalog"}},
    {"type": "assistant", "cwd": "/Users/x/Documents/animepipe/manga2anime", "timestamp": "2026-06-01T11:01:00Z",
     "message": {"role": "assistant", "content": "submitted msgbatch_abcdefghij0123456789"}},
]
segs2 = conv.segment_records(recs2, sid="sess-B")
ck("one session spanning two projects → two subconversations", len(segs2) == 2)
ck("segments span lmm + manga2anime", {s["project_prior"] for s in segs2} == {"lmm", "manga2anime"})

# ── 3. batch_project_map: the AGENTIC classification (mocked) flows to each batch ──
_SEGS = [
    {"seg_id": "s1", "sid": "A", "cwd": "/x/lmm", "project_prior": "lmm",
     "prompt": "nightly job", "batch_ids": ["batch_aaaaaaaaaaaaaaaaaaaa"], "ts": "2026-06-01T10:00:00Z", "day": "2026-06-01"},
    {"seg_id": "s2", "sid": "A", "cwd": "/x/manga2anime", "project_prior": "manga2anime",
     "prompt": "corpus catalog", "batch_ids": ["msgbatch_bbbbbbbbbbbbbbbbbb"], "ts": "2026-06-01T11:00:00Z", "day": "2026-06-01"},
    {"seg_id": "s3", "sid": "A", "cwd": "/x/lmm", "project_prior": "lmm",
     "prompt": "not classified yet", "batch_ids": ["batch_cccccccccccccccccccc"], "ts": "2026-06-01T12:00:00Z", "day": "2026-06-01"},
]
conv.segments = lambda tdir=None: _SEGS                      # stand in for the file scan (offline)
# MOCKED agentic result (what classify_items WOULD return) — s1/s2 classified; s3 deliberately NOT (cache miss)
conv._save_seg_cache({"s1": {"org": "Healiom", "team": "LMM", "project": "lmm", "confidence": 90},
                      "s2": {"org": "Ensight", "team": "anime", "project": "manga2anime", "confidence": 88}})
bmap = conv.batch_project_map()
ck("batch → agentic project (lmm/Healiom)",
   bmap["batch_aaaaaaaaaaaaaaaaaaaa"]["project"] == "lmm" and bmap["batch_aaaaaaaaaaaaaaaaaaaa"]["org"] == "Healiom")
ck("batch → agentic project (manga2anime/Ensight)",
   bmap["msgbatch_bbbbbbbbbbbbbbbbbb"]["org"] == "Ensight")
# s3: EVIDENCED (we know the repo=lmm) but no LLM result yet → the cwd PRIOR, never '' / unattributed
ck("evidenced-but-unclassified batch → cwd PRIOR (lmm), NOT unattributed",
   bmap["batch_cccccccccccccccccccc"]["project"] == "lmm")
ck("THE INVARIANT: every evidenced batch has a non-empty project (never silent unattributed)",
   all(b["project"] for b in bmap.values()))
ck("evidenced flag set on all linked batches", all(b.get("evidenced") for b in bmap.values()))

# ── 4. anti-regression: the regex attribution is GONE and cannot silently return ──
# The 2026-06 regression was a keyword matcher (conv._project_of / _PROJECT_RULES). It is deleted; the ONLY resolver
# is the agentic classifier + cwd prior (proved above). If anyone re-introduces regex attribution, this fails.
ck("legacy regex attribution conv._project_of has been REMOVED (agentic-only)",
   not hasattr(conv, "_project_of") and not hasattr(conv, "_PROJECT_RULES"))

# ── 5. PERSISTENCE in the base sqlite: decisions survive + are reused, human beats llm (never redo/re-pay) ──
conv._seg_put_cls("h1", {"project": "lmm", "org": "Healiom", "confidence": 90}, source="llm", model="m")
ck("decision persisted + retrievable from the base sqlite", conv._seg_get_all().get("h1", {}).get("project") == "lmm")
conv._seg_put_cls("h1", {"project": "manga2anime", "org": "Ensight", "confidence": 95}, source="human")
ck("human override wins", conv._seg_get_all()["h1"]["project"] == "manga2anime")
conv._seg_put_cls("h1", {"project": "lmm", "confidence": 99}, source="llm", model="m")   # llm tries to change it
ck("llm NEVER overwrites a human override (durable)", conv._seg_get_all()["h1"]["project"] == "manga2anime")
conv._seg_put_cls("lowc", {"project": "documents", "confidence": 40}, source="llm", model="m")
ck("low-confidence decision recorded (the convergence loop re-runs it)", conv._seg_get_all()["lowc"]["confidence"] == 40)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"segment-attribution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
