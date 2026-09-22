"""GUARD — advise.ranked prices a $0-subscription-lane arm at its TRUE amortized cost, so best-value / the bandit
STOP ranking a plan lane as free. The floor is out_tok × the lane's eff (route_economics.lane_eff_by_provider, from
lane_economics.economics()); a metered arm (no lane for its provider) is unchanged; an empty map (no converged lane)
leaves the prior $0 behaviour (fail-open). Hermetic: lane_economics.economics / advise.evidence stubbed; no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanecost-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import advise, route_economics, lane_economics   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


EFF = 15.29e-6

# ── (a) lane_eff_by_provider maps a converged lane's provider → its eff ──
print("-- (a) lane_eff_by_provider: converged lane's provider -> eff --")
_orig_econ = lane_economics.economics
lane_economics.economics = lambda *a, **k: [{"lane": "claude-code", "provider": "anthropic", "converged": True,
                                             "binding": {"eff_usd_per_tok": EFF, "remaining_abs": 1_000_000}}]
try:
    m = route_economics.lane_eff_by_provider()
    ck("maps the converged lane's provider to its amortized eff", abs(m.get("anthropic", 0) - EFF) < 1e-12)
finally:
    lane_economics.economics = _orig_econ

_orig_ev = advise.evidence
_orig_lm = route_economics.lane_eff_by_provider
try:
    # ── (b) a $0-lane arm is floored to its TRUE cost (out_tok × eff), never a flat $0 ──
    print("\n-- (b) advise.ranked floors a $0-lane arm at out_tok x eff (no longer $0) --")
    advise.evidence = lambda *a, **k: {("anthropic", "claude-opus-4-8"): dict(
        provider="anthropic", model="claude-opus-4-8", jobs=2, cost=0.0, outtok=100_000, good=2, labeled=2.0)}
    route_economics.lane_eff_by_provider = lambda now=None: {"anthropic": EFF}
    arm = advise.ranked(intent="x")["models"][0]
    ck("a $0-lane arm is priced at its TRUE cost (out_tok x eff = $1.529), never a flat $0",
       abs(arm["cost"] - 100_000 * EFF) < 1e-6 and arm["cost"] > 0)
    ck("per_good uses the true lane cost (not $0)", arm["per_good"] is not None and arm["per_good"] > 0)

    # ── (c) a METERED arm (no lane for its provider) is UNCHANGED ──
    print("\n-- (c) a metered arm (no lane for its provider) keeps its recorded cost --")
    advise.evidence = lambda *a, **k: {("openai", "gpt-5-nano"): dict(
        provider="openai", model="gpt-5-nano", jobs=2, cost=0.05, outtok=100_000, good=2, labeled=2.0)}
    route_economics.lane_eff_by_provider = lambda now=None: {"anthropic": EFF}   # no openai lane
    arm2 = advise.ranked(intent="x")["models"][0]
    ck("a metered arm (no lane) keeps its recorded cost, unchanged", abs(arm2["cost"] - 0.05) < 1e-12)

    # ── (d) empty map (no converged lane) → prior behaviour, the recorded $0 stands (fail-open) ──
    print("\n-- (d) no converged lane -> prior $0-lane behaviour (fail-open) --")
    advise.evidence = lambda *a, **k: {("anthropic", "claude-opus-4-8"): dict(
        provider="anthropic", model="claude-opus-4-8", jobs=2, cost=0.0, outtok=100_000, good=2, labeled=2.0)}
    route_economics.lane_eff_by_provider = lambda now=None: {}
    arm3 = advise.ranked(intent="x")["models"][0]
    ck("no converged lane -> the arm's recorded $0 is unchanged (never crashes)", arm3["cost"] == 0.0)
finally:
    advise.evidence = _orig_ev
    route_economics.lane_eff_by_provider = _orig_lm

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_cost_in_ranking: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
