"""Guard — bulk_delegate pairs results by MEANING (task_key/return_keyed), and estimate_fan previews a fan for $0.

The mis-pairing bug: bulk_delegate returns a task-ORDERED list, so a caller that deduped/filtered/reordered before
fanning and then zips by position crosses rows onto the wrong item. `return_keyed=True` + `task_key` returns
{key: row} — pairing is explicit and can never drift. Pins:
  · every return path flows through _finalize → a keyed dict when asked (error-row paths included, tested via the
    no-viable-lane early return so NOTHING spends);
  · a DUPLICATE caller key RAISES (a silent overwrite would drop a result — the very failure this closes);
  · a task_key LIST of the wrong length RAISES at the door (before any fan work);
  · default (return_keyed=False) is the byte-for-byte task-ordered LIST — backward compatible;
  · estimate_fan: $0, counts DISTINCT calls (content-key dedup), resolves the SAME arms bulk_delegate would, and
    prices the worst-case metered CEILING from pricing (never a literal), OUTPUT from the measured p99.
Offline: arms/pricing are monkeypatched — no lanes, no network, no spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-fan-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import lane_balance, lane_catalog, lane_economics, bulkgate, pricing

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


print("-- return_keyed: pair error rows by MEANING (no-viable-lane path → $0, no spend) --")
lane_balance._bulk_arms = lambda intent, lanes=None: []      # force the no_viable_lane early return (task-aligned rows)

tasks = ["alpha", "beta", "gamma"]
keyed = lane_balance.bulk_delegate(tasks, "t-intent", task_key=lambda t: f"card-{t}", return_keyed=True)
ck("return_keyed → a dict", isinstance(keyed, dict))
ck("one row per task, keyed by the caller key", set(keyed) == {"card-alpha", "card-beta", "card-gamma"})
ck("each row carries the refusal reason (not crossed)", all(r["reason"] == "no_viable_lane" for r in keyed.values()))

keyed_list = lane_balance.bulk_delegate(tasks, "t-intent", task_key=["k0", "k1", "k2"], return_keyed=True)
ck("a task_key LIST keys the same way", set(keyed_list) == {"k0", "k1", "k2"})

print("-- default return is the task-ordered LIST (backward compatible) --")
lst = lane_balance.bulk_delegate(tasks, "t-intent")
ck("no return_keyed → a list, one row per task in order", isinstance(lst, list) and len(lst) == 3)

print("-- fail LOUD: a duplicate caller key raises (a collision would drop a result) --")
try:
    lane_balance.bulk_delegate(["a", "b"], "t-intent", task_key=lambda t: "same", return_keyed=True)
    ck("duplicate key raised", False)
except ValueError as e:
    ck("duplicate key raised ValueError", "duplicate task_key" in str(e))

print("-- fail LOUD: a task_key list of the wrong length raises at the door --")
try:
    lane_balance.bulk_delegate(["a", "b", "c"], "t-intent", task_key=["only", "two"], return_keyed=True)
    ck("wrong-length key list raised", False)
except ValueError as e:
    ck("wrong-length key list raised ValueError", "one key per task" in str(e))

print("-- empty tasks: keyed → {}, list → [] --")
ck("empty + return_keyed → {}", lane_balance.bulk_delegate([], "t-intent", return_keyed=True) == {})
ck("empty → []", lane_balance.bulk_delegate([], "t-intent") == [])

print("-- estimate_fan: $0 preview, DISTINCT-call dedup, worst-case metered CEILING from pricing --")
# Two arms; provider == lane name; nothing reserved; a measured p99; a fixed per-call realtime price.
lane_balance._bulk_arms = lambda intent, lanes=None: [("codex", "gpt-x"), ("gemini", "gem-y")]
lane_catalog.lane_provider = lambda ln: ln
lane_economics.prompt_lane_reserved = lambda ln: False
bulkgate.maxtokens = lambda intent, **k: {"p99": 200, "n": 50}   # **k: real maxtokens takes (sig, current_max, model)
pricing.realtime_cost = lambda model, i, o, provider=None: 0.001    # $0.001 per distinct task

est = lane_balance.estimate_fan(["a", "a", "b"], "t-intent", system="sys")   # 3 tasks, 2 DISTINCT
ck("viable", est["viable"] is True)
ck("counts tasks vs DISTINCT calls (identical collapse)", est["n_tasks"] == 3 and est["n_distinct"] == 2)
ck("arms are the (provider:model) set the fan would use", est["arms"] == ["codex:gpt-x", "gemini:gem-y"])
ck("worst-case ceiling = n_distinct × per-call price", abs(est["est_metered_usd_worst"] - 0.002) < 1e-9)
ck("output basis is the MEASURED p99 (not a literal)", est["out_tok"] == 200 and "measured p99" in est["out_basis"])
ck("makes no spend claim beyond the ceiling (note names $0 on lanes)", "$0 on the" in est["note"])

print("-- estimate_fan: an unpriced model is counted + EXCLUDED from the ceiling, never $0-hidden --")
def _raise_unpriced(model, i, o, provider=None):
    raise KeyError(model)
pricing.realtime_cost = _raise_unpriced
est2 = lane_balance.estimate_fan(["x", "y"], "t-intent")
ck("unpriced models counted", est2["n_unpriced"] == 2)
ck("ceiling excludes unpriced (stays 0, with a note)", est2["est_metered_usd_worst"] == 0.0 and "unpriced" in est2["note"])

print("-- estimate_fan: no viable lane → viable=False, $0, a reason (never an error) --")
lane_balance._bulk_arms = lambda intent, lanes=None: []
est3 = lane_balance.estimate_fan(["a"], "t-intent")
ck("no lane → viable False", est3["viable"] is False and est3["reason"] == "no_viable_lane")
ck("no lane → ceiling 0", est3["est_metered_usd_worst"] == 0.0)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_bulk_delegate_keyed_and_estimate: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
