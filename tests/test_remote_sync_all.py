"""remote.sync_all — the at-source SWEEP: pull every live vast.ai box's realtime_log into the LOCAL ledger under
its attributed project, so ephemeral boxes report back completely WITHOUT reconstruction. Guards: _ssh_for derives
an SSH prefix from the vast fields (and returns None for a box still provisioning), the sweep syncs each box under
its project, a box with no SSH endpoint is SKIPPED (not an error), a FAILED pull is SURFACED (never counted as
synced/zero — the teardown-safety contract), and --dry runs nothing. Offline: instances/attributions/sync injected,
no network, no SSH.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-syncall-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import remote                                                          # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
INSTS = [
    {"id": 111, "label": "m2a-gpu", "ssh_host": "1.2.3.4", "ssh_port": 40001},
    {"id": 222, "label": "gliner", "ssh_host": "5.6.7.8", "ssh_port": 40002},
    {"id": 333, "label": "provisioning"},                 # no ssh endpoint yet → SKIP
]
ATTRIB = {"m2a-gpu": {"project": "manga2anime"}, "gliner": {"project": "lmm"}}

print("-- _ssh_for --")
s0 = remote._ssh_for(INSTS[0]) or ""
fails += ck("derives ssh from ssh_host/ssh_port", "-p 40001" in s0 and "root@1.2.3.4" in s0)
fails += ck("a box with no endpoint → None (skip, not error)", remote._ssh_for(INSTS[2]) is None)

synced = []


def _fake_sync(ssh, project, label=None, home=None):
    synced.append((ssh, project, label))
    if "5.6.7.8" in ssh:                                   # this box is unreachable → a FAILED pull
        return {"error": "pull failed (rc=255) — spend UNKNOWN", "rows": 0, "usd": 0.0,
                "project": project, "label": label}
    return {"rows": 7, "usd": 0.42, "project": project, "label": label}


print("\n-- sweep --")
res = remote.sync_all(_instances=INSTS, _attrib=ATTRIB, _sync=_fake_sync)
fails += ck("one result per live box (3)", len(res) == 3)
fails += ck("synced box carries rows + its attributed project (label fallback)",
            any(r.get("rows") == 7 and r.get("project") == "manga2anime" for r in res))
fails += ck("a FAILED pull is surfaced (error present, not counted as synced)",
            any(r.get("error") for r in res))
fails += ck("the no-ssh box is SKIPPED, and only the 2 reachable boxes were synced",
            any(r.get("skipped") for r in res) and len(synced) == 2)

print("\n-- dry --")
synced.clear()
resd = remote.sync_all(dry=True, _instances=INSTS, _attrib=ATTRIB, _sync=_fake_sync)
fails += ck("--dry runs NO sync, marks would-sync", len(synced) == 0 and any(r.get("would_sync") for r in resd))

print(f"\n{'[FAIL]' if fails else 'OK'} test_remote_sync_all: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
