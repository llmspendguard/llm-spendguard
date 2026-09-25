"""Guardrail D is PATH-INDEPENDENT. The per-intent running cap + the hand-rolled-fan detector live at the CALL DOOR
(gate._rt_precheck_usd → gate._intent_door_gate), the ONE seam every metered call passes through — so they hold on
EVERY path a call is issued: a single adapters.call, a hand-rolled ThreadPoolExecutor fan, or bulk_delegate. That is
the hole that let warden's 2026-09-23/24 fan spend ~$96 on gpt-5.5 before anything stopped it: guardrail D lived only
INSIDE bulk_delegate, which a hand-rolled fan never enters. Offline: no network, no model call — drives the door helper
directly against a ledger the test populates."""
import datetime
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-gd-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("GATE_ALLOW", None)                 # the cap must NOT be globally bypassed
os.environ["GATE_INTENT_CAP_GD_CAP_TEST"] = "0.05"   # per-intent cap for intent 'gd-cap-test' (non-alnum → _, upper)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import gate, calls, bulkgate, dispatch  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


def _seed(intent, n, each):
    """Record n metered calls of $each for `intent`, NOW, straight into the ledger the door sums."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with calls._lock:
        for k in range(n):
            calls._calls_db().execute("INSERT INTO calls(id, ts, intent, cost) VALUES(?,?,?,?)",
                                      (f"{intent}-{k}", now, intent, each))
        calls._calls_db().commit()


# ── (1) the per-intent RUNNING CAP at the door ──
print("-- (1) per-intent running cap (path-independent) --")
_seed("gd-cap-test", 4, 0.01)                      # $0.04 already spent this window
ck("intent_spend reads the seeded ledger ($0.04)", abs(calls.intent_spend("gd-cap-test") - 0.04) < 1e-6)
_refused = False
try:
    gate._intent_door_gate("gd-cap-test", 0.02, "openai", "gpt-5.5")   # 0.04 + 0.02 = 0.06 > 0.05 cap
except gate.SpendGateRefused:
    _refused = True
ck("a call that would cross the cap is REFUSED (typed deliberate stop, not a silent partial)", _refused)
ck("the refusal is recorded for the admission snapshot", gate.intent_cap_refusals().get("gd-cap-test", 0) >= 1)
_under_ok = True
try:
    gate._intent_door_gate("gd-cap-test", 0.005, "openai", "gpt-5.5")  # 0.045 < 0.05 → allowed
except gate.SpendGateRefused:
    _under_ok = False
ck("a call UNDER the cap proceeds (cumulative stops AT ~the cap, not before)", _under_ok)

# ── (2) caps are OPT-IN — an intent with no configured cap is never refused ──
print("-- (2) opt-in --")
_nocap_ok = True
try:
    gate._intent_door_gate("gd-no-cap", 100.0, "openai", "gpt-5.5")
except gate.SpendGateRefused:
    _nocap_ok = False
ck("an intent with no configured cap is not refused (a cap only ADDS safety where set)", _nocap_ok)

# ── (3) HAND-ROLLED-FAN detection: a raw same-intent fan trips; a bulk_delegate fan does NOT ──
print("-- (3) hand-rolled-fan detection --")
_pm = bulkgate.preview_max()
for _ in range(_pm + 2):
    gate._intent_door_gate("gd-raw-fan", 0.0, "openai", "gpt-5.5")     # est 0 → no cap concern; pure velocity signal
ck(f"a raw fan of >{_pm} same-intent calls is flagged as un-gated", gate.ungated_fans().get("gd-raw-fan", 0) >= 1)
with gate.governed_bulk():
    for _ in range(_pm + 5):
        gate._intent_door_gate("gd-bulk-fan", 0.0, "openai", "gpt-5.5")
ck("the SAME-size fan under governed_bulk is NOT flagged (bulk_delegate is already governed)",
   gate.ungated_fans().get("gd-bulk-fan", 0) == 0)

# ── (4) admission_state surfaces both (parity: `spendguard dispatch` CLI + spendguard_dispatch_state MCP) ──
print("-- (4) observability parity --")
st = dispatch.admission_state()
ck("admission_state carries intent_cap_refusals reflecting the refusal",
   isinstance(st.get("intent_cap_refusals"), dict) and st["intent_cap_refusals"].get("gd-cap-test", 0) >= 1)
ck("admission_state carries ungated_fans reflecting the raw fan",
   isinstance(st.get("ungated_fans"), dict) and st["ungated_fans"].get("gd-raw-fan", 0) >= 1)

# ── (5) PATH-INDEPENDENCE: the ONE door every metered call passes invokes the gate ──
print("-- (5) the control is at the door, not only in bulk_delegate --")
import inspect  # noqa: E402
ck("gate._rt_precheck_usd (the metered-call door) invokes _intent_door_gate",
   "_intent_door_gate(" in inspect.getsource(gate._rt_precheck_usd))
ck("bulk_delegate marks its fan governed_bulk (so its own calls aren't mis-flagged as un-gated)",
   "governed_bulk()" in inspect.getsource(__import__("spendguard.lane_balance", fromlist=["bulk_delegate"]).bulk_delegate))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_guardrail_d_path_independence: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
