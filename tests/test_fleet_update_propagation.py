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
import contextlib
import os
import sqlite3
import sys
import tempfile
import pathlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fleet-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import fleet, gate, release, config, setup  # noqa: E402

config.HOME = pathlib.Path(os.environ["SPENDGUARD_HOME"])

fails = []
from functools import partial
from _script_checks import record_script_check
ck = partial(record_script_check, fails)

# ── 0. enforcing-interpreter registration is persistent, idempotent, and fail-observable ──
original_fleet_iso = fleet._fleet_iso
registration_clock = {"calls": 0}


def registration_test_timestamp():
    registration_clock["calls"] += 1
    return "first" if registration_clock["calls"] <= 2 else "last"


fleet._fleet_iso = registration_test_timestamp
fleet.register_enforcing_interpreter()
fleet.register_enforcing_interpreter()
with fleet._fleet_db() as registration_connection:
    registration_rows = registration_connection.execute(
        "SELECT first_seen,last_seen FROM registered_interpreters WHERE python_executable=?",
        (str(pathlib.Path(sys.executable).resolve()),)).fetchall()
ck("enforcing interpreter has exactly one persistent registration", registration_rows == [("first", "last")])
fleet._fleet_iso = original_fleet_iso

original_fleet_db = fleet._fleet_db
fleet._fleet_db = lambda: (_ for _ in ()).throw(PermissionError("read-only fleet registry"))
ck("read-only registry never crashes enforcement", fleet.register_enforcing_interpreter() is False)
registration_status = fleet.enforcing_interpreter_registration_status()
ck("read-only registry is loudly tracked as enforcing-but-unregistered",
   registration_status["registered"] is False and "read-only" in registration_status["error"])
fleet._fleet_db = original_fleet_db
fleet.register_enforcing_interpreter()

original_register_enforcing_interpreter = fleet.register_enforcing_interpreter


def emit_and_fail_install_registration():
    print("registration dependency warning", file=sys.stderr)
    raise RuntimeError("registration persistence failed")


fleet.register_enforcing_interpreter = emit_and_fail_install_registration
install_registration_stderr = tempfile.TemporaryFile(mode="w+")
try:
    with contextlib.redirect_stderr(install_registration_stderr):
        gate._register_installing_enforcing_interpreter()
    install_registration_stderr.seek(0)
    escaped_install_registration_stderr = install_registration_stderr.read()
finally:
    install_registration_stderr.close()
    fleet.register_enforcing_interpreter = original_register_enforcing_interpreter
registration_status = fleet.enforcing_interpreter_registration_status()
ck("failed install registration adds no stderr noise",
   escaped_install_registration_stderr == "")
ck("failed install registration routes stderr and exception detail to doctor",
   registration_status["registered"] is False
   and "registration dependency warning" in registration_status["error"]
   and "registration persistence failed" in registration_status["error"])
fleet.register_enforcing_interpreter()

# ── 0b. host discovery reconciles CLI shebang identity against the persistent registry ──
fake_cli_root = pathlib.Path(tempfile.mkdtemp(prefix="sg-discovery-"))
fake_python = fake_cli_root / "python"
fake_cli = fake_cli_root / "spendguard"
fake_python.write_text("")
fake_cli.write_text(f"#!{fake_python}\n")
discovered = fleet.discover_host_spendguard_installations(cli_paths=[fake_cli], pipx_document={})
escape_audit = fleet.reconcile_registered_host_installations(discovered)
ck("discovered unregistered CLI interpreter is a registration escape with remediation",
   not escape_audit["complete"] and escape_audit["escapes"]
   and "install-hook --user --python" in escape_audit["escapes"][0]["remediation"])
with fleet._fleet_db() as registration_connection:
    registration_connection.execute(
        "INSERT INTO registered_interpreters"
        "(python_executable,venv_root,first_seen,last_seen) VALUES(?,?,?,?)",
        (str(fake_python.resolve()), str(fake_cli_root), "first", "last"))
registered_audit = fleet.reconcile_registered_host_installations(discovered)
ck("fully registered discovered host reports complete", registered_audit["complete"] is True)

other_python = fake_cli_root / "other-python"
with fleet._fleet_db() as registration_connection:
    registration_connection.execute(
        "INSERT INTO registered_interpreters"
        "(python_executable,venv_root,first_seen,last_seen) VALUES(?,?,?,?)",
        (str(other_python.resolve()), str(fake_cli_root), "first", "last"))
deregistered = fleet.deregister_interpreter(fake_python)
remaining_executables = {row["python_executable"] for row in fleet.registered_interpreters()}
ck("deregister removes exactly the resolved interpreter and preserves every other registration",
   deregistered == {"removed": True, "python_executable": str(fake_python.resolve()),
                    "remaining": len(remaining_executables)}
   and str(fake_python.resolve()) not in remaining_executables
   and str(other_python.resolve()) in remaining_executables)
second_deregistration = fleet.deregister_interpreter(fake_python)
ck("deregister is an idempotent no-op for an absent interpreter",
   second_deregistration["removed"] is False
   and second_deregistration["remaining"] == len(remaining_executables))
escaped_after_deregistration = fleet.reconcile_registered_host_installations(discovered)
ck("a still-discoverable gated interpreter reappears as an escape after explicit deregistration",
   not escaped_after_deregistration["complete"]
   and escaped_after_deregistration["escapes"][0]["python_executable"] == str(fake_python.resolve()))

original_reconcile = fleet.reconcile_registered_host_installations
original_install_hook = setup.install_hook
fleet.reconcile_registered_host_installations = lambda: escape_audit
applied_interpreters = []
setup.install_hook = lambda **kwargs: applied_interpreters.append(kwargs["python"])
try:
    gate_host_rc = setup.cmd_gate_discovered_host(["--apply"])
finally:
    fleet.reconcile_registered_host_installations = original_reconcile
    setup.install_hook = original_install_hook
ck("gate-host apply consumes every discovered registration escape",
   gate_host_rc == 0 and applied_interpreters == [str(fake_python.resolve())])

# control the served commit of THIS process + the green pointer
release.served_sha = lambda: {"sha": "AAAAAAA", "short": "AAAAAAA", "dirty": False, "describe": "AAAAAAA"}

def _reregister(**declarations):
    fleet._registered_pid = None
    fleet.register_consumer(**declarations)

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
declared_fixture_label = f"{__name__}.launchd"
os.environ[fleet.LAUNCHD_SERVICE_NAME_ENV] = declared_fixture_label
_reregister(role=fleet.CONSUMER_ROLE_DAEMON, supervision_mode=fleet.SUPERVISION_LAUNCHD,
            supervision_ref=declared_fixture_label)
os.environ.pop(fleet.LAUNCHD_SERVICE_NAME_ENV)
original_launchd_consumer_label_loaded = fleet.launchd_consumer_label_loaded


def report_fixture_launchd_label_as_loaded(_label):
    return True


fleet.launchd_consumer_label_loaded = report_fixture_launchd_label_as_loaded
res = fleet.restart_stale(apply=False)
fleet.launchd_consumer_label_loaded = original_launchd_consumer_label_loaded
ck("dry-run reports it as 'would kickstart' (no real restart)", res["dry_run"] is True
   and any("would kickstart" in r.get("action", "") for r in res["restarted"]))
_reregister(role=fleet.CONSUMER_ROLE_DAEMON)
res2 = fleet.restart_stale(apply=False)
ck("an unsupervised stale consumer is WARNed, never killed",
   any("not supervised" in w.get("why", "") for w in res2["warn"]))

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
