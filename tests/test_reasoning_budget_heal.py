"""A reasoning model's OUTPUT BUDGET is the CEILING, so hidden reasoning tokens can never starve a real answer.

Hidden reasoning tokens consume max_tokens before a visible word is written, so an output cap sized from the VISIBLE
answer (a caller's max_tokens_per=1200) once yielded an EMPTY reply that certified as 'no card' and re-fired every run
(measured ~90% of a describe backlog). The doctrine fix (docs/CANONICAL_CONCERNS.json: output_budget) is simple:
spendguard IGNORES the caller's cap and sends the model's CEILING (max_output is billed on ACTUAL tokens, so the max is
free). A reasoning model then has all the headroom it needs and answers on the FIRST call — no upward heal, because the
call already starts at the max.

Locked here:
  • CEILING BUDGET — a reasoning model + a small caller cap → the cap is IGNORED, the ceiling is sent, it answers first try;
  • JUMBO SURFACED — when the model's REAL ceiling is genuinely too small for reasoning+answer, the empty reply is
    SURFACED as text=None (never a silent ''), in ONE attempt (there is nothing larger to grow into);
  • LEARNING — the successful call's out_tok becomes the learned recommend (an ESTIMATE input, a separate concern);
  • PROBES — _probe=True keeps the caller's deliberate tiny cap (one bounded shot), never the ceiling.
Offline: raw dispatch (_call_once), served-substitute, input-fit, output-ceiling stubbed — no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-reasonbudget-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, vendor_call, models, bulkgate      # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


NEED = 20000          # the model needs this many completion tokens (reasoning + answer) before a visible word appears
_budgets = []         # every max_tokens the raw leg was actually called with (proves the ceiling is sent)


def _fake_call_once(model, prompt, max_tokens=None, **kw):
    """Simulate a reasoning model: below NEED the whole cap is eaten by hidden thinking → a CLEAN empty reply
    (out_tok>0, finish_reason='stop', text=''); at/above NEED it emits the answer, out_tok counting the reasoning."""
    _budgets.append(int(max_tokens))
    base = {"provider": "openai", "model": model.split(":", 1)[-1], "cost": 0.01, "executor": "api", "error": None,
            "latency": 0.1, "in_tok": 10}
    if int(max_tokens) >= NEED:
        return {**base, "text": "REAL-ANSWER", "out_tok": NEED, "finish_reason": "stop"}
    return {**base, "text": "", "out_tok": 8, "finish_reason": "stop"}     # reasoning ate it → empty visible answer


# ── shared stubs (offline). The output ceiling is variable so we can drive both the roomy case and the jumbo case. ──
_CEIL = {"v": 128000}
adapters._call_once = _fake_call_once
adapters._input_fits = lambda *a, **k: (True, "")
adapters._book_substitution = lambda *a, **k: None
vendor_call.served_substitute = lambda v, m: (m, None)
adapters.pricing.output_ceiling = lambda vendor, model, backstop, **kw: _CEIL["v"]   # the model's ceiling (tunable)
_REASONS = {"on": True}
models.reasons_by_default = lambda m: _REASONS["on"]


def _run(**kw):
    _budgets.clear()
    return adapters.call("openai:gpt-5-nano", "describe this", **kw)


# ── CEILING BUDGET: a reasoning model + a small caller cap → cap IGNORED, ceiling sent, answers on the first call ──
_REASONS["on"] = True
_CEIL["v"] = 128000
r = _run(max_tokens=1200, sig="describe-ceiling")
ck("the small caller cap (1200) is IGNORED — the model's ceiling is sent", _budgets and _budgets[0] == 128000)
ck("...so the reasoning model answers on the FIRST call (no empty, no retry)",
   r.get("text") == "REAL-ANSWER" and len(_budgets) == 1)

# ── JUMBO SURFACED: the model's REAL ceiling is genuinely below the reasoning need → empty at the ceiling → text=None ──
_REASONS["on"] = True
_CEIL["v"] = 8000            # < NEED(20000): even at the max, reasoning+answer does not fit — the jumbo case
r = _run(max_tokens=1200, sig="describe-jumbo")
ck("at the ceiling (8000 < need) the empty reply is SURFACED as text=None, not a silent ''", r.get("text") is None)
ck("...in ONE attempt — there is nothing larger to grow into (the ceiling IS the max)",
   len(_budgets) == 1 and _budgets[0] == 8000)

# ── LEARNING: a successful call's out_tok becomes the recommend (the ESTIMATE input — a different concern from the budget) ──
_CEIL["v"] = 128000
_run(sig="describe-learn")                                  # succeeds first call, out_tok=NEED
_sig_key = bulkgate.sig("openai:gpt-5-nano", template_id="describe-learn")
_rec = int((bulkgate.maxtokens(_sig_key) or {}).get("recommend") or 0)
ck("the learned recommend reflects the real out_tok (for the estimate, not the budget)", _rec >= NEED)

# ── PROBES: _probe=True keeps the caller's deliberate tiny cap (one bounded shot), never the ceiling ──
_REASONS["on"] = True
_CEIL["v"] = 128000
r = _run(max_tokens=1200, sig="describe-probe", _probe=True)
ck("a probe keeps the caller's tiny cap (1200), not the ceiling", _budgets[0] == 1200)
ck("...one shot, and an empty is surfaced as text=None (never balloons)", r.get("text") is None and len(_budgets) == 1)

print(("[OK]" if not fails else "[FAIL]") + " reasoning budget heal: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
