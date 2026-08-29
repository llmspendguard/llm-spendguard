"""Two defects reported from real use on 2026-07-16.

1. **The gate's cost warning wasn't a budget signal.** A batch that billed $0.60 warned at ~$16 (~27×). The rate
   was right (batch_cost); the OUTPUT assumption was not — the estimators sum each request's `max_tokens` as if
   every request runs to the limit (real fill is ~40-55%). A cap must bound what COULD be spent, so the ceiling
   stays the cap's basis — but the number a HUMAN reads now leads with the learned expectation (calibrate's
   fill/opi quantiles, already measured per model), with the ceiling named as a ceiling.
2. **Key-missing errors named `~/.spendguard/.env`** — the LEGACY path — while `init` scaffolds `keys.env`.
   Users were sent to create a file nothing creates ("I thought we moved to keys.env?").
Offline: calibrate is stubbed; no network, no model calls.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-estsig-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate, config

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


# A job whose ceiling is wildly above reality: 200 requests × 4096 max_tokens, real fill ~15%.
EST = {"provider": "anthropic", "model": "claude-sonnet-4-6", "requests": 200,
       "in_tok": 300_000, "out_tok": 819_200, "cost": 16.20}

print("-- the displayed number leads with the LEARNED expectation, ceiling named as a ceiling --")
import spendguard.calibrate as cal
# Stub the CANONICAL name. `estimate` is a back-compat alias for outside consumers; stubbing the alias
# would silently stop intercepting the moment internal callers moved to the real name — which is exactly
# what happened when calibrate.estimate was renamed to predict_cost.
cal.predict_cost = lambda label, n=1, model=None, transport="batch", est_in_tokens=None, est_out_max=None, as_of=None: {
    "p50_usd": 0.60, "p90_usd": 1.10, "level": "model", "n_obs": 1666}
e = dict(EST)
c = gate._calibrate_est(e)
check("calibration attached with p50/p90/basis/n_obs",
      c and abs(c["cost_p50"] - 0.60) < 1e-9 and abs(c["cost_p90"] - 1.10) < 1e-9
      and c["basis"] == "model" and c["n_obs"] == 1666)
e["_cal"] = c
line = gate._est_cost_phrase(e)
check("likely cost is first in the phrase", line.startswith("~$0.60 likely"))
check("p90 band shown", "$1.10 p90" in line)
check("the ceiling is present but LABELLED a ceiling", "ceiling $16.20" in line)
check("provenance is shown (how many observations, at what level)", "1,666 obs" in line and "@model" in line)

print("-- per-request inputs are what get passed to the learner (not whole-batch totals) --")
seen = {}
# Stub the CANONICAL name. `estimate` is a back-compat alias for outside consumers; stubbing the alias
# would silently stop intercepting the moment internal callers moved to the real name — which is exactly
# what happened when calibrate.estimate was renamed to predict_cost.
cal.predict_cost = lambda label, n=1, model=None, transport="batch", est_in_tokens=None, est_out_max=None, as_of=None: (
    seen.update(n=n, model=model, transport=transport, in_=est_in_tokens, out=est_out_max)
    or {"p50_usd": 0.6, "p90_usd": 1.1, "level": "model", "n_obs": 10})
gate._calibrate_est(dict(EST))
check("n = request count", seen["n"] == 200)
check("est_in_tokens is PER REQUEST (1,500), not the 300,000 batch total", seen["in_"] == 1500)
check("est_out_max is PER REQUEST (4,096), not the 819,200 total", seen["out"] == 4096)
check("transport is batch (batch rates, not realtime)", seen["transport"] == "batch")

print("-- degrade: no calibration → honest ceiling language, never a fabricated 'likely' --")
def _boom(*a, **k):
    raise RuntimeError("no history")
cal.predict_cost = _boom      # canonical name, same reason as above
e2 = dict(EST)
check("a failing learner returns None (never raises into the gate)", gate._calibrate_est(e2) is None)
e2["_cal"] = None
line2 = gate._est_cost_phrase(e2)
check("falls back to the ceiling, SAYING it's a ceiling", "$16.20" in line2 and "ceiling" in line2)
check("no invented 'likely' number when nothing is learned", "likely" not in line2)

print("-- the CAP still compares the ceiling (fail-safe: a cap bounds what COULD be spent) --")
import inspect
src = inspect.getsource(gate._decide)
check("cap comparison uses est['cost'] (the ceiling)", 'if est["cost"] <= cap' in src)
check("the docstring states the fail-safe intent", "fail-safe" in (gate._decide.__doc__ or ""))

print("-- key-missing errors name keys.env (the file init CREATES), never the legacy .env --")
import spendguard.reconcile_openai as ro, spendguard.reconcile_anthropic as ra
for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)
_real_key, _real_ra = config.api_key, ra._api_key
config.api_key = lambda name: ""            # reconcile_openai imports api_key INSIDE load_key → this patch lands
ra._api_key = lambda name: ""               # reconcile_anthropic aliased it at import → patch the alias
msgs = []
try:
    ro.load_key()
except Exception as e:
    msgs.append(str(e))
try:
    ra._key()
except Exception as e:
    msgs.append(str(e))
config.api_key, ra._api_key = _real_key, _real_ra
check(f"both errors mention keys.env ({len(msgs)} captured)",
      len(msgs) == 2 and all("keys.env" in m for m in msgs))
check("neither error sends the user to the legacy ~/.spendguard/.env",
      all("/.env" not in m.replace("keys.env", "") for m in msgs))

print("-- NOTHING (code or docs) tells a user to write the legacy .env — keys.env is the one answer --")
import pathlib
root = pathlib.Path(gate.__file__).parent.parent.parent          # repo root
targets = list((root / "src" / "spendguard").glob("*.py")) + [root / "README.md", root / "SETUP.md",
                                                              root / "scripts" / "README.md"]
targets += list((root / "docs").glob("*.md"))
bad = []
for p in targets:
    if not p.exists() or p.name.startswith("test_"):
        continue
    for i, ln in enumerate(p.read_text().splitlines(), 1):
        # the loader/back-compat mentions are FINE — they must say "legacy" (or name KEYS_ENV) to prove they're
        # describing history, not instructing the user.
        if "spendguard/.env" in ln and "legacy" not in ln.lower() and "KEYS_ENV" not in ln:
            bad.append(f"{p.name}:{i}")
check(f"no legacy-.env instructions in code OR docs: {bad}", not bad)

print(f"\n{'[FAIL]' if failures else 'OK'} test_estimate_signal: {failures} failure(s)")
sys.exit(1 if failures else 0)
