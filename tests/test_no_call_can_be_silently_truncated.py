"""THE max_tokens REGRESSION SUITE.

This defect came back three times over more than a week — not because any fix was wrong, but because
nothing failed when it returned. Every check here exists so that the specific hole that let it back in is
now a red test. If you are reading this because a test below failed, the failure is the point: something
re-introduced a cap that can silently cut an answer in half.

Why silent: you are billed for tokens GENERATED, so a low cap saves no money. What it does is truncate the
reply. A truncated JSON body does not raise — it fails to match, and the caller reads "no findings" where
the truth was "no answer". Success and failure look identical, which is why it was always found by accident.

TWO LAYERS, because the defect came back at a different layer each time:

  STRUCTURE — no default cap may exist anywhere on the call path, and no cap literal may sit in library
  code without a recorded verdict. Walks the real source with ast, so it cannot be satisfied by a comment.

  BEHAVIOUR — with fake providers and no network: a missing budget is refused loudly, a sig draws the
  MEASURED budget, a truncated reply is retried at double, and a reply that is STILL truncated comes back
  with text=None so that no caller can parse it into a confident empty answer.
"""
import ast
import json
import os
import pathlib
import sys
import tempfile

# ISOLATE the home BEFORE importing spendguard: the BEHAVIOUR checks call the named model expecting ITS budget/clamp,
# but _call_guarded's proactive lane-balance reads confirmed-substitutes + lane state from ~/.spendguard and would
# SWAP the model (measured: claude-haiku-4-5 → codex/gpt-5.6-sol when shared bandit state favours codex), breaking
# every model-specific budget assertion. A fresh home has no confirmed substitutes, so the named model runs. (The
# spendguard test-isolation gotcha — a suite that reads real state is non-deterministic.)
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-notrunc-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from spendguard import adapters, token_caps  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "spendguard"

_fails = []


def check(name, cond, detail=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}" + (f"\n        {detail}" if (detail and not cond) else ""))
    if not cond:
        _fails.append(name)


# ────────────────────────────────────────────────────────────────────────────
print("-- STRUCTURE: the call path carries no default cap --")
# Read the real signatures out of the real file. Reflection on the imported function would also work, but
# parsing the source proves the SOURCE is clean, which is what a reviewer and a future editor both see.
_tree = ast.parse((SRC / "adapters.py").read_text())
_defaults = {}
for n in ast.walk(_tree):
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = n.args
        pairs = list(zip(a.args[-len(a.defaults):], a.defaults)) if a.defaults else []
        pairs += list(zip(a.kwonlyargs, a.kw_defaults))
        for arg, d in pairs:
            if arg.arg in token_caps.CAP_KWARGS:
                _defaults[n.name] = d.value if isinstance(d, ast.Constant) else "<expr>"

for fn in ("call", "_call_guarded", "_call_once"):
    check(f"adapters.{fn}() has NO numeric default cap",
          _defaults.get(fn, None) is None,
          f"{fn}() defaults {token_caps.CAP_KWARGS[0]}={_defaults.get(fn)!r} — that is a cap nobody chose. "
          f"This is the exact regression: call() was fixed and this one kept its 512.")

# The general form, so a NEW helper on the path cannot reintroduce it.
_bad_defaults = [s for s in token_caps.sites(SRC) if s["kind"] == "signature-default"]
check("no function in src/spendguard has a numeric default cap", not _bad_defaults,
      "; ".join(f"{s['file']}:{s['symbol']} {s['kwarg']}={s['value']}" for s in _bad_defaults))


# ────────────────────────────────────────────────────────────────────────────
print("-- STRUCTURE: every literal cap in library code has been RULED ON --")
_audit = token_caps.unjudged_and_content(REPO)
check(f"all {_audit['total']} cap literal(s) in src/ have a recorded verdict", not _audit["unjudged"],
      "UNJUDGED (a cap with no verdict fails by design — run `spendguard token-caps --judge`): "
      + "; ".join(f"{s['file']}:{s['symbol']} {s['kwarg']}={s['value']}" for s in _audit["unjudged"]))
check("no cap sits on a call whose OUTPUT IS USED", not _audit["failed"],
      "; ".join(f"{c['file']}:{c['symbol']} {c['kwarg']}={c['value']} — {c.get('why','')}"
                for c in _audit["failed"]))


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: spendguard OWNS the budget — a caller need not (and cannot) choose it --")
# The doctrine changed (docs/CANONICAL_CONCERNS.json: output_budget): max_output is a CEILING billed on ACTUAL tokens, so
# spendguard sends the model's ceiling and the caller's request is IGNORED. A bare call therefore SUCCEEDS at the ceiling
# — it is never refused for "no budget", because there is always a safe one (the ceiling). The raw path still guards.
_ceil_haiku = adapters.output_budget("claude-haiku-4-5")


class _CaptureOnce:
    """Stub _call_once: record the budget it was handed, return a clean short reply."""
    def __init__(self):
        self.budgets = []

    def __call__(self, model, prompt, max_tokens=None, **kw):
        self.budgets.append(max_tokens)
        return {"provider": "fake", "model": model, "text": "ok", "in_tok": 10, "out_tok": 5,
                "latency": 0.01, "cost": 0.0, "finish_reason": "stop", "truncated": False, "error": None}


_real_once = adapters._call_once
try:
    cap = _CaptureOnce()
    adapters._call_once = cap
    r0 = adapters.call("claude-haiku-4-5", "hi")     # no sig, no max_tokens — must SUCCEED at the ceiling now
    check("a bare call (no sig, no max_tokens) succeeds — spendguard supplies the ceiling budget",
          not r0.get("error") and cap.budgets and cap.budgets[0] == _ceil_haiku,
          f"budgets tried: {cap.budgets} — expected the ceiling {_ceil_haiku}")
finally:
    adapters._call_once = _real_once

try:
    adapters._call_once("claude-haiku-4-5", "hi", max_tokens=None)
    check("_call_once refuses a None budget on the raw path", False, "it proceeded with no budget")
except ValueError:
    check("_call_once refuses a None budget on the raw path", True)


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: a truncation at the ceiling is SURFACED (text=None), never returned as a partial body --")
from spendguard import bulkgate, pricing  # noqa: E402


class FakeProvider:
    """Records the budget of every attempt and truncates for the first `truncate_n` of them."""

    def __init__(self, truncate_n, body='{"findings": [1, 2, 3]}'):
        self.truncate_n, self.body, self.budgets = truncate_n, body, []

    def __call__(self, model, prompt, max_tokens=None, **kw):
        self.budgets.append(max_tokens)
        cut = len(self.budgets) <= self.truncate_n
        return {"provider": "fake", "model": model,
                "text": (self.body[: len(self.body) // 2] if cut else self.body),
                "in_tok": 10, "out_tok": ((max_tokens or 0) if cut else 12),
                "latency": 0.01, "cost": 0.0,
                "finish_reason": ("length" if cut else "stop"), "truncated": cut, "error": None}


# spendguard sends the model's CEILING (billed on actual, so the max is free) — there is no upward doubling because the
# call already STARTS at the max. A truncation therefore means the output genuinely exceeded the model's real maximum
# (the jumbo case), and it MUST come back as text=None, never a partial JSON a caller parses into "no findings". The
# caller's max_tokens is IGNORED throughout (docs/CANONICAL_CONCERNS.json: output_budget).
try:
    fake = FakeProvider(truncate_n=1)
    adapters._call_once = fake
    r = adapters.call("claude-haiku-4-5", "x", max_tokens=100, sig=None)
    check("the caller's max_tokens=100 is IGNORED — the ceiling budget is sent",
          fake.budgets and fake.budgets[0] == _ceil_haiku, f"budgets tried: {fake.budgets} — expected ceiling {_ceil_haiku}")
    check("a truncated reply returns text=None (surfaced, never a partial body)", r.get("text") is None,
          f"text={r.get('text')!r} — a partial body here is the silent wrong answer this suite exists for")
    check("and it is flagged truncated", bool(r.get("truncated")), f"truncated={r.get('truncated')!r}")
    parsed_empty = False
    try:
        json.loads(r.get("text") or "")
    except (TypeError, ValueError):
        parsed_empty = True
    check("a truncated result cannot be parsed into an empty answer", parsed_empty,
          "json.loads() succeeded on a truncated body — that is how 'no answer' becomes 'no findings'")
finally:
    adapters._call_once = _real_once


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: the budget is the CEILING, regardless of caller value or per-class prediction --")
_real_max = bulkgate.maxtokens
try:
    # a per-class 'recommend' is an ESTIMATE input (expected_output.expect), NEVER the budget. A tiny recommend can
    # no longer starve a call — the budget is the ceiling.
    bulkgate.maxtokens = lambda sig: {"recommend": 7}
    fake3 = FakeProvider(truncate_n=0)
    adapters._call_once = fake3
    adapters.call("claude-haiku-4-5", "x", sig="probe:measured")
    check("a tiny per-class recommend does NOT lower the budget — the ceiling is sent",
          fake3.budgets and fake3.budgets[0] == _ceil_haiku, f"budgets tried: {fake3.budgets} — expected ceiling {_ceil_haiku}")

    # the budget IS the model's output ceiling, so it can never exceed what the endpoint accepts (no over-ceiling 400).
    check("the budget equals the model's output ceiling (never an over-ceiling number)",
          _ceil_haiku == pricing.output_ceiling(adapters.provider_for("claude-haiku-4-5"),
                                                 "claude-haiku-4-5", adapters.MAX_TOKEN_CEILING))

    # a model whose PUBLISHED max is genuinely below the floor gets its real max — authoritative-low is honoured (the
    # floor only applies where the ceiling is unknown/guessed, never over a published limit). Stubbed so it ALWAYS runs.
    _real_pub = pricing.max_output_tokens
    try:
        pricing.max_output_tokens = lambda m: 8192 if m == "tiny-pub-model" else _real_pub(m)
        check("a genuinely-small PUBLISHED model gets its real max (8192), not the 32K floor",
              adapters.output_budget("openai:tiny-pub-model") == 8192)
    finally:
        pricing.max_output_tokens = _real_pub

    # THE DOCTRINE REVERSAL: an explicit caller max_tokens is IGNORED, not honoured — it can only ever truncate, so
    # spendguard drops it and sends the ceiling. (An INTERNAL tiny probe is the sole exception; see _internal_tiny.)
    bulkgate.maxtokens = lambda sig: {"recommend": 0}
    fake5 = FakeProvider(truncate_n=0)
    adapters._call_once = fake5
    adapters.call("claude-haiku-4-5", "x", max_tokens=16, sig="probe:deliberate")
    check("an EXPLICIT caller max_tokens is IGNORED, not honoured — spendguard sends the ceiling",
          fake5.budgets and fake5.budgets[0] == _ceil_haiku, f"budgets tried: {fake5.budgets} — expected ceiling {_ceil_haiku}")
finally:
    bulkgate.maxtokens = _real_max
    adapters._call_once = _real_once


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: an empty visible answer is SURFACED (text=None), not returned as '' --")


class BurnsBudgetOnReasoning:
    """Spends the whole budget on hidden reasoning and returns a clean, EMPTY response — the field shape: a reasoning
    model whose thinking tokens bill against max_tokens and whose visible text is "" (parses as nothing, reads as
    "no findings"). At the ceiling budget there is nothing larger to grow into, so an empty here must be SURFACED."""

    def __init__(self):
        self.budgets = []

    def __call__(self, model, prompt, max_tokens=None, **kw):
        self.budgets.append(max_tokens)
        return {"provider": "fake", "model": model, "text": "", "in_tok": 10,
                "out_tok": max_tokens or 0, "latency": 0.01, "cost": 0.0,
                "finish_reason": "stop", "truncated": False, "error": None}


_ceil_g55 = adapters.output_budget("gpt-5.5")
try:
    burn = BurnsBudgetOnReasoning()
    adapters._call_once = burn
    rb = adapters.call("gpt-5.5", "x", sig="probe:reason")
    check("an empty answer that consumed tokens is surfaced as text=None, not ''", rb.get("text") is None,
          f"text={rb.get('text')!r} — an empty string here is the silent 'no findings' this suite exists for")
    check("the ceiling budget was sent (huge headroom — an empty at the ceiling is the model's own behaviour)",
          burn.budgets and burn.budgets[0] == _ceil_g55, f"budgets tried: {burn.budgets} — expected ceiling {_ceil_g55}")
finally:
    adapters._call_once = _real_once

print(("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails)))
sys.exit(1 if _fails else 0)
