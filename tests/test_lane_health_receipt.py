"""Guard — a down lane surfaces in EVERY receipt ($0 cached read), and the notifier/receipt read the same state.

Ash's ask: 'let me know in any conversation'. reliability persists the last check's reachability + fix into
lane_health; health_alert() reads it for the receipt (which prints every turn); the macOS notifier reads the same.
Pins: a persisted RED row → health_reds/health_alert surface it with the fix; ALL GREEN → None (silent when
healthy); the receipt wrapper is fail-safe; stale rows drop out of the freshness window.
Offline: cached ledger reads only — no sweep, no network, no spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lh-")
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

from spendguard import reliability, receipt

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")

DOWN = {"lanes": {"claude-code": {"reachable": False, "reason": "OAuth session expired"},
                  "codex": {"reachable": True, "reason": None}},
        "metered": {"gemini": {"reachable": False, "reason": "credits depleted"},
                    "openai": {"reachable": True}}}
ACTS = [{"kind": "lane", "resource": "claude-code", "fix": "log in the Claude CLI", "command": "claude auth login"},
        {"kind": "metered", "resource": "gemini", "fix": "top up AI Studio credits", "command": ""}]

print("-- a persisted RED check surfaces the down resources + the fix --")
reliability._persist_health(DOWN, ACTS)
reds = reliability.health_reds()
ck("both down resources recorded (green ones excluded)",
   {r["resource"] for r in reds} == {"claude-code", "gemini"})
ck("the fix is carried", any(r["resource"] == "claude-code" and r["command"] == "claude auth login" for r in reds))

print("-- health_alert(): a ONE-LINE receipt alert naming a resource + a fix --")
al = reliability.health_alert()
ck("alert names the count + a down resource", al and "2 resource(s) unreachable" in al and "claude-code" in al)
ck("alert carries a concrete fix", al and ("claude auth login" in al or "log in the Claude CLI" in al))

print("-- the RECEIPT wrapper returns the same alert (rides every turn's receipt), fail-safe --")
ck("receipt._lane_health_alert() == the alert", receipt._lane_health_alert() == al)

print("-- ALL GREEN → None (the receipt stays silent when healthy) --")
reliability._persist_health({"lanes": {"claude-code": {"reachable": True}, "codex": {"reachable": True}},
                            "metered": {"gemini": {"reachable": True}, "openai": {"reachable": True}}}, None)
ck("no alert when everything is reachable", reliability.health_alert() is None)
ck("receipt wrapper also None when healthy", receipt._lane_health_alert() is None)

print("-- freshness: a STALE check (older than the window) does not raise a phantom alert --")
reliability._persist_health(DOWN, ACTS)
ck("recent red is seen", any(r["resource"] == "claude-code" for r in reliability.health_reds(since_hours=48)))
import datetime as _dt
from spendguard import budget as _budget
_old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=100)).isoformat(timespec="seconds")
_db = reliability._health_db()
with _budget._lock:
    _db.execute("INSERT OR REPLACE INTO lane_health VALUES (?,?,?,?,?,?,?)",
                ("stalelane", "lane", 0, "an old failure", None, None, _old))
    _db.commit()
ck("a row older than the window is EXCLUDED (no phantom alert from a days-old check)",
   not any(r["resource"] == "stalelane" for r in reliability.health_reds(since_hours=48)))

print(f"\n{'[FAIL]' if _fails else 'OK'} test_lane_health_receipt: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
