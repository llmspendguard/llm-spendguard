"""Guard — provider_tokens estimates TEXT tokens with a REAL BPE base × an AGENTICALLY-chosen provider factor.

The bug it closes: non-OpenAI estimates used an OpenAI tokenizer or a flat chars/4 guess, so anthropic/gemini/
glm forecasts were biased. Pins:
  · count_text("") == 0; a non-empty string → a positive int; the base is a REAL o200k BPE count, not len//4;
  · a stored (agentically-chosen) factor multiplies the o200k base; an UNCALIBRATED provider is factor 1.0;
  · calibrate --dry-run ($0) computes the FULL stat panel (n, median, aggregate, spread) and makes NO LLM call;
  · calibrate(store) hands the panel to the chooser (monkeypatched here) and stores its factor + rationale;
    every returned choice is accounted for — a model-named provider with NO measured basis lands in `ignored`
    (stored + Σignored == choices), and every row is accounted for (used + Σskipped == n_rows);
  · a DELIBERATE stop (SpendGateRefused) is recognized as one and would propagate; a plain error is not.
Offline: the factor CHOICE is monkeypatched — no API calls, no spend."""
import os, sys, tempfile, json

os.environ.setdefault("OPENAI_API_KEY", "sk-test-offline")          # offline: key RESOLUTION must succeed; the factor
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-offline")   #   choice is monkeypatched + test_runner's dead proxy
#   fails any real call in ms — a fake key never bills (its "a test that needs a key sets its own fake one" convention)
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
ck("base IS the real o200k count (not chars/4)", provider_tokens.count_text(code) == len(_o200k.encode(code)))
ck("chars/4 would have been a DIFFERENT number (proves it's not the heuristic)",
   provider_tokens.count_text(code) != max(1, len(code) // 4))

print("-- an AGENTICALLY-chosen factor multiplies the o200k base for a non-OpenAI provider --")
db = provider_tokens._factors_db()
with budget._lock:
    db.execute("INSERT INTO token_factors (provider,factor,confidence,why,char_ratio,n,stats,ts) "
               "VALUES (?,?,?,?,?,?,?,?)",
               ("anthropic", 2.0, 0.9, "leaned on the token-weighted aggregate; n large, spread tight", 3.0, 100,
                json.dumps({"n": 100, "median": 1.9, "aggregate": 2.0}), "2026-09-12T00:00:00+00:00"))
    db.commit()
provider_tokens.reload_factors()
_txt = "the quick brown fox jumps over the lazy dog"
_base = len(_o200k.encode(_txt))
ck("factor('anthropic') is the chosen 2.0 (applied as-is)", provider_tokens.factor("anthropic")[0] == 2.0)
ck("factor basis is 'agentic', confidence + rationale surfaced",
   provider_tokens.factor("anthropic")[1]["basis"] == "agentic"
   and provider_tokens.factor("anthropic")[1]["confidence"] == 0.9
   and "aggregate" in provider_tokens.factor("anthropic")[1]["why"])
ck("count_text applies the factor (≈ 2× the o200k base)",
   provider_tokens.count_text(_txt, provider="anthropic") == max(1, round(_base * 2.0)))
ck("an UNCALIBRATED provider stays factor 1.0 (raw o200k proxy)",
   provider_tokens.factor("gemini")[0] == 1.0 and provider_tokens.count_text(_txt, provider="gemini") == _base)

# 4 usable anthropic rows (≥4 → real quartile spread), 1 with no provider, 1 with empty text — all pass the WHERE.
for i in range(4):
    callio.record_io_sample("t-int", "anthropic", "claude-x", "b1", f"ok-{i}",
                            "system words here", "the answer text " * 5, in_tok=40 + i, out_tok=10)
callio.record_io_sample("t-int", None, "claude-x", "b1", "noprov", "orphan prompt", "out", in_tok=12, out_tok=3)
callio.record_io_sample("t-int", "gemini", "gem-y", "b1", "empty", "", "out", in_tok=7, out_tok=3, system="")

print("-- calibrate --dry-run: full stat panel, row accounting, and NO LLM call --")
_orig_choose = provider_tokens._choose_factors_agentically
def _boom_choose(*a, **k):
    raise AssertionError("dry-run must not invoke the factor chooser")
provider_tokens._choose_factors_agentically = _boom_choose
d = provider_tokens.calibrate(store=False)
provider_tokens._choose_factors_agentically = _orig_choose
ck("dry-run ok, made no chooser call", d.get("ok") is True)
ck("used + Σskipped == n_rows (nothing silently dropped)", d["used"] + sum(d["skipped"].values()) == d["n_rows"])
ck("the no-provider row was skipped, the empty-text row was skipped",
   d["skipped"]["no_provider"] == 1 and d["skipped"]["no_text"] == 1)
ck("anthropic's panel has n=4 with median + aggregate",
   d["stats"].get("anthropic", {}).get("n") == 4 and d["stats"]["anthropic"].get("aggregate") is not None)

print("-- calibrate(store): the chooser's factor is stored; a hallucinated provider is ACCOUNTED (ignored) --")
def _fake_choose(stats_by_provider, model):
    # anthropic is measured; 'phantom' is NOT — the model hallucinated it. cost is a tiny meta cost.
    return ({"anthropic": {"factor": 1.7, "confidence": 0.8, "why": "aggregate, robust n"},
             "phantom":   {"factor": 5.0, "confidence": 0.9, "why": "hallucinated"}}, 0.0012)
provider_tokens._choose_factors_agentically = _fake_choose
r = provider_tokens.calibrate(store=True)
provider_tokens._choose_factors_agentically = _orig_choose
ck("the measured provider's chosen factor was stored", r["chosen"].get("anthropic", {}).get("factor") == 1.7)
ck("the hallucinated provider is in `ignored`, never silently dropped", r["ignored"] == ["phantom"])
ck("stored + Σignored == choices returned", len(r["chosen"]) + len(r["ignored"]) == 2)
ck("factor('anthropic') now reflects the stored agentic choice (1.7)", provider_tokens.factor("anthropic")[0] == 1.7)
ck("meta cost surfaced", r.get("meta_cost") == 0.0012)

print("-- deliberate-stop recognition (refusal propagates, a plain error does not) --")
_refusal = gate.SpendGateRefused.__new__(gate.SpendGateRefused)
ck("a SpendGateRefused is a deliberate stop", provider_tokens._stop_or_locked(_refusal) is True)
ck("a plain ValueError is NOT a deliberate stop", provider_tokens._stop_or_locked(ValueError("x")) is False)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_provider_tokens: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
