"""register_critical — a consumer (e.g. warden) pins its no-substitution VENDOR-CRITICAL intents into
advisor.bandit_denylist, drift-proof AND attributed, so `doctor` reads a not-yet-run REGISTERED pin as
"registered by <source>" instead of a possible typo. The REAL guarantee stays the per-call no_substitution;
this is the belt-and-suspenders layer made drift-proof. Offline + hermetic: config under a temp SPENDGUARD_HOME."""
import os
import sqlite3
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-regcrit-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, config   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# an EMPTY calls table → read_ok=True, every pin unmatched (nothing recorded) — the state that makes coverage's
# registered-vs-suspect split meaningful (a fresh consumer pin has run nowhere yet).
_con = sqlite3.connect(config.db_path())
_con.execute("CREATE TABLE IF NOT EXISTS calls (intent TEXT)")
_con.commit()
_con.close()

print("-- register_critical merges pins into advisor.bandit_denylist + records the source --")
merged = lane_balance.register_critical(["warden:govern_adjudicate*", "warden:card_faithful*"], source="warden")
config.cfg_invalidate()
dl = config._cfg_get("advisor", "bandit_denylist", None) or []
ck("both pins land in advisor.bandit_denylist", "warden:govern_adjudicate*" in dl and "warden:card_faithful*" in dl)
ck("returns the merged list", set(merged) >= {"warden:govern_adjudicate*", "warden:card_faithful*"})
src = config._cfg_get("advisor", "bandit_denylist_sources", None) or {}
ck("the source is recorded per pattern", src.get("warden:govern_adjudicate*") == "warden")

print("-- idempotent: re-registering the same pattern does not duplicate --")
lane_balance.register_critical(["warden:card_faithful*"], source="warden")
config.cfg_invalidate()
dl2 = config._cfg_get("advisor", "bandit_denylist", None) or []
ck("no duplicate entry after re-register", dl2.count("warden:card_faithful*") == 1)
ck("list stays sorted + deduped", dl2 == sorted(set(dl2)))

print("-- a DIFFERENT source adds more without dropping the first --")
lane_balance.register_critical(["review:"], source="honestreview")
config.cfg_invalidate()
dl3 = config._cfg_get("advisor", "bandit_denylist", None) or []
src3 = config._cfg_get("advisor", "bandit_denylist_sources", None) or {}
ck("the new pin is added, warden's preserved", "review:" in dl3 and "warden:card_faithful*" in dl3)
ck("each pin keeps its own source", src3.get("review:") == "honestreview" and src3.get("warden:card_faithful*") == "warden")

print("-- coverage exposes sources so doctor attributes a not-yet-run pin (registered, not a suspect typo) --")
cov = (lane_balance.bandit_list_coverage() or {}).get("bandit_denylist", {})
ck("coverage carries the sources map", cov.get("sources", {}).get("warden:govern_adjudicate*") == "warden")
_un = cov.get("unmatched") or []
_registered = [e for e in _un if e in (cov.get("sources") or {})]
_suspect = [e for e in _un if e not in (cov.get("sources") or {})]
ck("a registered pin reads as registered-unmatched (benign)", "warden:govern_adjudicate*" in _registered)
ck("no unregistered suspects here (every pin was registered with a source)", _suspect == [])

print("-- empty call is a no-op returning the current list unchanged --")
before = config._cfg_get("advisor", "bandit_denylist", None) or []
ck("register_critical([]) is a no-op", lane_balance.register_critical([]) == before)

print(f"\n{'[FAIL]' if fails else 'OK'} test_register_critical: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
