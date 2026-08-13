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
import pathlib
import sys

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
print("-- BEHAVIOUR: a budget that was never chosen is refused, not invented --")
try:
    adapters.call("claude-haiku-4-5", "hi")          # no sig, no max_tokens
    check("call() with neither sig nor max_tokens raises", False, "it returned instead of refusing")
except ValueError as e:
    check("call() with neither sig nor max_tokens raises", "sig" in str(e))
except Exception as e:
    check("call() with neither sig nor max_tokens raises", False, f"raised {type(e).__name__}: {e}")

try:
    adapters._call_once("claude-haiku-4-5", "hi", max_tokens=None)
    check("_call_once refuses a None budget on the raw path", False, "it proceeded with no budget")
except ValueError:
    check("_call_once refuses a None budget on the raw path", True)


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: truncation is retried, then surfaced — never returned as text --")


class FakeProvider:
    """Records the budget of every attempt and truncates for the first `truncate_n` of them."""

    def __init__(self, truncate_n, body='{"findings": [1, 2, 3]}'):
        self.truncate_n, self.body, self.budgets = truncate_n, body, []

    def __call__(self, model, prompt, max_tokens=None, **kw):
        self.budgets.append(max_tokens)
        cut = len(self.budgets) <= self.truncate_n
        return {"provider": "fake", "model": model,
                # a truncated JSON body: parses as nothing, reads downstream as "no findings"
                "text": (self.body[: len(self.body) // 2] if cut else self.body),
                # out_tok MATTERS: truncation is detected from finish_reason AND from out_tok reaching the
                # budget, so a "good" reply must come back well under it. An earlier version of this fake
                # returned out_tok == budget every time, which made even the successful retry look
                # truncated — the fixture, not the code, was wrong.
                "in_tok": 10, "out_tok": ((max_tokens or 0) if cut else 12),
                "latency": 0.01, "cost": 0.0,
                "finish_reason": ("length" if cut else "stop"), "truncated": cut, "error": None}


_real_once = adapters._call_once
try:
    # 1. truncated once -> retried at DOUBLE, and the good body comes back
    fake = FakeProvider(truncate_n=1)
    adapters._call_once = fake
    r = adapters.call("claude-haiku-4-5", "x", max_tokens=100, sig=None, retries=2)
    check("a truncated reply is retried", len(fake.budgets) >= 2,
          f"budgets tried: {fake.budgets} — only one attempt means truncation was accepted")
    check("the retry DOUBLES the budget", len(fake.budgets) >= 2 and fake.budgets[1] == fake.budgets[0] * 2,
          f"budgets tried: {fake.budgets}")
    check("after a successful retry the full body is returned", (r.get("text") or "").endswith("}"),
          f"text={r.get('text')!r}")

    # 2. truncated EVERY time -> text must be None, so nothing downstream can parse a confident empty answer
    fake2 = FakeProvider(truncate_n=99)
    adapters._call_once = fake2
    r2 = adapters.call("claude-haiku-4-5", "x", max_tokens=100, sig=None, retries=2)
    check("a reply truncated on every attempt returns text=None", r2.get("text") is None,
          f"text={r2.get('text')!r} — a partial body here is the silent wrong answer this suite exists for")
    check("and it is flagged truncated", bool(r2.get("truncated")), f"truncated={r2.get('truncated')!r}")

    # 3. THE ACTUAL HARM, stated as a test: the caller's parse must fail loudly rather than yield []
    parsed_empty = False
    try:
        json.loads(r2.get("text") or "")
    except (TypeError, ValueError):
        parsed_empty = True
    check("a truncated result cannot be parsed into an empty answer", parsed_empty,
          "json.loads() succeeded on a truncated body — that is how 'no answer' becomes 'no findings'")
finally:
    adapters._call_once = _real_once


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: the FLOOR governs; a prediction may only raise it --")
from spendguard import bulkgate  # noqa: E402

# THE RULE, stated once: an unspecified budget starts at TOKEN_FLOOR and a measurement can only add to it.
# The old rule was max(caller, predicted) with the floor used only when BOTH were zero, so a measured
# recommend of 400 produced a 400-token budget — the calls with the most history got the least room. It is
# also wrong in a way measurement cannot see: on reasoning models the hidden reasoning is billed against
# max_tokens, and a p99 of VISIBLE output never observed it.
_real_max = bulkgate.maxtokens
try:
    bulkgate.maxtokens = lambda sig: {"recommend": 7777}
    fake3 = FakeProvider(truncate_n=0)
    adapters._call_once = fake3
    adapters.call("claude-haiku-4-5", "x", sig="probe:measured")
    check("a prediction BELOW the floor does not lower the budget",
          fake3.budgets and fake3.budgets[0] == adapters.TOKEN_FLOOR,
          f"budgets tried: {fake3.budgets} — expected the {adapters.TOKEN_FLOOR} floor, not the 7777 prediction")

    bulkgate.maxtokens = lambda sig: {"recommend": adapters.TOKEN_FLOOR * 2}
    fake4 = FakeProvider(truncate_n=0)
    adapters._call_once = fake4
    adapters.call("claude-haiku-4-5", "x", sig="probe:measured")
    check("a prediction ABOVE the floor raises the budget",
          fake4.budgets and fake4.budgets[0] == adapters.TOKEN_FLOOR * 2, f"budgets tried: {fake4.budgets}")

    # THE CLAMP, stated as its own case. A floor above a model's own maximum is a 400, so the budget is
    # min(model_max, max(floor, predicted)). Proven against a model whose documented limit is BELOW what the
    # floor+prediction would otherwise ask for — otherwise the clamp can pass without ever being exercised,
    # which is what the assertion above does when the two numbers happen to coincide.
    from spendguard import pricing  # noqa: E402
    _clamped_model = next((m for m in pricing.MAX_OUT if pricing.MAX_OUT[m] < adapters.TOKEN_FLOOR * 4), None)
    if _clamped_model:
        _limit = pricing.MAX_OUT[_clamped_model]
        bulkgate.maxtokens = lambda sig: {"recommend": adapters.TOKEN_FLOOR * 4}   # asks for far more
        fake_c = FakeProvider(truncate_n=0)
        adapters._call_once = fake_c
        adapters.call(_clamped_model, "x", sig="probe:clamped")
        check(f"the budget is clamped to {_clamped_model}'s documented max ({_limit:,})",
              fake_c.budgets and fake_c.budgets[0] == _limit,
              f"budgets tried: {fake_c.budgets} — sending more than a model accepts is a 400, not a big answer")

    # An EXPLICIT number is the caller's deliberate choice — a 16-token connectivity probe is legitimate,
    # and token_caps holds a recorded verdict for every such literal in the tree. The floor must not
    # silently inflate it; only a measurement may raise it.
    bulkgate.maxtokens = lambda sig: {"recommend": 0}
    fake5 = FakeProvider(truncate_n=0)
    adapters._call_once = fake5
    adapters.call("claude-haiku-4-5", "x", max_tokens=16, sig="probe:deliberate")
    check("an EXPLICIT caller budget is honoured, not inflated to the floor",
          fake5.budgets and fake5.budgets[0] == 16, f"budgets tried: {fake5.budgets}")
finally:
    bulkgate.maxtokens = _real_max
    adapters._call_once = _real_once


# ────────────────────────────────────────────────────────────────────────────
print("-- BEHAVIOUR: an empty visible answer is not an answer (reasoning models) --")


class BurnsBudgetOnReasoning:
    """Spends the whole budget on hidden reasoning and returns a clean, EMPTY response.

    This is the shape reported in the field: gpt-5.5 is a reasoning model, the reasoning tokens are billed
    against max_tokens, and a 4000-token budget went entirely on thinking. The response is well-formed,
    carries no error, and its text is "" — which parses as nothing and reads as "no findings"."""

    def __init__(self):
        self.budgets = []

    def __call__(self, model, prompt, max_tokens=None, **kw):
        self.budgets.append(max_tokens)
        return {"provider": "fake", "model": model, "text": "", "in_tok": 10,
                "out_tok": max_tokens or 0, "latency": 0.01, "cost": 0.0,
                # note: NOT "length" — the model stopped cleanly having written only reasoning
                "finish_reason": "stop", "truncated": False, "error": None}


try:
    burn = BurnsBudgetOnReasoning()
    adapters._call_once = burn
    rb = adapters.call("gpt-5.5", "x", max_tokens=4000, sig=None, retries=2)
    check("an empty answer that consumed tokens is treated as truncated, not returned as ''",
          rb.get("text") is None,
          f"text={rb.get('text')!r} — an empty string here is the silent 'no findings' this suite exists for")
    check("and it is retried with MORE budget, which is what a reasoning model needs",
          len(burn.budgets) >= 2 and burn.budgets[1] > burn.budgets[0], f"budgets tried: {burn.budgets}")
finally:
    adapters._call_once = _real_once

print(("\nPASS — 0 failure(s)" if not _fails else f"\nFAIL — {len(_fails)} failure(s): " + "; ".join(_fails)))
sys.exit(1 if _fails else 0)
