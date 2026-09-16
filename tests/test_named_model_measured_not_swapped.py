"""A NAMED model is the one MEASURED — never a silent lane/bandit swap; and the judge scores the WHOLE output.

The bug (surfaced by a warden batch-1 smoke test, 2026-09): bakeoff runs each candidate via adapters.call WITHOUT
no_substitution, so in bandit-optout mode the candidate can be swapped for another lane/model — and the arm still
records that substitute's cost×quality under the candidate's name, corrupting the very numbers advise/recommend
rank on. Same 'WHICH MODEL ANSWERED is the measurement' class as the 2026-08-29 consensus-panel collapse.

This locks four things so it can't regress:
  1. bakeoff PINS each candidate (no_substitution=True) — it measures the exact model named.
  2. bakeoff PINS the judge too — every candidate is rated by the SAME ruler, not a swapped one.
  4. the judge scores the candidate's WHOLE output (no [:4000] evidence truncation).
  2*. adapters._warn_once_if_substituted makes a silent swap of an EXPLICITLY-NAMED model LOUD (once per pair),
      so the next measurement caller can't silently measure the wrong model.

Offline + hermetic: adapters.call is stubbed (no real model call, no network, no spend); _plan is stubbed so the
run proceeds without the pricing/tokenizer path.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-measure-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, bakeoff   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


print("-- adapters._warn_once_if_substituted: a NAMED model silently swapped is announced LOUD, once per pair --")
adapters._SUBST_WARN_LEDGER.clear()
swap = {"substituted_from": "openai:gpt-5-nano", "model": "anthropic:claude-haiku-4-5"}
m1 = adapters._warn_once_if_substituted(dict(swap))
ck("a swap of an explicitly-named model is announced (non-empty message)", isinstance(m1, str) and bool(m1))
ck("no swap → silent", adapters._warn_once_if_substituted({"model": "x"}) is None)
ck("best-value delegation is exempt (intentional + prints its own notice)",
   adapters._warn_once_if_substituted({"substituted_from": "a", "model": "b", "best_value": True}) is None)
ck("a self-'swap' (served == requested) is silent",
   adapters._warn_once_if_substituted({"substituted_from": "a", "model": "a"}) is None)
ck("ONCE per pair: the same swap does not re-announce", adapters._warn_once_if_substituted(dict(swap)) is None)

print("-- bakeoff PINS the candidate AND the judge (no_substitution=True) so it measures the model it names --")
seen = []


def _stub_call(model, prompt, **kw):
    seen.append({"model": model, "prompt": prompt, "kw": kw})
    if kw.get("schema") is not None:                     # the judge call (structured verdict)
        return {"text": '{"good": true}', "json": {"good": True}, "model": model, "cost": 0.0}
    return {"text": "candidate output", "model": model, "cost": 0.0}   # the candidate generation


adapters.call = _stub_call
bakeoff._plan = lambda *a, **k: (0.01, {"openai:gpt-5-nano": 0.01}, 1, 1)   # skip the pricing/tokenizer estimate path
res = bakeoff.bakeoff("test:intent", candidates=["openai:gpt-5-nano"], prompts=["do the task"],
                      run=True, budget_usd=999.0, judge_model="openai:gpt-5-nano")
cand_calls = [c for c in seen if c["kw"].get("schema") is None]
judge_calls = [c for c in seen if c["kw"].get("schema") is not None]
ck("bakeoff ran the candidate and judged it", len(cand_calls) >= 1 and len(judge_calls) >= 1)
ck("every candidate call is PINNED (no_substitution=True) — the named model is what's measured",
   bool(cand_calls) and all(c["kw"].get("no_substitution") is True for c in cand_calls))
ck("the judge call is PINNED too — one ruler across candidates",
   bool(judge_calls) and all(c["kw"].get("no_substitution") is True for c in judge_calls))

print("-- fix 4: the judge scores the candidate's WHOLE output, not a [:4000] head --")
captured = {}


def _capture_judge(model, prompt, **kw):
    captured["prompt"] = prompt
    return {"text": '{"good": true}', "json": {"good": True}, "model": model, "cost": 0.0}


adapters.call = _capture_judge
_long = "Z" * 5000                                        # a real candidate output longer than the old 4000 cut
bakeoff._judge_one("the task", _long, "openai:gpt-5-nano")
ck("the judge prompt carries the ENTIRE output (all 5000 chars, not a 4000 truncation)",
   captured.get("prompt", "").count("Z") >= 5000)

print("-- the requirement-aware path PINS its judge + adjudicator too (a ruler must not be swapped) --")
from spendguard import requirement_judge   # noqa: E402
rj_seen = []


def _stub_meta(model, prompt, **kw):
    rj_seen.append(kw)
    return {"text": '{"good": true}', "json": {"good": True}, "model": model, "cost": 0.0}


adapters.call = _stub_meta
requirement_judge._meta_call("openai:gpt-5-nano", "judge this", system="s",
                             schema={"type": "object"}, out=200, sig="test-judge")
ck("requirement_judge's meta ruler call is PINNED (no_substitution=True)",
   bool(rj_seen) and all(k.get("no_substitution") is True for k in rj_seen))

print(f"\n{'[FAIL]' if fails else 'OK'} test_named_model_measured_not_swapped: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
