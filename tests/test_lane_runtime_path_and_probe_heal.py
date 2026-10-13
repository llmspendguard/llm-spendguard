"""Resolved lane CLIs expose co-located runtimes, and newer successful probes heal stale sweep reds. Offline/$0."""
import datetime
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lane-runtime-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import config, lanes, reliability  # noqa: E402

fails = []


def ck(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        fails.append(label)


# The helper derives the runtime directory from the resolved binary; no installer/version-manager path is assumed.
old_path = os.environ.get("PATH")
old_key = os.environ.get("OPENAI_API_KEY")
try:
    os.environ["PATH"] = os.pathsep.join(("/daemon/bin", "/usr/bin"))
    os.environ["OPENAI_API_KEY"] = "must-not-reach-plan-child"
    resolved = os.path.join(tempfile.gettempdir(), "node-v-test", "bin", "codex")
    child = config.lane_exec_env(resolved)
    ck("resolved CLI directory is prepended to PATH", child["PATH"].split(os.pathsep)[0] == os.path.dirname(resolved))
    ck("ambient PATH follows the co-located runtime directory", child["PATH"].split(os.pathsep)[1:] == ["/daemon/bin", "/usr/bin"])
    ck("lane credential scrubbing is preserved", "OPENAI_API_KEY" not in child)
finally:
    if old_path is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = old_path
    if old_key is None:
        os.environ.pop("OPENAI_API_KEY", None)
    else:
        os.environ["OPENAI_API_KEY"] = old_key


def seed(ts):
    db = reliability._health_db()
    db.execute("DELETE FROM lane_health")
    db.execute("INSERT INTO lane_health (resource,kind,reachable,reason,fix,command,ts,source,notified_ts) "
               "VALUES (?,?,?,?,?,?,?,?,?)", ("codex", "lane", 0, "probe failed", "fix", "", ts, "sweep", None))
    db.commit()


def exists():
    return reliability._health_db().execute(
        "SELECT 1 FROM lane_health WHERE resource='codex' AND source='sweep'").fetchone() is not None


base = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
older = base.isoformat()
newer = (base + datetime.timedelta(seconds=1)).isoformat()
orig_probe = lanes._last_probe_ok
try:
    seed(older)
    lanes._last_probe_ok = lambda lane: (True, newer)
    ck("newer successful live probe removes sweep red", reliability.health_reds() == [])
    ck("stale sweep row is deleted", not exists())

    seed(newer)
    lanes._last_probe_ok = lambda lane: (True, older)
    ck("newer sweep remains red", {r["resource"] for r in reliability.health_reds()} == {"codex"})
    ck("newer sweep row is retained", exists())

    seed(older)
    lanes._last_probe_ok = lambda lane: (False, "")
    ck("missing successful probe keeps sweep red", {r["resource"] for r in reliability.health_reds()} == {"codex"})
    ck("unconfirmed sweep row is retained", exists())
finally:
    lanes._last_probe_ok = orig_probe

print(f"\n{'OK' if not fails else 'FAIL'} test_lane_runtime_path_and_probe_heal: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
