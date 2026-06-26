"""AGENTIC per-subconversation attribution — the regression guard.

The 2026-06 incident: batch spend silently fell to 'unattributed' because attribution used a REGEX keyword matcher
(genericized to nlp/vision EXAMPLES) instead of the LLM classifier + cwd prior — and every test passed because the
fixtures were rigged to match the regex. These tests are the never-again guard:

  • fixtures are deliberately NOT regex-shaped (the old _PROJECT_RULES would resolve them to "" / unattributed),
  • the classifier is MOCKED (offline, zero spend) — we assert the AGENTIC RESULT flows to the spend,
  • the core invariant is explicit: EVIDENCED spend (we know the repo) is NEVER '' / 'unattributed'.

Offline, isolated home, zero spend.
"""
import os, sys, tempfile, json

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
    # s4: ran IN the lmm repo (prior=lmm) but the work was actually manga2anime — the human used the "wrong" chat.
    {"seg_id": "s4", "sid": "A", "cwd": "/x/lmm", "project_prior": "lmm",
     "prompt": "scene-graph captioning for the anime fleet", "batch_ids": ["batch_dddddddddddddddddddd"], "ts": "2026-06-01T13:00:00Z", "day": "2026-06-01"},
]
conv.segments = lambda tdir=None: _SEGS                      # stand in for the file scan (offline)
# MOCKED agentic result (what classify_items WOULD return) — s1/s2 classified; s3 deliberately NOT (cache miss);
# s4 = the OVERRIDE: prior is lmm (it ran there) but the CONTENT is manga2anime, so the classifier returns manga2anime.
conv._save_seg_cache({"s1": {"org": "Healiom", "team": "lmm", "project": "lmm", "confidence": 90},
                      "s2": {"org": "manga2anime", "team": "engineering", "project": "manga2anime", "confidence": 88},
                      "s4": {"org": "manga2anime", "team": "engineering", "project": "manga2anime", "confidence": 86}})
bmap = conv.batch_project_map()
ck("batch → agentic project (lmm/Healiom)",
   bmap["batch_aaaaaaaaaaaaaaaaaaaa"]["project"] == "lmm" and bmap["batch_aaaaaaaaaaaaaaaaaaaa"]["org"] == "Healiom")
ck("batch → agentic project (manga2anime)",
   bmap["msgbatch_bbbbbbbbbbbbbbbbbb"]["org"] == "manga2anime")
# THE OVERRIDE (the core agentic point): s4 RAN in the lmm repo (prior=lmm) but its work was manga2anime — the
# classifier OVERRIDES the cwd prior, so the batch follows the CONTENT, not the repo it happened to run in. A human
# using the "wrong" chat for some work must NOT mis-attribute the spend.
ck("LLM OVERRIDES the cwd prior: lmm-repo batch whose work is manga2anime → manga2anime, NOT the lmm prior",
   bmap["batch_dddddddddddddddddddd"]["project"] == "manga2anime" and bmap["batch_dddddddddddddddddddd"]["org"] == "manga2anime")
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
# the LLM's full DETERMINATION is remembered (conv id + segment + what it classified) → never re-pay, can re-derive
conv._seg_put_cls("det1", {"project": "lmm", "org": "Healiom", "team": "LMM", "confidence": 88},
                  source="llm", model="claude-x", seg={"sid": "S9", "prompt": "nightly", "batch_ids": ["batch_zzzzzzzzzzzzzzzzzzzz"]})
rec = conv.seg_record("det1")
ck("determination stored (the LLM's classification, as JSON)", bool(rec) and (rec["determination"] or {}).get("project") == "lmm")
ck("seg_record keeps conv id + model + source (decide redo-when-needed)", rec["sid"] == "S9" and rec["model"] == "claude-x" and rec["source"] == "llm")

# ── 6. session_classification: the SHARED primitive for NON-batch units (GPU instance, remote realtime) — the
#       conversation that launched the box / ran the fleet rolls up to its dominant org/project (highest confidence) ──
conv._seg_put_cls("sc-a", {"project": "manga2anime", "org": "Ensight", "team": "anime", "confidence": 92},
                  source="llm", model="m", seg={"sid": "SESS1", "prompt": "fleet caption run"})
conv._seg_put_cls("sc-b", {"project": "manga2anime", "org": "Ensight", "confidence": 70},
                  source="llm", model="m", seg={"sid": "SESS1", "prompt": "more"})
sc = conv.session_classification("SESS1")
ck("session_classification rolls a conversation up to its dominant org/project (GPU + realtime use this)",
   bool(sc) and sc["org"] == "ensight" and sc["project"] == "manga2anime")   # taxonomy names are case-insensitive → lowercase
ck("session_classification: unclassified session → None (never a fake attribution)",
   conv.session_classification("NO-SUCH-SESSION") is None)

# ── 7. remote-realtime reconstruction (vast.ai box LLM calls): batch ids EXCLUDED (realtime must NOT re-count batch),
#       attributed via session_classification, deduped. classifier mocked (offline, zero spend). ──
from spendguard import resources, adapters
conv.remote_llm_excerpts = lambda tdir=None, max_sessions=None: [("SESS1", "box ran haiku on clips; cross-check msgbatch_01VW")]
conv._seg_put_cls("rt1", {"project": "manga2anime", "org": "Ensight", "confidence": 90}, source="llm",
                  seg={"sid": "SESS1", "prompt": "fleet caption run"})
# the LLM extracts TOKENS (in/out × scale); the SYSTEM prices them via pricing.py (cost basis), not the LLM
adapters.call = lambda *a, **k: {"error": None, "cost": 0.0, "text":
    '{"runs":[{"model":"haiku","kind":"realtime","calls":2526,"in_tokens":5000000,"out_tokens":500000,"executed":true,"evidence":"2526 clips x 5 frames haiku","confidence":80},'
    '{"model":"opus","kind":"realtime","in_tokens":1000,"out_tokens":500,"executed":true,"evidence":"cross-check opus msgbatch_01VW","confidence":85},'
    '{"model":"sonnet","kind":"realtime","in_tokens":1000,"out_tokens":100,"executed":false,"evidence":"planned only","confidence":90}]}'}
rr = resources.reconstruct_remote_llm(run=True, model_org_hints={"haiku": "Ensight"})
ck("remote realtime PRICED from tokens via pricing.py (>0, not an LLM-stated $)", rr["total"] > 0)
ck("remote realtime keeps the executed haiku token run", any(x["model"] == "haiku" for x in rr["rows"]))
ck("remote realtime EXCLUDES batch-id runs (msgbatch in evidence → no double-count)",
   all("msgbatch" not in (x.get("evidence") or "") for x in rr["rows"]))
ck("remote realtime drops PLANNED/not-executed runs", all(x["model"] != "sonnet" for x in rr["rows"]))
ck("remote realtime attributed to the session's org (ensight)", rr["by_org"].get("ensight", 0) > 0)

# ── 8. GPU TIMING MATCH: a vast.ai instance's run window ⨝ the conversation active then → org/project. This is the
#       "combine vast.ai cost (window) + LLM attribution" join: vast gives the window; the matched conversation the org. ──
import datetime as _dt
_t = _dt.datetime(2026, 6, 10, 12, 0, 0, tzinfo=_dt.timezone.utc)
conv.segments = lambda tdir=None: [{"sid": "GSESS", "seg_id": "g1", "ts": _t.isoformat(),
                                    "prompt": "fleet box run", "batch_ids": [], "project_prior": "manga2anime", "cwd": "/x"}]
conv._seg_put_cls("g1", {"project": "manga2anime", "org": "Ensight", "confidence": 90}, source="llm",
                  seg={"sid": "GSESS", "prompt": "fleet box run"})
att = conv.instance_attributions([{"id": "7777777", "label": "caption", "start_date": _t.timestamp() - 3600, "end_date": _t.timestamp() + 3600}])
ck("GPU instance timing-matched to the conversation active in its window → ensight", att.get("7777777", {}).get("org") == "ensight")
att2 = conv.instance_attributions([{"id": "8888888", "label": "x", "start_date": _t.timestamp() + 99999, "end_date": _t.timestamp() + 199999}])
ck("GPU instance with NO overlapping conversation → unmatched (never a fake attribution)", "8888888" not in att2)

# ── 9. conversation-derived realtime tally (NO admin key): prices PRINTED token usage, skips batch-id lines ──
import tempfile as _tf, os as _os
_td = _tf.mkdtemp(prefix="sg-rt-"); _os.makedirs(_os.path.join(_td, "p"))
with open(_os.path.join(_td, "p", "s.jsonl"), "w") as _f:
    _f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
             "content": "=== USAGE === 1000000 in / 200000 out  (opus realtime judge)"}}) + "\n")
    _f.write(json.dumps({"type": "assistant", "message": {"role": "assistant",
             "content": "submitted msgbatch_01ZZ1234567890 : 500000 in / 100000 out (batch — must skip)"}}) + "\n")
rt = conv.realtime_token_tally(tdir=_td)
ck("realtime tally prices PRINTED realtime token usage (>0, via pricing.py)", rt["total"] > 0)
ck("realtime tally SKIPS batch-id usage lines (only the 1 realtime call counts)", rt["calls"] == 1)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"segment-attribution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
