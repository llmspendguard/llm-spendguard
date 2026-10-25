"""Consumer audit proof: deterministic enumeration, exact verdicts, and narrowly-scoped auto-arm."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="consumer-audit-home-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config, consumer_audit  # noqa: E402

FIXTURE_DEPLOYED_SHA = "fixture-deployed-sha"
FIXTURE_MAXIMUM_DEPTH = 2
failure_count = 0


def check(label, condition):
    print(("  [OK] " if condition else "  [FAIL] ") + label)
    return 0 if condition else 1


def make_fake_venv(root, package_present=True):
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", os.fspath(venv)], check=True)
    python = venv / "bin" / "python"
    purelib = Path(subprocess.run(
        [os.fspath(python), "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    if package_present:
        package = purelib / "spendguard"
        package.mkdir()
        (package / "__init__.py").write_text(
            "import sys, types\n"
            "__version__ = 'fixture-version'\n"
            "def install():\n"
            " gate = types.ModuleType('spendguard.gate')\n"
            " gate.fixture_enforcing = True\n"
            " sys.modules['spendguard.gate'] = gate\n"
            "def require():\n"
            " gate = sys.modules.get('spendguard.gate')\n"
            " if gate is None or not getattr(gate, 'fixture_enforcing', False):\n"
            "  raise RuntimeError('fixture gate refused')\n"
        )
    return venv


with tempfile.TemporaryDirectory(prefix="consumer-audit-proof-") as temporary:
    fixture_root = Path(temporary)
    config.HOME = fixture_root / "spendguard-state"
    config.KEYS_ENV = config.HOME / "keys.env"
    structurally_valid_venv = fixture_root / ".environment"
    structurally_valid_venv.mkdir()
    (structurally_valid_venv / "pyvenv.cfg").write_text("fixture = true\n")
    misleadingly_named_directory = fixture_root / "venv"
    misleadingly_named_directory.mkdir()
    failure_count += check("nonstandard venv name is accepted from structure",
                           consumer_audit._is_venv_directory(structurally_valid_venv))
    failure_count += check("venv-like name without structure is rejected",
                           not consumer_audit._is_venv_directory(misleadingly_named_directory))
    repo = fixture_root / "consumer"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "client.py").write_text("import openai\n")
    fixable_venv = make_fake_venv(repo)
    settings = {"workspace_roots": (fixture_root,), "workspace_extra_venv_dirs": (),
                "workspace_scan_depth": FIXTURE_MAXIMUM_DEPTH}
    extra_venv_root = fixture_root / "external-environments"
    extra_venv_root.mkdir()
    external_venv = make_fake_venv(extra_venv_root)
    external_non_venv = extra_venv_root / "ordinary-directory"
    external_non_venv.mkdir()
    extra_directory_settings = {**settings, "workspace_extra_venv_dirs": (extra_venv_root,)}
    enumerated_venvs = consumer_audit.enumerate_consumer_venvs(
        repo, extra_directory_settings["workspace_extra_venv_dirs"]
    )
    failure_count += check(
        "configured extra directory includes only structurally valid venv children",
        external_venv.resolve() in enumerated_venvs
        and external_non_venv.resolve() not in enumerated_venvs,
    )

    original_checkout = consumer_audit._checkout_for_module
    original_git_output = consumer_audit._git_output
    original_live_pids = consumer_audit._live_pids
    consumer_audit._checkout_for_module = lambda _module_path: fixture_root
    consumer_audit._git_output = lambda _checkout, *arguments: FIXTURE_DEPLOYED_SHA
    consumer_audit._live_pids = lambda _python: []
    try:
        initial_probe = consumer_audit.probe_consumer_interpreter(fixable_venv / "bin" / "python")
        failure_count += check("fixture starts NOT-ENFORCING", not initial_probe["gate_enforcing"])
        first = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        fixable_row = first["consumers"][0]
        failure_count += check("armed-fixable -> ENFORCING + zero", fixable_row["initial_verdict"] == "NOT-ENFORCING"
              and fixable_row["action"] == "armed" and fixable_row["final_verdict"] == "GATED+LATEST"
              and first["exit_code"] == 0)

        settled_once = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        settled_twice = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        failure_count += check("deterministic repeated final audit output",
              consumer_audit.render_consumer_audit_table(settled_once)
              == consumer_audit.render_consumer_audit_table(settled_twice))

        broken_repo = fixture_root / "broken"
        broken_repo.mkdir()
        (broken_repo / ".git").mkdir()
        (broken_repo / "client.py").write_text("import anthropic\n")
        broken_venv = make_fake_venv(broken_repo, package_present=False)
        before = tuple(sorted(path.relative_to(broken_venv) for path in broken_venv.rglob("*")))
        broken = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        broken_row = next(row for row in broken["consumers"] if row["venv"] == os.fspath(broken_venv.resolve()))
        after = tuple(sorted(path.relative_to(broken_venv) for path in broken_venv.rglob("*")))
        failure_count += check("unfixable -> untouched + non-zero", broken_row["initial_verdict"] == "NO-SPENDGUARD"
              and broken_row["action"] == "none" and broken["exit_code"] != 0 and before == after)
        failure_count += check("deliberately unfixable audit exits non-zero", broken["remaining_gaps"] > 0)

        uncertain_repo = fixture_root / "uncertain"
        uncertain_repo.mkdir()
        (uncertain_repo / ".git").mkdir()
        uncertain_venv = make_fake_venv(uncertain_repo)
        uncertain = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        uncertain_row = next(
            row for row in uncertain["consumers"] if row["venv"] == os.fspath(uncertain_venv.resolve())
        )
        failure_count += check("absent LLM-import fact stays undetermined and non-zero",
                               uncertain_row["final_verdict"] == "LLM-USE-UNDETERMINED"
                               and uncertain_row["action"] == "none" and uncertain["exit_code"] != 0)

        spendguard_only_repo = fixture_root / "spendguard-only"
        spendguard_only_repo.mkdir()
        (spendguard_only_repo / ".git").mkdir()
        (spendguard_only_repo / "client.py").write_text("import spendguard\n")
        spendguard_only_venv = make_fake_venv(spendguard_only_repo)
        spendguard_only = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        spendguard_only_row = next(
            row for row in spendguard_only["consumers"]
            if row["venv"] == os.fspath(spendguard_only_venv.resolve())
        )
        failure_count += check("spendguard-only source is not classified as an LLM consumer",
                               spendguard_only_row["final_verdict"] == "LLM-USE-UNDETERMINED"
                               and spendguard_only_row["action"] == "none")
        failure_count += check("source AST facts report SDK and spendguard imports separately",
                               spendguard_only_row["sdk_imports_source"] == ()
                               and spendguard_only_row["spendguard_import_source"] is True)

        tripwire_repo = fixture_root / "tripwire"
        tripwire_repo.mkdir()
        (tripwire_repo / ".git").mkdir()
        tripwire_source = tripwire_repo / "client.py"
        tripwire_source.write_text("provider_name = 'anthropic'\n")
        tripwire_venv = make_fake_venv(tripwire_repo)
        flagged = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        flagged_row = next(
            row for row in flagged["consumers"] if row["venv"] == os.fspath(tripwire_venv.resolve())
        )
        tripwire_source.write_text("provider_name = 'provider'\n")
        unflagged = consumer_audit.audit_consumer_workspaces(FIXTURE_DEPLOYED_SHA, settings=settings)
        unflagged_row = next(
            row for row in unflagged["consumers"] if row["venv"] == os.fspath(tripwire_venv.resolve())
        )
        failure_count += check("text mention without AST import raises the completeness tripwire",
                               flagged_row["source_text_mentions"] == ("anthropic",)
                               and flagged_row["completeness_flag"] == "text-mentions-not-imported:anthropic"
                               and flagged_row["sdk_imports_source"] == ())
        failure_count += check("completeness tripwire is advisory only",
                               flagged_row["final_verdict"] == unflagged_row["final_verdict"]
                               and flagged["remaining_gaps"] == unflagged["remaining_gaps"]
                               and flagged["exit_code"] == unflagged["exit_code"])

        discovered = consumer_audit.enumerate_consumer_repositories((fixture_root,), FIXTURE_MAXIMUM_DEPTH)
        failure_count += check("repository enumeration is sorted and complete",
                               discovered["repositories"]
                               == tuple(sorted((repo.resolve(), broken_repo.resolve(), uncertain_repo.resolve(),
                                                spendguard_only_repo.resolve(), tripwire_repo.resolve()))))
        deep_repository = fixture_root / "nested" / "deeper" / "consumer"
        deep_repository.mkdir(parents=True)
        (deep_repository / ".git").mkdir()
        bounded_discovery = consumer_audit.enumerate_consumer_repositories(
            (fixture_root,), FIXTURE_MAXIMUM_DEPTH
        )
        failure_count += check("bounded repository discovery reports the exact pruned subtree",
                               bounded_discovery["pruned_at_depth"] == (deep_repository.resolve(),))
        failure_count += check("sha equality classification", consumer_audit.classify_deployed_sha(
            FIXTURE_DEPLOYED_SHA, FIXTURE_DEPLOYED_SHA, FIXTURE_DEPLOYED_SHA) == "==")
        failure_count += check("sha behind classification", consumer_audit.classify_deployed_sha(
            "older-sha", "older-sha", FIXTURE_DEPLOYED_SHA) == "behind")
    finally:
        consumer_audit._checkout_for_module = original_checkout
        consumer_audit._git_output = original_git_output
        consumer_audit._live_pids = original_live_pids

print(f"\n{'[FAIL]' if failure_count else 'OK'} test_consumer_audit_safe_auto_arm: {failure_count} failure(s)")
sys.exit(1 if failure_count else 0)
