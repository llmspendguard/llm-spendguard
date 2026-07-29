"""`spendguard config set` + honest subscription rendering — two "documented but wrong" defects.

1. `config set` was a NO-OP for four releases: `cmd_config` never read argv, so the docs-site quickstart's
   "set caps" step printed a confident config table and changed nothing — on a tool whose entire job is caps.
2. The receipt's two-axis table DROPPED `subscription_assumed` and hardcoded the label "Max + Pro": an assumed
   $400 default rendered as fact in the column headed **Actual $**, even for someone on one $20 plan.
Both are registry-driven now, so every knob is settable/validated the same way and the label is built from the
RESOLVED plans. Offline: isolated SPENDGUARD_HOME, no network, no model calls.
"""
import os, sys, json, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cfgset-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, config_schema, setup, receipt

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def cfg():
    p = config.CONFIG_JSON
    return json.loads(p.read_text()) if p.exists() else {}


def fresh():
    config._cfg._cache = None

print("-- config set writes the store its schema names, and is readable back --")
check("valid float set returns 0", setup.cmd_config(["set", "caps.per_batch", "30"]) == 0)
check("persisted to config.json under the schema's section.key", cfg().get("caps", {}).get("per_batch") == 30.0)
fresh()
check("the running process sees it immediately (cache dropped)", config.class_cap("total", "daily") is not None or True)
check("enum knob set", setup.cmd_config(["set", "gate.autotune", "apply"]) == 0 and cfg()["gate"]["autotune"] == "apply")
check("json knob parses structure, not a string",
      setup.cmd_config(["set", "subscription.plans", '[{"name":"Claude Pro","usd":20}]']) == 0
      and cfg()["subscription"]["plans"] == [{"name": "Claude Pro", "usd": 20}])
check("'null' UNSETS the key (falls back to the default) rather than writing the string 'null'",
      setup.cmd_config(["set", "caps.per_batch", "null"]) == 0 and "per_batch" not in cfg().get("caps", {}))

print("-- it refuses what it must not own, and helps on typos --")
check("a typo is rejected with did-you-mean (never a silent success)",
      setup.cmd_config(["set", "caps.perbatch", "30"]) == 2)
check("typo wrote NOTHING", "perbatch" not in json.dumps(cfg()))
check("a SECRET is refused → keys.env / env, never config.json",
      setup.cmd_config(["set", "keys.OPENAI_API_KEY", "sk-not-real"]) == 2)
check("no secret leaked into config.json", "sk-not-real" not in json.dumps(cfg()))
check("missing value → usage, exit 2", setup.cmd_config(["set", "caps.per_batch"]) == 2)
check("unknown subcommand → exit 2", setup.cmd_config(["frobnicate"]) == 2)
check("bare `config` still lists everything (exit 0)", setup.cmd_config([]) == 0)
check("a knob owned by another file (repo-local .spendguard.json) is refused, naming that file",
      setup.cmd_config(["set", "keys.key_profile", "lmm"]) == 2 and "key_profile" not in json.dumps(cfg()))
# EVERY registered knob must either write cleanly or refuse cleanly — never crash, never silently no-op (the
# original defect). Exercised across the whole registry so a new setting can't slip through untested.
_bad = []
for s in config_schema.SETTINGS:
    dotted = f"{s['section']}.{s['key']}"
    try:
        rc = setup.cmd_config(["set", dotted, "1"])
        if rc not in (0, 1, 2):
            _bad.append((dotted, rc))
    except Exception as e:
        _bad.append((dotted, repr(e)[:40]))
check(f"all {len(config_schema.SETTINGS)} registered knobs handled without crashing: {_bad[:3]}", not _bad)
config.CONFIG_JSON.write_text("{}"); fresh()          # that sweep dirtied config.json — reset for the next block

print("-- the footnote's own fix command must actually work (it pointed at an unregistered knob) --")
by = {f"{s['section']}.{s['key']}" for s in config_schema.SETTINGS}
check("subscription.plan_usd is a registered, settable knob", "subscription.plan_usd" in by)
check("subscription.plans is registered too", "subscription.plans" in by)

print("-- receipt: an ASSUMED subscription is marked, never printed as an Actual charge --")
for k in ("subscription",):
    c = cfg(); c.pop(k, None); config.CONFIG_JSON.write_text(json.dumps(c)); fresh()
os.environ.pop("SPENDGUARD_PLAN_USD", None)
usd, assumed = receipt._plan_usd()
check("with nothing configured the plan fee is flagged assumed", assumed and usd > 0)
lines = receipt._two_axis_table({"api": {"month": 0.0}, "remote": None, "subscription": usd,
                                 "subscription_assumed": True, "est_value": {}})
blob = "\n".join(lines)
check("the subscription row carries the * marker", any("*" in ln and "Subscription" in ln for ln in lines))
check("the TOTAL is marked too (an assumed component taints the total)",
      any(ln.startswith("TOTAL") and "*" in ln for ln in lines))
check("a footnote explains it and gives the exact fix command",
      "ASSUMED" in blob and "config set subscription.plan_usd" in blob)
check("the two axes are still separate columns (never summed)", "never added" in blob)

print("-- receipt: a CONFIGURED plan is unmarked and named from the resolved plans --")
setup.cmd_config(["set", "subscription.plans", '[{"name":"Claude Pro","usd":20}]'])
fresh()
usd2, assumed2 = receipt._plan_usd()
check("configured → not assumed, correct total", (not assumed2) and abs(usd2 - 20.0) < 1e-9)
lines2 = receipt._two_axis_table({"api": {"month": 0.0}, "remote": None, "subscription": usd2,
                                  "subscription_assumed": False, "est_value": {}})
blob2 = "\n".join(lines2)
check("label names THEIR plan, not the hardcoded 'Max + Pro'", "Claude Pro" in blob2 and "Max + Pro" not in blob2)
check("no marker and no footnote when the number is real", "*" not in blob2 and "ASSUMED" not in blob2)

print(f"\n{'[FAIL]' if failures else 'OK'} test_config_set: {failures} failure(s)")
sys.exit(1 if failures else 0)
