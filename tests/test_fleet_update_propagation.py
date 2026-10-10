"""The fleet registry makes spendguard update-propagation observable + enforceable. A long-lived process loads
spendguard once at startup and runs that code until it restarts; a later pip/deploy does not touch it (measured
2026-10-10: pre-fix 7thsense daemons false-fired the codex toast for days). This locks in:
  • register() records THIS process (pid, version, served_sha) — once per process, never raises;
  • running() returns live consumers, each flagged STALE when its served_sha != the green pointer (exact, not a
    start-time guess), and prunes dead pids;
  • restart_stale() kickstarts launchd-MANAGED stale consumers and WARNs about unsupervised ones (never kills what it
    can't bring back);
  • release.exit_if_stale() self-exits when a respawn would land on green.
Offline, isolated HOME, no network, no LLM."""
import os
import sqlite3
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fleet-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import fleet, release, config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# control the served commit of THIS process + the green pointer
release.served_sha = lambda: {"sha": "AAAAAAA", "short": "AAAAAAA", "dirty": False, "describe": "AAAAAAA"}

def _reregister():
    fleet._registered = False
    fleet.register_consumer()

# ── 1. register() records this process with its served_sha ──
release.green_pointer = lambda: {"sha": "AAAAAAA", "short": "AAAAAAA"}   # green == served → not stale
_reregister()
procs = fleet.running_consumers()
me = [p for p in procs if p["pid"] == os.getpid()]
ck("register() recorded this process", len(me) == 1 and me[0]["served_sha"] == "AAAAAAA")
ck("on-green consumer is NOT stale", me and me[0]["stale"] is False)

# ── 2. a newer green pointer makes this process STALE (exact served_sha compare) ──
release.green_pointer = lambda: {"sha": "BBBBBBB", "short": "BBBBBBB"}
me2 = [p for p in fleet.running_consumers() if p["pid"] == os.getpid()][0]
ck("consumer behind green is flagged STALE", me2["stale"] is True)

# ── 3. no green pointer → stale is None (cannot tell), never a false STALE ──
release.green_pointer = lambda: None
me3 = [p for p in fleet.running_consumers() if p["pid"] == os.getpid()][0]
ck("no green pointer → stale is None (not a false positive)", me3["stale"] is None)

# ── 4. a dead pid is pruned on read ──
with sqlite3.connect(str(config.HOME / "fleet.db")) as c:
    c.execute("INSERT OR REPLACE INTO consumers(pid,version,served_sha,served_short,dirty,cmdline,launchd_label,registered_ts)"
              " VALUES(999999,'0.0.0','DEAD','DEAD',0,'ghost',NULL,'t')")
    c.commit()
_ = fleet.running_consumers()                         # read prunes dead pids
with sqlite3.connect(str(config.HOME / "fleet.db")) as c:
    ghost = c.execute("SELECT COUNT(*) FROM consumers WHERE pid=999999").fetchone()[0]
ck("dead pid pruned from the registry on read", ghost == 0)

# ── 5. restart_stale (dry run): a launchd-managed stale consumer → would kickstart; unsupervised → warn ──
release.green_pointer = lambda: {"sha": "BBBBBBB", "short": "BBBBBBB"}   # make this process stale
fleet._launchd_label_for = lambda pid: "com.example.fake"               # pretend it IS launchd-managed
res = fleet.restart_stale(apply=False)
ck("dry-run reports it as 'would kickstart' (no real restart)", res["dry_run"] is True
   and any("would kickstart" in r.get("action", "") for r in res["restarted"]))
fleet._launchd_label_for = lambda pid: None                             # pretend it is NOT supervised
res2 = fleet.restart_stale(apply=False)
ck("an unsupervised stale consumer is WARNed, never killed",
   any("NOT launchd-managed" in w.get("why", "") for w in res2["warn"]))

# ── 6. exit_if_stale: exits when should_respawn, returns False when not ──
release.should_respawn = lambda: False
ck("exit_if_stale returns False when not stale (no exit)", release.exit_if_stale() is False)
release.should_respawn = lambda: True
_exited = False
try:
    release.exit_if_stale()
except SystemExit:
    _exited = True
ck("exit_if_stale raises SystemExit when a respawn would land on green", _exited)

print(f"\n{'[FAIL]' if fails else 'OK'} test_fleet_update_propagation: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
