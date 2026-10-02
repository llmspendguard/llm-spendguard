"""The cold-start model prior (intent_model_prior) fills the gap Ash named — "the advisor has no quality labels for
this intent, it's ranking by cost alone" — with a GROUNDED, AGENTIC prior: for an intent with no measured evidence it
ranks the REAL priced catalog models, labelled source="cold-start-prior" (never passed off as measurement), estimate-
FIRST, PERSISTED so it is never re-paid, and a deliberate spend-stop PROPAGATES. Verified (offline, the ranker LLM call
mocked — zero spend):
  • run=False → a zero-spend estimate (estimate_only, candidates listed), NO adapters.call;
  • run=True → ranks ONLY real catalog ids (a hallucinated id is dropped), labelled prior=True / source;
  • the prior is PERSISTED — a second call returns it WITHOUT a second ranker call (never re-paid);
  • recommend_models on a COLD intent returns the prior (source="cold-start-prior"), not an empty "no pick";
  • a SpendGateRefused from the ranker PROPAGATES (never swallowed into an empty prior).

Offline, isolated SPENDGUARD_HOME, zero spend."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-coldprior-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import intent_model_prior as imp, adapters, gate  # noqa: E402


class Checks:
    def __init__(self):
        self.fails = []

    def __call__(self, label, cond, extra=""):
        if not cond:
            self.fails.append(label)
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck = Checks()

# two REAL priced catalog ids to ground the mocked ranker reply on (from the module's own candidate builder)
_lines, _ids, _per_m_out = imp._candidate_lines()
ck("the catalog yields priced candidates to rank", len(_ids) >= 2, extra=f"n={len(_ids)}")
REAL1, REAL2 = sorted(_ids)[0], sorted(_ids)[1]
HALLUCINATED = "totally-made-up-model-xyz-999"
assert HALLUCINATED not in _ids

# ── A. run=False → a zero-spend ESTIMATE (no ranker call) ─────────────────────────────────────────────────────────────
_calls = {"n": 0}
_real_call = adapters.call
adapters.call = lambda *a, **k: (_calls.__setitem__("n", _calls["n"] + 1) or {"text": "{}", "cost": 0.0})
try:
    est = imp.rank_models_for_intent("cold-intent-A", k=3, run=False)
    ck("run=False is estimate_only", est.get("estimate_only") is True)
    ck("run=False lists real candidates", bool(est.get("candidates")) and REAL1 in est["candidates"])
    ck("run=False is labelled a cold-start prior", est.get("source") == "cold-start-prior" and est.get("prior") is True)
    ck("run=False spends NOTHING (no ranker call)", _calls["n"] == 0, extra=f"calls={_calls['n']}")
finally:
    adapters.call = _real_call

# ── B. run=True → ranks ONLY real ids (hallucination dropped), labelled, persisted ────────────────────────────────────
_real_sr = adapters.structured_reply
_calls = {"n": 0}


def _mock_call(*a, **k):
    _calls["n"] += 1
    return {"text": "mocked", "cost": 0.0012}


adapters.call = _mock_call
adapters.structured_reply = lambda r: {"task_type": "classification",
                                       "top": [{"id": REAL1, "why": "cheap + capable"},
                                               {"id": HALLUCINATED, "why": "not a real id — must be dropped"},
                                               {"id": REAL2, "why": "fallback"}]}
try:
    res = imp.rank_models_for_intent("cold-intent-B", k=5, run=True)
    picked = [t["id"] for t in res.get("top", [])]
    ck("run=True ranks the real ids", REAL1 in picked and REAL2 in picked)
    ck("a hallucinated id is DROPPED (grounded in the real catalog)", HALLUCINATED not in picked)
    ck("the result is labelled a PRIOR, not measurement", res.get("prior") is True and res.get("source") == "cold-start-prior")
    ck("the top pick carries no measured numbers (per_good None — it is a prior)",
       res["top"][0].get("per_good") is None and res["top"][0].get("prior") is True)
    ck("model-of-record = the top prior pick", res.get("model") == picked[0])
    ck("the ranker was called exactly once", _calls["n"] == 1, extra=f"calls={_calls['n']}")

    # ── C. PERSISTED — a second call returns the prior WITHOUT a second ranker call (never re-paid) ──
    def _boom(*a, **k):
        raise AssertionError("ranker must NOT be called again — the prior is persisted")
    adapters.call = _boom
    res2 = imp.rank_models_for_intent("cold-intent-B", k=5, run=True)
    ck("a persisted prior is reused with NO second ranker call (never re-paid)", res2.get("cached") is True)
    ck("the reused prior matches the stored ranking", [t["id"] for t in res2.get("top", [])] == picked)
finally:
    adapters.call = _real_call
    adapters.structured_reply = _real_sr

# ── D. catalog fingerprint moves when the candidate set changes (prior auto-invalidates) ──────────────────────────────
fp1 = imp._catalog_fingerprint(_ids, _per_m_out)
fp2 = imp._catalog_fingerprint(_ids, {**_per_m_out, REAL1: _per_m_out.get(REAL1, 1.0) + 99.0})   # reprice one model
ck("the catalog fingerprint moves when a model is repriced (prior re-derives)", fp1 != fp2)

# ── E. recommend_models on a COLD intent returns the prior (not an empty 'no pick') ───────────────────────────────────
from spendguard import advisor, advise  # noqa: E402
_real_ranked = advise.ranked
_real_rank_prior = imp.rank_models_for_intent
try:
    advise.ranked = lambda intent=None: {"models": []}            # force the COLD branch
    imp.rank_models_for_intent = lambda intent, k=5, run=False: {"intent": intent, "source": "cold-start-prior",
                                                                 "prior": True, "top": [{"id": REAL1, "prior": True}],
                                                                 "model": REAL1, "note": "prior"}
    rec = advisor.recommend_models("brand-new-intent", k=3, run=True)
    ck("recommend_models on a cold intent returns the cold-start prior", rec.get("source") == "cold-start-prior")
    ck("the cold recommendation names a real model (not an empty no-pick)", rec.get("model") == REAL1)
finally:
    advise.ranked = _real_ranked
    imp.rank_models_for_intent = _real_rank_prior

# ── F. a deliberate spend-stop from the ranker PROPAGATES (never swallowed into an empty prior) ───────────────────────
adapters.call = lambda *a, **k: (_ for _ in ()).throw(gate.SpendGateRefused("cap"))
try:
    raised = False
    try:
        imp.rank_models_for_intent("cold-intent-F", k=3, run=True)
    except gate.SpendGateRefused:
        raised = True
    ck("a SpendGateRefused from the ranker propagates (not swallowed)", raised)
finally:
    adapters.call = _real_call

print(f"\n{'OK' if not ck.fails else 'FAIL'} test_cold_start_prior: {len(ck.fails)} failure(s)")
sys.exit(1 if ck.fails else 0)
