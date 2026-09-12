"""Guard — provider_tokens estimates TEXT tokens with a REAL BPE base × a MEASURED per-provider factor.

The bug it closes: non-OpenAI estimates used an OpenAI tokenizer or a flat chars/4 guess, so anthropic/gemini/
glm forecasts were biased. Pins:
  · count_text("") == 0; a non-empty string → a positive int (never 0, never raises);
  · the base is a REAL o200k BPE count, not len//4 (a code/whitespace string differs from its char count / 4);
  · a MEASURED factor (n≥min) multiplies the o200k base for a non-OpenAI provider; an UNCALIBRATED provider is
    factor 1.0 (the raw proxy), never a guess dressed as a measurement;
  · calibrate() accounts for EVERY fetched row — used + Σskipped == n_rows (nothing silently dropped, the I1 guard);
  · a DELIBERATE stop (SpendGateRefused) is recognized as one and would propagate; a plain error is not.
Offline: reads only an isolated call_io / ledger; makes NO API calls."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-ptok-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

import tiktoken
from spendguard import provider_tokens, callio, budget, gate

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

_o200k = tiktoken.get_encoding("o200k_base")

print("-- count_text: real BPE base, never 0 for non-empty, never raises --")
ck("empty → 0", provider_tokens.count_text("") == 0)
code = "def f(x):\n    return x*x  # a comment with    whitespace\n"
ck("a non-empty string → positive int", isinstance(provider_tokens.count_text(code), int) and provider_tokens.count_text(code) > 0)
ck("base IS the real o200k count (not chars/4)",
   provider_tokens.count_text(code) == len(_o200k.encode(code)))       # uncalibrated → factor 1.0 → equals the base
ck("chars/4 would have been a DIFFERENT number (proves it's not the heuristic)",
   provider_tokens.count_text(code) != max(1, len(code) // 4))

print("-- a MEASURED factor multiplies the o200k base for a non-OpenAI provider --")
db = provider_tokens._factors_db()
with budget._lock:
    db.execute("INSERT INTO token_factors (provider,factor,char_ratio,rel_spread,n,ts) VALUES (?,?,?,?,?,?)",
               ("anthropic", 2.0, 3.0, 0.1, 100, "2026-09-12T00:00:00+00:00"))
    db.commit()
provider_tokens.reload_factors()
_txt = "the quick brown fox jumps over the lazy dog"
_base = len(_o200k.encode(_txt))
ck("factor('anthropic') is the measured median 2.0 (applied as-is)", provider_tokens.factor("anthropic")[0] == 2.0)
ck("factor basis is 'measured', n + spread surfaced for audit",
   provider_tokens.factor("anthropic")[1]["basis"] == "measured"
   and provider_tokens.factor("anthropic")[1]["n"] == 100
   and provider_tokens.factor("anthropic")[1]["rel_spread"] == 0.1)
ck("count_text applies the factor (≈ 2× the o200k base)",
   provider_tokens.count_text(_txt, provider="anthropic") == max(1, round(_base * 2.0)))
ck("an UNCALIBRATED provider stays factor 1.0 (raw o200k proxy)",
   provider_tokens.factor("gemini")[0] == 1.0 and provider_tokens.count_text(_txt, provider="gemini") == _base)

print("-- calibrate: every fetched row is accounted for (used + Σskipped == n_rows) --")
# 4 usable anthropic rows (≥4 → real quartile spread), 1 with no provider, 1 with empty text — all pass the WHERE.
for i in range(4):
    callio.record_io_sample("t-int", "anthropic", "claude-x", "b1", f"ok-{i}",
                            "system words here", "the answer text " * 5, in_tok=40 + i, out_tok=10)
callio.record_io_sample("t-int", None, "claude-x", "b1", "noprov", "orphan prompt", "out", in_tok=12, out_tok=3)
callio.record_io_sample("t-int", "gemini", "gem-y", "b1", "empty", "", "out", in_tok=7, out_tok=3, system="")
r = provider_tokens.calibrate(store=False)
ck("calibrate ok", r.get("ok") is True)
ck("used + Σskipped == n_rows (nothing silently dropped)",
   r["used"] + sum(r["skipped"].values()) == r["n_rows"])
ck("the no-provider row was counted as skipped", r["skipped"]["no_provider"] == 1)
ck("the empty-text row was counted as skipped", r["skipped"]["no_text"] == 1)
ck("anthropic got a measured factor from its 4 rows, with a spread",
   any(p["provider"] == "anthropic" and p["n"] == 4 and p["rel_spread"] is not None for p in r["providers"]))

print("-- deliberate-stop recognition (refusal propagates, a plain error does not) --")
_refusal = gate.SpendGateRefused.__new__(gate.SpendGateRefused)      # instance without constructor args
ck("a SpendGateRefused is a deliberate stop", provider_tokens._stop_or_locked(_refusal) is True)
ck("a plain ValueError is NOT a deliberate stop", provider_tokens._stop_or_locked(ValueError("x")) is False)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_provider_tokens: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
