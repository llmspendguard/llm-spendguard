"""Two honestreview-identified fixes in vendor_call, guarded so they cannot regress:

(1) served_substitute must only CACHE a 'no good substitute' verdict when the advisor reply actually PARSED. A failed
    parse is NOT a negative verdict — caching it would permanently poison the id with a false 'no substitute' from a
    one-off bad reply (absence-coverage). A PARSED {"id": null} IS a real negative verdict and is cached.

(2) fan_out / first_ok must bound the CUMULATIVE spend across the vendor fan (not just per-call) — a hand-rolled pool
    over call() otherwise has no running ceiling. budget_usd=0 → every call is skipped (gate_refused), nothing spent.

Offline: adapters.call / catalog / models / time_budget / call are stubbed; zero real spend."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fanfix-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import vendor_call as vc, catalog, models, adapters, config, plan_admission  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


# ── (1) served_substitute parse-coverage ──────────────────────────────────────────────────────────────────────
vc.served_check = lambda vendor, m: "unserved"                 # force it past the early 'already served' return
catalog.live_model_ids = lambda v: ["gpt-cand-a", "gpt-cand-b"]
catalog.lane_model_ids = lambda v: []
models.facts = lambda m: {}                                    # no prior cached verdict → it runs the resolver call
plan_admission.ready_meta_model = lambda d: d
config.advisor_model = lambda: "anthropic:claude-x"
_added = []
models.add_fact = lambda requested, key, val, **kw: _added.append((requested, key, val))

adapters.call = lambda model, prompt, **kw: {"text": "sorry, I can't answer in JSON here", "error": None}
_added.clear()
res = vc.served_substitute("openai", "phantom-model")
ck("unparseable resolver reply → returns the original unchanged", res == ("phantom-model", None))
ck("unparseable reply caches NOTHING (no false 'no substitute')", _added == [])

adapters.call = lambda model, prompt, **kw: {"text": '{"id": null, "reason": "none of the candidates fit"}', "error": None}
_added.clear()
res = vc.served_substitute("openai", "phantom-model")
ck("a PARSED {\"id\": null} → returns original unchanged", res == ("phantom-model", None))
ck("a parsed negative verdict IS cached (real 'no substitute')", any(k == "served_substitute" and v == "" for _, k, v in _added))

adapters.call = lambda model, prompt, **kw: {"text": '{"id": "gpt-cand-a", "reason": "best match"}', "error": None}
_added.clear()
res = vc.served_substitute("openai", "phantom-model")
ck("a parsed valid id → resolves to that served candidate", res[0] == "gpt-cand-a")

# ── (2) fan cumulative budget ─────────────────────────────────────────────────────────────────────────────────
vc.time_budget = lambda v, m, **kw: (5, "stub")
_calls = []
def _fake_call(v, m, prompt, **kw):
    _calls.append((v, m))
    return vc.Result(vc.OK, v, m, text="ok", cost=1.0)
vc.call = _fake_call
VENDORS = [("openai", "m1"), ("anthropic", "m2"), ("zai", "m3"), ("gemini", "m4")]

_calls.clear()
out = vc.fan_out(VENDORS, "q", deadline_s=5, budget_usd=0.0)       # ceiling 0 → every call skipped
ck("fan_out budget_usd=0 → zero real calls made", _calls == [])
ck("fan_out budget_usd=0 → all results are gate_refused", all(r.kind == vc.GATE_REFUSED for r in out["results"]) and len(out["results"]) == 4)

_calls.clear()
out = vc.fan_out(VENDORS, "q", deadline_s=5, budget_usd=None)      # default ceiling (config) → all run
ck("fan_out default budget → all vendors run", len(_calls) == 4 and out["n_ok"] == 4)

_calls.clear()
out = vc.first_ok(VENDORS, "q", deadline_s=5, need=4, budget_usd=0.0)
ck("first_ok budget_usd=0 → zero real calls made", _calls == [])

print(f"\n{'[FAIL]' if fails else 'OK'} test_fan_budget_and_resolver_parse: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
