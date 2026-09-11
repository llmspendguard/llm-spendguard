"""The reasoning-model BUDGET TRAP: hidden reasoning tokens consume max_tokens before a visible word is written, so
an output cap sized from the VISIBLE answer (a caller's max_tokens_per=1200 from card length) yields an EMPTY reply
that certifies as 'no card' and re-fires every run (measured ~90% of a describe backlog). spendguard must SELF-HEAL
this — the caller should not have to know a model reasons.

Locked here:
  • PREEMPTIVE FLOOR — a model that always reasons (models.reasons_by_default) is floored to TOKEN_FLOOR even when the
    caller passed a small explicit max_tokens, so it succeeds on the FIRST call (a ceiling is billed on ACTUAL tokens);
  • BACKSTOP HEAL — even a model we do NOT know reasons: an empty reply (out_tok>0, no text) JUMPS to the reasoning
    floor and is NOT abandoned when `retries` runs out (retries bounds long-answer doublings, not the reasoning hump);
  • LEARNING ends the re-fire — the empty attempt is recorded TRUNCATED (censored), the successful heal's large out_tok
    becomes the learned recommend, so the NEXT call of the class starts high enough and never empties again;
  • PROBES stay bounded — _probe=True skips both the floor and the growth (a reachability probe is one tiny shot);
  • ORDINARY truncation still respects `retries` (a genuinely long answer is not grown without bound).
Offline: the raw dispatch (_call_once), served-substitute, input-fit, output-ceiling are stubbed — no network, no spend.
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
_budgets = []         # every max_tokens the raw leg was actually called with (proves floor + growth)


def _fake_call_once(model, prompt, max_tokens=None, **kw):
    """Simulate a reasoning model: below NEED the whole cap is eaten by hidden thinking → a CLEAN empty reply
    (out_tok>0, finish_reason='stop', text=''); at/above NEED it emits the answer, out_tok counting the reasoning."""
    _budgets.append(int(max_tokens))
    base = {"provider": "openai", "model": model.split(":", 1)[-1], "cost": 0.01, "executor": "api", "error": None,
            "latency": 0.1, "in_tok": 10}
    if int(max_tokens) >= NEED:
        return {**base, "text": "REAL-ANSWER", "out_tok": NEED, "finish_reason": "stop"}
    return {**base, "text": "", "out_tok": 8, "finish_reason": "stop"}     # reasoning ate it → empty visible answer


# ── shared stubs (offline) ──
adapters._call_once = _fake_call_once
adapters._input_fits = lambda *a, **k: (True, "")
adapters._book_substitution = lambda *a, **k: None
vendor_call.served_substitute = lambda v, m: (m, None)
adapters.pricing.output_ceiling = lambda vendor, model, backstop: 128000     # a generous model ceiling
_REASONS = {"on": True}
models.reasons_by_default = lambda m: _REASONS["on"]

FLOOR = adapters.TOKEN_FLOOR


def _run(**kw):
    _budgets.clear()
    return adapters.call("openai:gpt-5-nano", "describe this", **kw)


# ── PREEMPTIVE FLOOR: a known reasoning model + a small explicit cap → floored, succeeds on the FIRST call ──
_REASONS["on"] = True
r = _run(max_tokens=1200, sig="describe-preempt")
ck("a reasoning model's small explicit cap is floored to TOKEN_FLOOR up front", _budgets and _budgets[0] >= FLOOR)
ck("...so it answers on the FIRST call (no empty, no retry)", r.get("text") == "REAL-ANSWER" and len(_budgets) == 1)

# ── BACKSTOP HEAL: a model we do NOT know reasons — the empty reply still jumps to the floor and recovers ──
_REASONS["on"] = False
r = _run(max_tokens=1200, sig="describe-backstop", retries=2)
ck("an unknown-reasoning empty reply is NOT floored preemptively (first call uses the caller's cap)", _budgets[0] == 1200)
ck("...but the empty reply JUMPS to the reasoning floor and recovers the answer", r.get("text") == "REAL-ANSWER" and max(_budgets) >= FLOOR)

# ── retries does NOT bound a reasoning-empty: even retries=0 grows past the hump ──
_REASONS["on"] = False
r = _run(max_tokens=1200, sig="describe-retries0", retries=0)
ck("a reasoning-empty grows even with retries=0 (retries bounds long-answer doublings, not the reasoning hump)",
   r.get("text") == "REAL-ANSWER" and len(_budgets) >= 2)

# ── LEARNING ends the re-fire: the successful heal's out_tok becomes the learned recommend (empty sample censored) ──
_sig_key = bulkgate.sig("openai:gpt-5-nano", template_id="describe-backstop")
_rec = int((bulkgate.maxtokens(_sig_key) or {}).get("recommend") or 0)
ck("the learned recommend reflects the REAL need (empty sample censored, not learned as an 8-token output)", _rec >= NEED)

# ── PROBES stay bounded: _probe=True skips the floor AND the growth (one tiny shot) ──
_REASONS["on"] = True
r = _run(max_tokens=1200, sig="describe-probe", _probe=True)
ck("a probe is NOT floored (stays at the caller's tiny cap)", _budgets[0] == 1200)
ck("...and a probe does NOT grow on empty (one shot; returns text=None, never balloons)", r.get("text") is None and len(_budgets) == 1)

# ── ORDINARY truncation (a genuinely long answer, not reasoning-empty) still respects `retries` ──
_trunc_budgets = []


def _always_truncates(model, prompt, max_tokens=None, **kw):
    _trunc_budgets.append(int(max_tokens))
    return {"provider": "openai", "model": "m", "text": "partial…", "out_tok": int(max_tokens), "cost": 0.01,
            "executor": "api", "error": None, "latency": 0.1, "in_tok": 10, "finish_reason": "length"}


adapters._call_once = _always_truncates
_REASONS["on"] = False
r = _run(max_tokens=1000, sig="describe-trunc", retries=2)
ck("a genuine (non-empty) truncation still stops after `retries` doublings — not grown without bound",
   r.get("text") is None and len(_trunc_budgets) == 3)      # initial + 2 retries

print(("[OK]" if not fails else "[FAIL]") + " reasoning budget heal: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
