"""Offline guard for fleet editable-install metadata refreshes: no network, real pip, git, or LLM."""
import contextlib
import io
import os
import json
import pathlib
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fleet-refresh-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

from spendguard import __version__ as CURRENT_SOURCE_VERSION, config, fleet, setup  # noqa: E402

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
original_registered_interpreters = fleet.registered_interpreters
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

    print("\n-- stale egg-info shadow is distinct from a genuinely pinned install --")
    fleet.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({
            "code_version": fleet.__version__ if hasattr(fleet, "__version__") else PINNED_CONSUMER_VERSION,
            "metadata_version": PINNED_CONSUMER_VERSION,
            "module_path": str(pathlib.Path(fleet.__file__).resolve()), "editable": False,
            "selected_metadata_path": str(pathlib.Path(fleet.__file__).resolve().parents[1] / "stale.egg-info"),
            "editable_metadata_paths": [str(pinned / "site-packages" / "current.dist-info")],
            "catalog_verified": None}))
    shadow_state = fleet._consumer_install_state(pathlib.Path(sys.executable),
                                                  pathlib.Path(fleet.__file__).resolve().parents[2],
                                                  PINNED_CONSUMER_VERSION)
    fleet.subprocess.run = original_subprocess_run
    ck("egg-info shadow has its own state instead of noneditable_pinned",
       shadow_state["state"] == "editable_shadowed_by_egg_info")

    print("\n-- an absent distribution remains probe-able and identifies managed source injection --")
    import importlib.metadata as probe_metadata
    original_probe_distribution = probe_metadata.distribution
    probe_metadata.distribution = lambda distribution_name: (_ for _ in ()).throw(
        probe_metadata.PackageNotFoundError(distribution_name))
    absent_distribution_output = io.StringIO()
    try:
        with contextlib.redirect_stdout(absent_distribution_output):
            exec(fleet._CONSUMER_INSTALL_PROBE, {})
    finally:
        probe_metadata.distribution = original_probe_distribution
    absent_distribution_details = json.loads(absent_distribution_output.getvalue())
    ck("probe reports an absent distribution without losing imported module identity",
       absent_distribution_details["distribution_present"] is False
       and absent_distribution_details["metadata_version"] is None
       and absent_distribution_details["selected_metadata_path"] is None
       and absent_distribution_details["editable_metadata_paths"] == []
       and absent_distribution_details["editable"] is False
       and absent_distribution_details["code_version"]
       and absent_distribution_details["module_path"]
       and absent_distribution_details["catalog_verified"])

    fleet.subprocess.run = lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({
            **absent_distribution_details,
            "module_path": str(pathlib.Path(fleet.__file__).resolve())}))
    managed_pathinjected_state = fleet._consumer_install_state(
        pathlib.Path(sys.executable), pathlib.Path(fleet.__file__).resolve().parents[2],
        absent_distribution_details["code_version"])
    fleet.subprocess.run = original_subprocess_run
    ck("an absent distribution imported from the managed source has its own state",
       managed_pathinjected_state["state"] == "pathinjected_managed")

    refresh_repo = pathlib.Path(tempfile.mkdtemp(prefix="sg-egg-shadow-"))
    source_repo = pathlib.Path(setup.__file__).resolve().parents[2]
    pyproject_name = setup._PYPROJECT_CONFIGURATION_FILE_NAME
    (refresh_repo / pyproject_name).write_bytes((source_repo / pyproject_name).read_bytes())
    shadow = refresh_repo / "src" / "llm_spendguard.egg-info"
    shadow.mkdir(parents=True)
    (shadow / "PKG-INFO").write_text("stale")
    original_setup_run = __import__("subprocess").run
    setup.subprocess = __import__("subprocess")
    setup.subprocess.run = lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout="1\n")
    try:
        setup.refresh_consumer_editable_package(sys.executable, refresh_repo, 10)
    finally:
        setup.subprocess.run = original_setup_run
    ck("successful refresh purges only the in-tree egg-info shadow", not shadow.exists())
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

    print("\n-- deployment completeness touches and verifies every persistent registration --")
    repo_root = pathlib.Path(fleet.__file__).resolve().parents[2]
    registered = [{"python_executable": str(home / name / "bin" / "python"), "venv_root": str(home / name)}
                  for name in ("consumer-a", "consumer-b", "consumer-c")]
    fleet.registered_interpreters = lambda: registered
    probed = []
    from spendguard import pricing

    print("\n-- path-injected freshness requires managed source identity at HEAD --")
    managed_pathinjected_identity = {
        "state": "pathinjected_managed", "code_version": CURRENT_SOURCE_VERSION,
        "metadata_version": None, "catalog_verified": pricing.PRICING_VERIFIED}
    ck("managed path injection at current code and catalog is verified fresh",
       fleet._consumer_install_is_verified_fresh(
           managed_pathinjected_identity, CURRENT_SOURCE_VERSION, pricing.PRICING_VERIFIED))
    stale_pathinjected_code = {**managed_pathinjected_identity, "code_version": PINNED_CONSUMER_VERSION}
    stale_pathinjected_catalog = {**managed_pathinjected_identity, "catalog_verified": None}
    foreign_pathinjected_identity = {**managed_pathinjected_identity, "state": "pathinjected_foreign"}
    ck("managed path injection with stale code is not fresh",
       not fleet._consumer_install_is_verified_fresh(
           stale_pathinjected_code, CURRENT_SOURCE_VERSION, pricing.PRICING_VERIFIED))
    ck("managed path injection with stale catalog is not fresh",
       not fleet._consumer_install_is_verified_fresh(
           stale_pathinjected_catalog, CURRENT_SOURCE_VERSION, pricing.PRICING_VERIFIED))
    ck("foreign path injection is not fresh even with current identities",
       not fleet._consumer_install_is_verified_fresh(
           foreign_pathinjected_identity, CURRENT_SOURCE_VERSION, pricing.PRICING_VERIFIED))
    installed_missing_metadata_identity = {**managed_pathinjected_identity, "state": "editable_current"}
    installed_stale_metadata_identity = {
        **installed_missing_metadata_identity, "metadata_version": PINNED_CONSUMER_VERSION}
    ck("installed states still reject absent metadata identity",
       not fleet._consumer_install_is_verified_fresh(
           installed_missing_metadata_identity,
           CURRENT_SOURCE_VERSION, pricing.PRICING_VERIFIED))
    ck("installed states still reject mismatched metadata identity",
       not fleet._consumer_install_is_verified_fresh(
           installed_stale_metadata_identity,
           CURRENT_SOURCE_VERSION, pricing.PRICING_VERIFIED))

    def all_fresh_probe(python_executable, root, version, timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S):
        probed.append(str(python_executable))
        return {"state": "editable_current", "code_version": version, "metadata_version": version,
                "module_path": str(repo_root / "src" / "spendguard" / "__init__.py"), "editable": True,
                "catalog_verified": pricing.PRICING_VERIFIED}

    fleet._consumer_install_state = all_fresh_probe
    fresh_verdict = fleet.verify_and_refresh_registered_interpreters(apply=True)
    ck("all K registered deployments are touched and completeness is proven",
       len(set(probed)) == len(registered) and fresh_verdict["N_total"] == len(registered)
       and fresh_verdict["N_verified_fresh"] == len(registered) and fresh_verdict["complete"])

    def managed_pathinjected_probe(python_executable, root, version,
                                   timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S):
        return {"state": "pathinjected_managed", "code_version": version, "metadata_version": None,
                "module_path": str(repo_root / "src" / "spendguard" / "__init__.py"), "editable": False,
                "catalog_verified": pricing.PRICING_VERIFIED, "distribution_present": False}

    fleet._consumer_install_state = managed_pathinjected_probe
    fleet._refresh_editable_install = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("managed path injection must not invoke pip refresh"))
    pathinjected_verdict = fleet.verify_and_refresh_registered_interpreters(apply=True)
    ck("managed path-injected deployments verify fresh without invoking pip",
       pathinjected_verdict["complete"]
       and pathinjected_verdict["N_verified_fresh"] == len(registered)
       and pathinjected_verdict["N_updated"] == 0)

    refreshed = []

    def noneditable_current_probe(python_executable, root, version,
                                  timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S):
        return {"state": "noneditable_pinned", "code_version": version, "metadata_version": version,
                "module_path": str(home / "site-packages" / "spendguard" / "__init__.py"), "editable": False,
                "catalog_verified": pricing.PRICING_VERIFIED}

    fleet._consumer_install_state = noneditable_current_probe
    fleet._refresh_editable_install = lambda python, root, timeout=fleet._CONSUMER_EDITABLE_REFRESH_TIMEOUT_S: (
        refreshed.append(str(python)))
    noneditable_verdict = fleet.verify_and_refresh_registered_interpreters(apply=True)
    ck("current noneditable deployments are verified fresh without an unnecessary refresh",
       noneditable_verdict["complete"] and noneditable_verdict["N_verified_fresh"] == len(registered)
       and refreshed == [])

    def shadowed_probe(python_executable, root, version, timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S):
        return {"state": "editable_shadowed_by_egg_info", "code_version": version, "metadata_version": version,
                "module_path": str(repo_root / "src" / "spendguard" / "__init__.py"), "editable": False,
                "catalog_verified": pricing.PRICING_VERIFIED}

    fleet._consumer_install_state = shadowed_probe
    shadowed_verdict = fleet.verify_and_refresh_registered_interpreters(apply=False)
    ck("editable metadata shadowing remains an explicit deployment failure despite matching identities",
       not shadowed_verdict["complete"] and len(shadowed_verdict["failed"]) == len(registered))

    stale_python = registered[1]["python_executable"]
    absent_python = registered[2]["python_executable"]
    probed = []
    refreshed = []

    def one_stale_probe(python_executable, root, version, timeout=fleet._CONSUMER_INSTALL_PROBE_TIMEOUT_S):
        probed.append(str(python_executable))
        if str(python_executable) == absent_python:
            raise FileNotFoundError(str(python_executable))
        stale = str(python_executable) == stale_python
        return {"state": "editable_stale" if stale else "editable_current", "code_version": version,
                "metadata_version": PINNED_CONSUMER_VERSION if stale else version,
                "module_path": str(repo_root / "src" / "spendguard" / "__init__.py"), "editable": True,
                "catalog_verified": pricing.PRICING_VERIFIED}

    fleet._consumer_install_state = one_stale_probe
    fleet._refresh_editable_install = lambda python, root, timeout=180: refreshed.append(str(python))
    failed_verdict = fleet.verify_and_refresh_registered_interpreters(apply=True)
    ck("one forced-stale deployment prevents the green completeness verdict",
       len(set(probed)) == len(registered) and refreshed == [stale_python]
       and not failed_verdict["complete"] and failed_verdict["failed"]
       and failed_verdict["N_verified_fresh"] == len(registered) - 2)
    ck("an absent sibling deployment remains failed instead of receiving a blanket freshness pass",
       any(failure.get("python_executable") == absent_python and failure.get("error")
           for failure in failed_verdict["failed"]))
    fleet.registered_interpreters = lambda: (_ for _ in ()).throw(PermissionError("registry unreadable"))
    unreadable_verdict = fleet.verify_and_refresh_registered_interpreters(apply=True)
    ck("an unreadable authoritative registry fails deployment completeness loudly",
       not unreadable_verdict["complete"] and unreadable_verdict["failed"]
       and "authoritative registry" in unreadable_verdict["failed"][0]["error"])
finally:
    fleet._refresh_editable_install = original_install
    fleet._consumer_install_state = original_install_state
    fleet.registered_interpreters = original_registered_interpreters

print(f"\n{'[FAIL]' if fails else 'OK'} test_fleet_refresh_stale_installs: {len(fails)} failure(s)")
raise SystemExit(1 if fails else 0)
