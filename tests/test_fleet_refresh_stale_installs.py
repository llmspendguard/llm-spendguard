"""Offline guard for fleet editable-install metadata refreshes: no network, real pip, git, or LLM."""
import os
import json
import pathlib
import sqlite3
import tempfile
from types import SimpleNamespace

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fleet-refresh-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import config, fleet  # noqa: E402

fails = []
PINNED_CONSUMER_VERSION = "0.14.0"


def ck(label, condition):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}")
    if not condition:
        fails.append(label)


def isolated_home():
    home = pathlib.Path(tempfile.mkdtemp(prefix="sg-fleet-refresh-"))
    config.HOME = home
    return home


def consumer(home, name, pid):
    venv = home / name
    venv.mkdir()
    with fleet._fleet_db() as connection:
        connection.execute("INSERT INTO consumers(pid,venv_root,python_executable) VALUES(?,?,?)",
                           (pid, str(venv), str(venv / "bin" / "python")))
    return venv


original_install = fleet._refresh_editable_install
original_install_state = fleet._consumer_install_state
try:
    fleet._consumer_install_state = lambda python, root, version, timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S: {
        "state": "editable_current", "code_version": version, "metadata_version": version,
        "module_path": str(root / "src" / "spendguard" / "__init__.py"), "editable": True}
    print("-- absent marker refreshes and records the current dependency fingerprint --")
    home = isolated_home()
    venv = consumer(home, "consumer", 1001)
    calls = []
    fleet._refresh_editable_install = lambda python, root, timeout=180: calls.append((python, root))
    result = fleet.refresh_stale_installs(apply=True)
    ck("missing marker is classified stale and refreshed", result["refreshed"] == [str(venv)])
    ck("pip boundary is invoked exactly once", len(calls) == 1)
    ck("successful refresh writes the current marker",
       (venv / fleet._INSTALL_FINGERPRINT_MARKER).read_text().strip() == fleet._dependency_fingerprint())

    print("\n-- matching marker is the deps-unchanged no-op --")
    home = isolated_home()
    current = consumer(home, "current", 1002)
    (current / fleet._INSTALL_FINGERPRINT_MARKER).write_text(fleet._dependency_fingerprint() + "\n")
    calls = []
    fleet._refresh_editable_install = lambda *args, **kwargs: calls.append(args)
    result = fleet.refresh_stale_installs(apply=True)
    ck("current venv is skipped", result["skipped_current"] == [str(current)])
    ck("current venv never invokes pip", calls == [] and result["refreshed"] == [])

    print("\n-- pinned non-editable install cannot hide behind a current dependency marker --")
    home = isolated_home()
    pinned = consumer(home, "pinned", 1006)
    (pinned / fleet._INSTALL_FINGERPRINT_MARKER).write_text(fleet._dependency_fingerprint() + "\n")
    fleet._consumer_install_state = original_install_state
    original_subprocess_run = fleet.subprocess.run
    fleet.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({
            "code_version": PINNED_CONSUMER_VERSION, "metadata_version": PINNED_CONSUMER_VERSION,
            "module_path": str(pinned / "site-packages" / "spendguard" / "__init__.py"), "editable": False}))
    calls = []
    fleet._refresh_editable_install = lambda python, root, timeout=180: calls.append((python, root))
    try:
        result = fleet.refresh_stale_installs(apply=True)
    finally:
        fleet.subprocess.run = original_subprocess_run
    ck("pinned consumer is detected and refreshed despite matching marker",
       result["refreshed"] == [str(pinned)] and len(calls) == 1 and result["skipped_current"] == [])
    fleet._consumer_install_state = lambda python, root, version, timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S: {
        "state": "editable_current", "code_version": version, "metadata_version": version,
        "module_path": str(root / "src" / "spendguard" / "__init__.py"), "editable": True}

    print("\n-- a per-venv failure does not block another refresh --")
    home = isolated_home()
    bad = consumer(home, "a-bad", 1003)
    good = consumer(home, "b-good", 1004)
    calls = []

    def fake_install(python, repo_root, timeout=180):
        calls.append(python)
        if bad in pathlib.Path(python).parents:
            raise RuntimeError("simulated pip failure")

    fleet._refresh_editable_install = fake_install
    result = fleet.refresh_stale_installs(apply=True)
    ck("both stale venvs reach the pip boundary", len(calls) == 2)
    ck("first failure is captured", result["failed"] == [{"venv": str(bad), "error": "simulated pip failure"}])
    ck("second venv still refreshes", result["refreshed"] == [str(good)]
       and (good / fleet._INSTALL_FINGERPRINT_MARKER).exists())

    print("\n-- dry-run classifies without invoking pip --")
    home = isolated_home()
    dry = consumer(home, "dry-run", 1005)
    calls = []
    fleet._refresh_editable_install = lambda *args, **kwargs: calls.append(args)
    result = fleet.refresh_stale_installs(apply=False)
    ck("dry-run reports would-refresh venv", result["refreshed"] == [str(dry)])
    ck("dry-run invokes pip zero times and writes no marker",
       calls == [] and not (dry / fleet._INSTALL_FINGERPRINT_MARKER).exists())

    print("\n-- fingerprint is stable by set and changes with dependencies --")
    pyproject = home / "fingerprint.toml"
    pyproject.write_text('[project]\nname="x"\ndependencies=["alpha >= 1", "beta"]\n'
                         '[project.optional-dependencies]\nextra=["gamma", "delta"]\n')
    first = fleet._dependency_fingerprint(pyproject)
    pyproject.write_text('[project]\nname = "x"\ndependencies = [ "beta", "alpha>=1" ]\n'
                         '[project.optional-dependencies]\nextra = [ "delta", "gamma" ]\n')
    ck("reordering and whitespace preserve fingerprint", fleet._dependency_fingerprint(pyproject) == first)
    pyproject.write_text('[project]\nname="x"\ndependencies=["alpha>=1", "beta", "new-dep"]\n'
                         '[project.optional-dependencies]\nextra=["gamma", "delta"]\n')
    ck("dependency addition changes fingerprint", fleet._dependency_fingerprint(pyproject) != first)

    print("\n-- old registry schema migrates additively --")
    home = isolated_home()
    with sqlite3.connect(home / "fleet.db") as connection:
        connection.execute("CREATE TABLE consumers(pid INTEGER PRIMARY KEY, version TEXT)")
    with fleet._fleet_db() as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(consumers)")}
    ck("migration adds venv columns", {"venv_root", "python_executable"} <= columns)
finally:
    fleet._refresh_editable_install = original_install
    fleet._consumer_install_state = original_install_state

print(f"\n{'[FAIL]' if fails else 'OK'} test_fleet_refresh_stale_installs: {len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
