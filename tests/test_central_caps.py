"""Central caps — client side: the gate pulls the org/team's server-set caps (saas.pull_policy → config.json
`policy`) and class_cap() applies them. The contract: an ENFORCED cap is a hard ceiling (effective = min(local,
enforced), applies even with no local cap, local may only tighten); an ADVISORY cap is a suggestion only and never
changes the effective cap (partner, not supervisor). Offline, no network (pull mocked), zero spend."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-caps-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402

from spendguard import config, saas

fails = []
def write_cfg(d):
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.CONFIG_JSON.write_text(json.dumps(d))
    config._cfg._cache = None     # invalidate the memoized config so the next read sees it

P_ENF = lambda usd: {"usd": usd, "mode": "enforced"}
P_ADV = lambda usd: {"usd": usd, "mode": "advisory"}
def _boom(*a, **k):
    raise RuntimeError("network down")

# ── local only (no policy) ──
write_cfg({"caps": {"llm": {"monthly": 100}}})
record_script_check(fails, "local cap honored when no policy", config.class_cap("llm", "monthly") == 100)
record_script_check(fails, "unset class/window → None", config.class_cap("compute", "daily") is None)

# ── enforced applies even with NO local cap ──
write_cfg({"policy": {"caps": {"llm": {"monthly": P_ENF(500)}}}})
record_script_check(fails, "enforced cap applies with no local cap set", config.class_cap("llm", "monthly") == 500)

# ── enforced is a CEILING: a higher local is capped down; a tighter local wins ──
write_cfg({"caps": {"llm": {"monthly": 2000}}, "policy": {"caps": {"llm": {"monthly": P_ENF(500)}}}})
record_script_check(fails, "enforced ceiling caps a higher local (can't loosen)", config.class_cap("llm", "monthly") == 500)
write_cfg({"caps": {"llm": {"monthly": 300}}, "policy": {"caps": {"llm": {"monthly": P_ENF(500)}}}})
record_script_check(fails, "tighter local stays under the enforced ceiling", config.class_cap("llm", "monthly") == 300)

# ── advisory NEVER changes the effective cap ──
write_cfg({"caps": {"llm": {"monthly": 2000}}, "policy": {"caps": {"llm": {"monthly": P_ADV(500)}}}})
record_script_check(fails, "advisory does NOT override a higher local", config.class_cap("llm", "monthly") == 2000)
write_cfg({"policy": {"caps": {"llm": {"monthly": P_ADV(500)}}}})
record_script_check(fails, "advisory with no local → still None (suggestion only)", config.class_cap("llm", "monthly") is None)

# ── env cap is LOCAL: enforced still ceilings it; tighter env wins ──
write_cfg({"policy": {"caps": {"total": {"daily": P_ENF(50)}}}})
os.environ["GATE_TOTAL_DAILY"] = "200"
record_script_check(fails, "env local capped by enforced ceiling", config.class_cap("total", "daily") == 50)
os.environ["GATE_TOTAL_DAILY"] = "20"
record_script_check(fails, "tighter env local wins under enforced", config.class_cap("total", "daily") == 20)
del os.environ["GATE_TOTAL_DAILY"]

# ── policy_caps() accessor (for doctor/receipt) ──
write_cfg({"policy": {"caps": {"llm": {"monthly": P_ADV(9)}}, "asof": "2026-06-28"}})
record_script_check(fails, "policy_caps() returns the pulled block", config.policy_caps().get("asof") == "2026-06-28")

# ── saas.pull_policy persists the server payload → class_cap enforces it ──
write_cfg({})
saas._request = lambda method, path, *a, **k: {"caps": {"compute": {"monthly": P_ENF(800)}}, "asof": "2026-06-28"}
res = saas.pull_policy()
record_script_check(fails, "pull_policy returns the server payload", (((res or {}).get("caps") or {}).get("compute") or {}).get("monthly", {}).get("usd") == 800)
record_script_check(fails, "pull_policy persisted → class_cap enforces it", config.class_cap("compute", "monthly") == 800)
saas._request = _boom
record_script_check(fails, "pull_policy is fail-open on a network error (returns a dict, never raises)", isinstance(saas.pull_policy(), dict))

print(("[OK]" if not fails else "[FAIL]") + " central-caps (client): %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
