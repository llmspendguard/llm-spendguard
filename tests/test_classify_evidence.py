"""Unified agentic recall (conv.classify_evidence) — the no-silent-drops guard.

The twin of resolve(): one recorded Haiku pass that BOTH reconcile (cost_lesson) and the realtime reconstruction
(spend_evidence + kind) consume, replacing the per-path keyword pre-filters (_SIG / history._SIGNAL / _RT_TELL) that
decided relevance BY REGEX and silently dropped evidence. These tests lock in:
  • spend evidence phrased with NO problem-topic keyword (no fix/bug/cancel/wrong) is still caught + classified —
    the exact failure the old keyword filters caused,
  • a chunk with ZERO cost-domain signal is skipped DETERMINISTICALLY (never sent to the LLM),
  • the LLM decides meaning (mocked here) — the regex only does a broad cost-domain candidate cut,
  • results are RECORDED → a re-run (and the other consumer) reads the cache free,
  • estimate-first: run=False spends nothing.

Offline, isolated home, zero real spend (the LLM call is mocked).
"""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ev-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import conv
import spendguard.adapters as adapters

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# MOCK the LLM (offline): classify from chunk content so we test the WIRING (candidate cut → LLM → parse → record),
# not a real model. spend_evidence iff a $/token shape is present; cost_lesson iff a decision phrase is present.
CALLS = {"n": 0, "seen": 0}
def mock_call(model, body, max_tokens=None, system=None, **kw):
    CALLS["n"] += 1
    items = []
    for ln in body.splitlines():
        if ":" not in ln:
            continue
        idx, txt = ln.split(":", 1)
        try: i = int(idx.strip())
        except ValueError: continue
        CALLS["seen"] += 1
        t = txt.lower()
        spend = ("$" in txt) or ("tokens" in t) or (" in /" in t)
        items.append({"i": i, "spend_evidence": spend, "kind": "realtime" if spend else "none",
                      "cost_lesson": ("don't cancel" in t or "lesson" in t or "estimate first" in t)})
    return {"text": json.dumps({"items": items})}
adapters.call = mock_call

# ── 1. spend evidence WITHOUT any problem-topic keyword (the old _SIG/_RT_TELL would have dropped this) ──
chunks = [
    {"id": "a", "text": "the sharded runner printed $213.75 for 24000 requests"},   # spend $, no fix/bug/cancel word
    {"id": "b", "text": "we should refactor the parser for clarity sometime"},       # ZERO cost-domain signal
    {"id": "c", "text": "lesson: don't cancel a running batch — completed requests still bill"},  # cost lesson, no $
]
res = conv.classify_evidence(chunks, run=True)
ck("spend evidence w/o topic keyword caught + classed realtime (NO silent drop)",
   res["a"]["spend_evidence"] is True and res["a"]["kind"] == "realtime")
ck("cost-free prose skipped DETERMINISTICALLY (never reached the LLM)",
   res["b"] == conv._NONE_EV and CALLS["seen"] == 2)        # only a + c were candidates → only 2 items hit the mock
ck("cost lesson caught (feeds reconcile-insights) even with no $/token",
   res["c"]["cost_lesson"] is True)

# ── 2. RECORDED: a re-run reads the cache, no new LLM call (both consumers read free) ──
n_before = CALLS["n"]
res2 = conv.classify_evidence(chunks, run=True)
ck("re-run reads recorded cache — zero new LLM calls",
   CALLS["n"] == n_before and res2["a"]["spend_evidence"] is True and res2["c"]["cost_lesson"] is True)

# ── 3. estimate-first: run=False spends nothing ──
CALLS["n"] = 0
out = conv.classify_evidence([{"id": "d", "text": "that run cost $9.00"}], run=False)
ck("estimate-first: run=False makes ZERO LLM calls", CALLS["n"] == 0)

# ── 4. the candidate cut is broad cost-DOMAIN, not problem-topic (anti-regression) ──
ck("broad cut: a bare '$5.50' is a candidate", bool(conv._EVIDENCE_CANDIDATE.search("it was $5.50")))
ck("broad cut: 'cancelled the batch' is a candidate (lesson recall)", bool(conv._EVIDENCE_CANDIDATE.search("cancelled the batch")))
ck("broad cut: pure prose is NOT a candidate", not conv._EVIDENCE_CANDIDATE.search("let us refactor the parser"))

print(("[OK]" if not fails else "[FAIL]") + " classify-evidence: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
