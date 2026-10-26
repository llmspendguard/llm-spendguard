"""GUARD — the deploy/release CLI wires the green pointer correctly. `deploy` REFUSES a dirty tree (the pointer
must name only COMMITTED code) and otherwise promotes through release.promote_release; `release` renders
served-vs-green + staleness. The load-bearing logic lives in release.py (unit-tested in
test_release_green_pointer); this pins the CLI GLUE so the deploy path cannot silently stop refusing a dirty tree
or stop promoting.

Hermetic: isolated home; release.{_tracked_dirty,promote_release,release_status} stubbed; --no-gate so the real
suite never runs; stdout/stderr captured. No network, no ledger, no git writes."""
import contextlib
import io
import os
import pathlib
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-deploycli-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import cli, release, config, fleet   # noqa: E402

config.HOME = pathlib.Path(os.environ["SPENDGUARD_HOME"])
fleet.verify_and_refresh_registered_interpreters = lambda apply=True: {
    "N_total": 0, "N_updated": 0, "N_verified_fresh": 0, "failed": [], "complete": True}

fails = []
DIRTY_TREE_MESSAGE = (
    "deploy: the working tree has uncommitted changes to tracked files — commit first (the green pointer must name "
    "committed code), or pass --allow-dirty deliberately."
)
PROMOTED_COMMIT_MESSAGE = (
    "deploy: 🟢 promoted abc1234 (v0.10.0-1-gabc1234) → green pointer  [skipped (--no-gate)]"
)
CONSUMER_GAP_MESSAGE = "deploy: promoted; armed 0; 1 consumers STILL not on latest/gated"


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._dispatch(argv)
    return rc, buf.getvalue()


class PromotionRecorder:
    def __init__(self):
        self.count = 0
        self.gate = None

    def promote_release(self, **keywords):
        self.count += 1
        self.gate = keywords.get("gate")
        return {"sha": "abc1234", "short": "abc1234", "describe": "v0.10.0-1-gabc1234",
                "gate": keywords.get("gate", "")}


promotion_recorder = PromotionRecorder()

release.promote_release = promotion_recorder.promote_release

print("-- deploy REFUSES a dirty tree and does not promote --")
release._tracked_dirty = lambda: True
rc, out = _run(["deploy", "--no-gate", "--skip-consumer-verify"])
ck("deploy on a DIRTY tree exits non-zero (refuses)", rc == 2)
ck("... and does NOT promote", promotion_recorder.count == 0)
ck("... telling the user to commit first", out.splitlines()[0] == DIRTY_TREE_MESSAGE)

print("\n-- deploy --no-gate on a CLEAN tree promotes exactly once --")
release._tracked_dirty = lambda: False
rc, out = _run(["deploy", "--no-gate", "--skip-consumer-verify"])
ck("deploy --no-gate on a clean tree exits 0", rc == 0)
ck("... promotes exactly once, through release.promote_release", promotion_recorder.count == 1)
ck("... and records that the gate was skipped (--no-gate), not faked as green", "no-gate" in (promotion_recorder.gate or ""))
ck("... reporting the promoted commit", PROMOTED_COMMIT_MESSAGE in out.splitlines())

print("\n-- deploy consumer verification fails closed after promotion --")
from spendguard import consumer_audit as _consumer_audit  # noqa: E402

_original_consumer_audit = _consumer_audit.audit_consumer_workspaces
_original_consumer_render = _consumer_audit.render_consumer_audit_table
_consumer_audit.audit_consumer_workspaces = lambda deployed_sha, auto_arm, dry_run: {
    "deployed_sha": deployed_sha, "armed": 0, "remaining_gaps": 1, "exit_code": 1,
    "dry_run": dry_run, "consumers": [],
}
_consumer_audit.render_consumer_audit_table = lambda _result: "fixture consumer audit"
rc, out = _run(["deploy", "--no-gate", "--no-restart", "--no-refresh"])
ck("deploy promotes but exits non-zero when consumer verification retains a gap",
   rc == 1 and PROMOTED_COMMIT_MESSAGE in out.splitlines() and CONSUMER_GAP_MESSAGE in out.splitlines())
_consumer_audit.audit_consumer_workspaces = lambda deployed_sha, auto_arm, dry_run: {
    "deployed_sha": deployed_sha, "armed": 1, "remaining_gaps": 0, "exit_code": 0,
    "dry_run": dry_run, "consumers": [],
}
rc, out = _run(["deploy", "--no-gate", "--no-restart", "--no-refresh"])
ck("deploy continues after a complete consumer proof", rc == 0 and "fixture consumer audit" in out)
_consumer_audit.audit_consumer_workspaces = _original_consumer_audit
_consumer_audit.render_consumer_audit_table = _original_consumer_render

print("\n-- deploy discovers, registers, then fails closed on a stale registry escape --")
from spendguard import setup as _setup  # noqa: E402

_escape_root = pathlib.Path(tempfile.mkdtemp(prefix="spendguard-deploy-escape-"))
_registered_python = _escape_root / "registered-python"
_escaped_python = _escape_root / "escaped-python"
_registered_python.write_text("")
_escaped_python.write_text("")
with fleet._fleet_db() as _fleet_connection:
    _fleet_connection.execute(
        "INSERT OR REPLACE INTO registered_interpreters"
        "(python_executable,venv_root,first_seen,last_seen) VALUES(?,?,?,?)",
        (str(_registered_python.resolve()), str(_escape_root), "first", "last"),
    )
    _fleet_connection.commit()
_original_host_discovery = fleet.discover_host_spendguard_installations
_original_host_install = _setup.install_hook
_original_deployment_verify = fleet.verify_and_refresh_registered_interpreters
fleet.discover_host_spendguard_installations = lambda: [
    {"source": "test-host", "python_executable": str(_escaped_python.resolve())}
]


def _register_discovered_escape(user, python):
    with fleet._fleet_db() as _fleet_connection:
        _fleet_connection.execute(
            "INSERT OR REPLACE INTO registered_interpreters"
            "(python_executable,venv_root,first_seen,last_seen) VALUES(?,?,?,?)",
            (str(pathlib.Path(python).resolve()), str(_escape_root), "first", "last"),
        )
        _fleet_connection.commit()
    return 0


def _fail_stale_discovered_escape(apply):
    registered = {row["python_executable"] for row in fleet.registered_interpreters()}
    escaped = str(_escaped_python.resolve())
    failed = ([{"python_executable": escaped, "state": "editable_stale"}]
              if escaped in registered else [])
    return {"N_total": len(registered), "N_updated": 0,
            "N_verified_fresh": len(registered) - len(failed), "updated": [],
            "verified_fresh": list(registered - {escaped}), "failed": failed, "complete": not failed}


_setup.install_hook = _register_discovered_escape
fleet.verify_and_refresh_registered_interpreters = _fail_stale_discovered_escape
rc, out = _run(["deploy", "--no-gate", "--skip-consumer-verify", "--no-restart"])
registered_after_deploy = {row["python_executable"] for row in fleet.registered_interpreters()}
ck("deploy registers a structurally discovered host escape before registered-interpreter verification",
   str(_escaped_python.resolve()) in registered_after_deploy
   and "deploy: registry — discovered=1 reconciled=1 remaining=0" in out)
ck("a newly registered stale escape fails deploy closed at the registered-interpreter bar",
   rc == 1 and f"registered deployment FAILED {_escaped_python.resolve()}" in out)
# Mutation bite: removing the reconcile/install wire-in leaves the escape absent from the registry; this verifier then
# sees only the pre-registered fresh interpreter, returns complete, and both assertions above fail (deploy exits 0).
fleet.discover_host_spendguard_installations = _original_host_discovery
_setup.install_hook = _original_host_install
fleet.verify_and_refresh_registered_interpreters = _original_deployment_verify

print("\n-- release renders served / green / staleness --")
release.release_status = lambda: {"served": {"short": "aaaaaaa", "describe": "v0.10.0-9-gaaaaaaa"},
                                  "green": {"short": "bbbbbbb", "describe": "v0.10.0-9-gbbbbbbb"},
                                  "stale": True, "served_dirty": False,
                                  "pointer_path": "/x/current_release.json", "note": "STALE: a… behind b…"}
rc, out = _run(["release"])
ck("release exits 0", rc == 0)
ck("release shows served, green, and a STALE status", "aaaaaaa" in out and "bbbbbbb" in out and "STALE" in out)

print("\n-- deploy --help prints usage WITHOUT acquiring the lock or running the gate --")
# Regression (2026-10-10): `deploy --help` fell through the manual arg handling, took the single-instance lock, and
# ran the full gate/promote; a hung --help then held the lock and blocked real deploys. Help must short-circuit first.
from spendguard import config as _config   # noqa: E402


def _lock_must_not_be_taken(_name):
    raise AssertionError("deploy --help acquired the single-instance deploy lock — it must not")


_config.single_instance_lock = _lock_must_not_be_taken
release._tracked_dirty = lambda: True       # even a DIRTY tree must still give help (help is before the dirty check)
_before = promotion_recorder.count
try:
    rc, out = _run(["deploy", "--help"])
    _lock_ok = True
except AssertionError:
    rc, out, _lock_ok = 1, "", False
ck("deploy --help never acquires the deploy lock", _lock_ok)
ck("deploy --help exits 0", rc == 0)
ck("deploy --help prints usage (flags shown)", "usage: spendguard deploy" in out and "--no-gate" in out and "--no-restart" in out)
ck("deploy --help does NOT promote / run the gate", promotion_recorder.count == _before)

print(f"\n{'[FAIL]' if fails else 'OK'} test_deploy_release_cli: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
