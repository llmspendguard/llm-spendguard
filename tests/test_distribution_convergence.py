"""Offline red→green convergence and mutation proofs; no real installers or daemons."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spendguard import distribution as distribution, manifest  # noqa: E402

FIXTURE_CURRENT_VERSION = "fixture-current"
FIXTURE_PREVIOUS_VERSION = "fixture-previous"
FIXTURE_CURRENT_SHA = "fixture-green-sha"


def exercise_distribution_fixture():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkout = root / "checkout"
        interpreter = str(root / "consumer" / "bin" / "python")
        target = {"sha": FIXTURE_CURRENT_SHA, "version": FIXTURE_CURRENT_VERSION,
                  "checkout": str(checkout)}
        state = {"version": FIXTURE_PREVIOUS_VERSION, "metadata_version": FIXTURE_PREVIOUS_VERSION,
                 "sha": None, "gate_enforcing": True, "confirmed": True, "dirty": True}
        events = []
        inventory = {"interpreters": [{"python": interpreter, "venv": str(root / "consumer"), "kind": "venv"}],
                     "gaps": []}
        audit = {"consumers": [], "pruned_at_depth": []}

        def fixture_distribution_probe(python, settings):
            events.append(("probe", python))
            return copy.deepcopy(state)

        def fixture_distribution_reinstall(python, wheel_cache, version, timeout):
            events.append(("install", python))
            state.update(version=FIXTURE_CURRENT_VERSION, metadata_version=FIXTURE_CURRENT_VERSION)

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(distribution, "resolve_distribution_target", return_value=target))
            stack.enter_context(patch.object(distribution.config, "consumer_audit_settings", return_value={}))
            stack.enter_context(patch.object(distribution.setup, "discover_distribution_interpreters", return_value=inventory))
            stack.enter_context(patch.object(distribution.consumer_audit, "audit_consumer_workspaces", return_value=audit))
            stack.enter_context(patch.object(distribution, "probe_distribution_interpreter", side_effect=fixture_distribution_probe))
            stack.enter_context(patch.object(
                distribution.setup, "refresh_consumer_wheel_package",
                side_effect=fixture_distribution_reinstall))
            stack.enter_context(patch.object(distribution, "prepare_distribution_checkout", return_value=str(checkout)))
            stack.enter_context(patch.object(distribution, "ensure_distribution_wheel"))
            stack.enter_context(patch.object(
                distribution, "inspect_distribution_install",
                return_value={"install_kind": manifest.MANIFEST_WHEEL_KIND,
                              "wheel_version": FIXTURE_CURRENT_VERSION, "editable_origin": None}))
            stack.enter_context(patch.object(manifest, "manifest_path", return_value=root / "absent-manifest.json"))
            stack.enter_context(patch.object(distribution.setup, "install_hook", return_value=0))
            stack.enter_context(patch.object(distribution, "attest_distribution_host", return_value=distribution.distribution_surface_row("host", "host", True, True)))
            stack.enter_context(patch.object(distribution, "attest_distribution_api_consumers", return_value=[]))
            stack.enter_context(patch.object(distribution, "attest_distribution_daemons", return_value=[]))

            def assert_distribution_old_consumer_is_red():
                result = distribution.distribute_fleet(check=True)
                assert result["exit_code"] != 0 and interpreter in result["summary"]["stragglers"], result
                assert not any(event[0] == "install" for event in events)
                return result

            assert_distribution_old_consumer_is_red()
            # Removing either wheel identity check must make its corresponding red assertion fail.
            for field, wrong in (("version", FIXTURE_PREVIOUS_VERSION),
                                 ("metadata_version", FIXTURE_PREVIOUS_VERSION)):
                state.update(version=FIXTURE_CURRENT_VERSION, metadata_version=FIXTURE_CURRENT_VERSION,
                             sha=None, dirty=True)
                state[field] = wrong
                assert_distribution_old_consumer_is_red()
                original = distribution.distribution_wheel_interpreter_on_target
                def omit_distribution_identity_check(probe, expected, omitted=field, original=original):
                    modified = dict(probe)
                    modified[omitted] = expected["version"]
                    return original(modified, expected)
                with patch.object(distribution, "distribution_wheel_interpreter_on_target",
                                  side_effect=omit_distribution_identity_check):
                    try:
                        assert_distribution_old_consumer_is_red()
                    except AssertionError:
                        pass
                    else:
                        raise AssertionError(f"removed {field} check survived")
            state.update(version=FIXTURE_PREVIOUS_VERSION, metadata_version=FIXTURE_PREVIOUS_VERSION,
                         sha=None, dirty=True)
            dry = distribution.distribute_fleet(dry_run=True)
            assert dry["exit_code"] != 0 and not any(event[0] == "install" for event in events)
            green = distribution.distribute_fleet()
            assert green["exit_code"] == 0 and not green["summary"]["stragglers"]
            green_rows = {row["surface"]: row for row in green["surfaces"]}
            assert green_rows[interpreter]["on_version"] is True
            assert ("install", interpreter) in events
            events.clear()
            assert distribution.distribute_fleet(check=True)["exit_code"] == 0
            assert not any(event[0] == "install" for event in events)
            audit["pruned_at_depth"].append(str(root))
            complete = distribution.distribute_fleet(check=True)
            assert complete["exit_code"] == 0
            assert complete["enumeration"]["audit_pruned_at_depth"] == [str(root)]
            audit["pruned_at_depth"].clear()
            # Host health is load-bearing even when all package identities match.
            with patch.object(distribution, "attest_distribution_host", return_value=distribution.distribution_surface_row("host", "host", True, False, reason="doctor failed")):
                assert "host" in distribution.distribute_fleet(check=True)["summary"]["stragglers"]
            inventory["gaps"].append({"surface": "unreadable-subtree", "reason": "enumeration denied"})
            assert "unreadable-subtree" in distribution.distribute_fleet(check=True)["summary"]["stragglers"]
            inventory["gaps"].clear()
            # CLI returns the same nonzero contract and complete JSON.
            state["version"] = FIXTURE_PREVIOUS_VERSION
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = distribution.distribute_command(["--check", "--json", "--roots", str(root)])
            assert code != 0 and interpreter in json.loads(output.getvalue())["summary"]["stragglers"]
    print("OK distribution old consumer red→green; wheel version/metadata mutation kills; dry/check read-only; host/gaps/CLI")


exercise_distribution_fixture()
