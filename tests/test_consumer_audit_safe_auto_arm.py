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
FIXTURE_REPOSITORY_DISCOVERY_DEPTH = 1
FIXTURE_PRUNED_BOUNDARY_DEPTH = 0
FIXTURE_VENV_MARKER_CONTENT = "fixture = true\n"
FIXTURE_EXHAUSTION_SCAN_BUDGET = 1
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
    venv_like_ancestor = fixture_root / "venv-like-ancestor"
    venv_like_ancestor.mkdir()
    (venv_like_ancestor / "pyvenv.cfg").write_text("fixture = true\n")
    ancestor_nested_repo = venv_like_ancestor / "repository"
    ancestor_nested_repo.mkdir()
    (ancestor_nested_repo / "client.py").write_text("import openai\n")
    nested_venv = ancestor_nested_repo / ".venv"
    nested_venv.mkdir()
    (nested_venv / "pyvenv.cfg").write_text("fixture = true\n")
    (nested_venv / "ignored.py").write_text("import anthropic\n")
    ancestor_nested_facts = consumer_audit.repository_import_facts(ancestor_nested_repo)
    failure_count += check(
        "venv-like ancestor does not suppress repository source while nested venv source stays skipped",
        ancestor_nested_facts["sdk"] == ("openai",),
    )
    repo = fixture_root / "consumer"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "client.py").write_text("import openai\n")
    fixable_venv = make_fake_venv(repo)
    settings = {"workspace_roots": (fixture_root,), "workspace_extra_venv_dirs": (),
                "workspace_scan_depth": FIXTURE_MAXIMUM_DEPTH, "consumer_audit_exempt": ()}
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
        failure_count += check("spendguard-only source is classified as an LLM consumer",
                               spendguard_only_row["initial_verdict"] == "NOT-ENFORCING"
                               and spendguard_only_row["action"] == "armed"
                               and spendguard_only_row["final_verdict"] == "GATED+LATEST")
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

        guard_root = fixture_root / "consumer-audit-guards"
        guard_root.mkdir()
        exempt_repository = guard_root / "declared-consumer"
        sibling_repository = guard_root / "sibling-consumer"
        for consumer_repository in (exempt_repository, sibling_repository):
            consumer_repository.mkdir()
            (consumer_repository / consumer_audit.CONSUMER_AUDIT_GIT_MARKER_NAME).mkdir()
            (consumer_repository / "client.py").write_text("import openai\n")
            consumer_venv = consumer_repository / "environment"
            consumer_venv.mkdir()
            (consumer_venv / consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME).write_text(
                FIXTURE_VENV_MARKER_CONTENT
            )
        exempt_venv = exempt_repository / "environment"
        sibling_venv = sibling_repository / "environment"
        guard_settings = {"workspace_roots": (guard_root,), "workspace_extra_venv_dirs": (),
                          "workspace_scan_depth": FIXTURE_REPOSITORY_DISCOVERY_DEPTH,
                          "consumer_audit_exempt": ()}
        original_probe = consumer_audit.probe_consumer_interpreter
        consumer_audit.probe_consumer_interpreter = lambda _python: {
            "confirmed": False, "probe_error": "offline fixture"
        }
        without_exemption = consumer_audit.audit_consumer_workspaces(
            FIXTURE_DEPLOYED_SHA, settings=guard_settings, auto_arm=False
        )
        with_exemption = consumer_audit.audit_consumer_workspaces(
            FIXTURE_DEPLOYED_SHA,
            settings={**guard_settings, "consumer_audit_exempt": (exempt_repository,)},
            auto_arm=False,
        )
        without_exempt_row = next(
            row for row in without_exemption["consumers"] if row["venv"] == os.fspath(exempt_venv.resolve())
        )
        exempt_row = next(
            row for row in with_exemption["consumers"] if row["venv"] == os.fspath(exempt_venv.resolve())
        )
        sibling_row = next(
            row for row in with_exemption["consumers"] if row["venv"] == os.fspath(sibling_venv.resolve())
        )
        failure_count += check(
            "consumer_audit_exempt fail-before/pass-after changes only the declared consumer",
            without_exempt_row["final_verdict"] == consumer_audit.CONSUMER_AUDIT_UNCONFIRMED_INTERPRETER
            and without_exemption["remaining_gaps"] == 2
            and exempt_row["initial_verdict"] == consumer_audit.CONSUMER_AUDIT_EXEMPT
            and exempt_row["final_verdict"] == consumer_audit.CONSUMER_AUDIT_EXEMPT
            and exempt_row["action"] == consumer_audit.CONSUMER_AUDIT_ACTION_NONE
            and sibling_row["final_verdict"] == consumer_audit.CONSUMER_AUDIT_UNCONFIRMED_INTERPRETER
            and with_exemption["remaining_gaps"] == 1
            and with_exemption["exit_code"] == 1,
        )
        all_exempt = consumer_audit.audit_consumer_workspaces(
            FIXTURE_DEPLOYED_SHA,
            settings={**guard_settings, "consumer_audit_exempt": (guard_root,)},
            auto_arm=False,
        )
        failure_count += check(
            "all terminal EXEMPT consumers produce zero gaps and exit zero",
            all_exempt["remaining_gaps"] == 0 and all_exempt["exit_code"] == 0,
        )
        consumer_audit.probe_consumer_interpreter = original_probe

        pruned_guard_root = fixture_root / "pruned-gap-guards"
        noise_subtree = pruned_guard_root / "noise-subtree"
        hidden_venv_subtree = pruned_guard_root / "hidden-consumer-subtree"
        exempt_hidden_subtree = pruned_guard_root / "exempt-hidden-subtree"
        noise_subtree.mkdir(parents=True)
        hidden_marker_parent = hidden_venv_subtree / "first" / "second" / "third"
        exempt_marker_parent = exempt_hidden_subtree / "first" / "second" / "third"
        clean_nested_directory = noise_subtree / "first" / "second"
        hidden_marker_parent.mkdir(parents=True)
        exempt_marker_parent.mkdir(parents=True)
        clean_nested_directory.mkdir(parents=True)
        (hidden_marker_parent / consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME).write_text(
            FIXTURE_VENV_MARKER_CONTENT
        )
        (exempt_marker_parent / consumer_audit.CONSUMER_AUDIT_VENV_CONFIGURATION_NAME).write_text(
            FIXTURE_VENV_MARKER_CONTENT
        )
        # Fail-before: the former depth-2 ceiling dropped this depth-3 marker as clean.
        failure_count += check(
            "three-level-deep pruned venv fail-before/pass-after is detected",
        consumer_audit._pruned_subtree_may_contain_venv(hidden_venv_subtree) is True,
        )
        failure_count += check(
            "clean fully exhausted pruned subtree still drops",
        consumer_audit._pruned_subtree_may_contain_venv(noise_subtree) is False,
        )
        original_pruned_scan_budget = consumer_audit.CONSUMER_AUDIT_PRUNED_VENV_SCAN_BUDGET
        consumer_audit.CONSUMER_AUDIT_PRUNED_VENV_SCAN_BUDGET = FIXTURE_EXHAUSTION_SCAN_BUDGET
        try:
            failure_count += check(
                "pruned subtree scan budget exhaustion fails closed",
        consumer_audit._pruned_subtree_may_contain_venv(noise_subtree) is True,
            )
        finally:
            consumer_audit.CONSUMER_AUDIT_PRUNED_VENV_SCAN_BUDGET = original_pruned_scan_budget
        pruned_result = consumer_audit.audit_consumer_workspaces(
            FIXTURE_DEPLOYED_SHA,
            settings={"workspace_roots": (pruned_guard_root,), "workspace_extra_venv_dirs": (),
                      "workspace_scan_depth": FIXTURE_PRUNED_BOUNDARY_DEPTH,
                      "consumer_audit_exempt": (exempt_hidden_subtree,)},
            auto_arm=False,
        )
        failure_count += check(
            "deep pruned venv fail-before/pass-after counts while clean and exempt drop",
            pruned_result["pruned_at_depth"] == (os.fspath(hidden_venv_subtree.resolve()),)
            and pruned_result["remaining_gaps"] == 1 and pruned_result["exit_code"] == 1
            and os.fspath(noise_subtree.resolve()) not in pruned_result["pruned_at_depth"]
            and os.fspath(exempt_hidden_subtree.resolve()) not in pruned_result["pruned_at_depth"],
        )
        pruned_clean = consumer_audit.audit_consumer_workspaces(
            FIXTURE_DEPLOYED_SHA,
            settings={"workspace_roots": (pruned_guard_root,), "workspace_extra_venv_dirs": (),
                      "workspace_scan_depth": FIXTURE_PRUNED_BOUNDARY_DEPTH,
                      "consumer_audit_exempt": (hidden_venv_subtree, exempt_hidden_subtree)},
            auto_arm=False,
        )
        failure_count += check(
            "filtered pruned set controls end-to-end zero exit",
            pruned_clean["pruned_at_depth"] == () and pruned_clean["remaining_gaps"] == 0
            and pruned_clean["exit_code"] == 0,
        )

        original_cfg = config._cfg
        config._cfg = lambda: {
            "workspace_roots": (os.fspath(guard_root),),
            "workspace_extra_venv_dirs": (),
            "workspace_scan_depth": FIXTURE_REPOSITORY_DISCOVERY_DEPTH,
            "consumer_audit_exempt": (os.fspath(exempt_repository),),
        }
        resolved_guard_settings = config.consumer_audit_settings(checkout_path=guard_root)
        config._cfg = original_cfg
        failure_count += check(
            "consumer_audit_settings resolves the declared consumer_audit_exempt paths",
            resolved_guard_settings["consumer_audit_exempt"] == (exempt_repository.resolve(),),
        )
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
