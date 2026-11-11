"""GUARD — release.py's green pointer: a server can tell whether it is serving the latest gate-green commit,
and the pointer only ever names COMMITTED code. This is the load-bearing signal behind "always serve the latest
with the best": if stale_vs_green() lied, a fleet of long-running MCP servers would keep serving old code with
nothing to show it (the exact incident this module exists to prevent).

Pins: (1) served_sha() is the frozen identity of THIS process (memoised write-once); (2) stale_vs_green() is
True only when a NEWER green sha is deployed than the one served, and degrades to False on any unknown;
(3) promote_release refuses a DIRTY tree (the pointer must name committed code) and an unresolvable sha, writes
atomically through config.update_json (leaving a `~` backup), and a later promote advances it; (4) release_status
reports the up-to-date / stale / no-pointer / unknown cases faithfully.

Hermetic: isolated SPENDGUARD_HOME; git is stubbed via release._git_src; no network, no real repo, no ledger."""
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

import yaml

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-release-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import release   # noqa: E402
from spendguard.analyze.assurance.capability_readiness import capability_selfcheck_exit_code  # noqa: E402
from spendguard.platform import manifest  # noqa: E402

fails = []



# ── a controllable fake git: HEAD sha, dirty flag, and ref resolution are all dials ──
STATE = {"head": "a" * 40, "dirty": False, "known": {"a" * 40, "b" * 40}}


def _fake_git(*args):
    if args[:2] == ("rev-parse", "HEAD"):
        return STATE["head"]
    if args[:1] == ("rev-parse",):                    # peel/validate an arbitrary ref → full sha or None
        ref = args[1].removesuffix("^{commit}")
        return ref if ref in STATE["known"] else None
    if args[:2] == ("status", "--porcelain"):
        return "M src/spendguard/x.py" if STATE["dirty"] else ""
    if args[:1] == ("describe",):
        described_sha = args[-1] if len(args) > 3 else STATE["head"]
        return "v0.10.0-1-g" + described_sha[:7]
    return None


release._git_src = _fake_git


def _fake_editable_install_kind():
    return {"install_kind": manifest.MANIFEST_EDITABLE_KIND}


manifest.inspect_current_interpreter_install_kind = _fake_editable_install_kind


def _reset_served():
    release.served_sha.cache_clear()                  # served identity is frozen per-process; clear between setups


print("-- served_sha() is the frozen identity of THIS process --")
_reset_served()
s = release.served_sha()
record_script_check(fails, "served_sha reports HEAD (sha/short/describe/dirty)",
   s and s["sha"] == "a" * 40 and s["short"] == "a" * 7 and s["dirty"] is False)
STATE["head"] = "c" * 40                              # HEAD 'moves' but the process identity must NOT
record_script_check(fails, "served_sha is FROZEN (memoised) — it does not follow a later HEAD change", release.served_sha()["sha"] == "a" * 40)
STATE["head"] = "a" * 40

print("\n-- no pointer yet → not stale, and status says so --")
record_script_check(fails, "green_pointer() is None before any deploy", release.green_pointer() is None)
record_script_check(fails, "stale_vs_green() is False with no pointer (never respawn on an unknown)", release.stale_vs_green() is False)
record_script_check(fails, "release_status: served known, green None, not stale", release.release_status()["green"] is None
   and release.release_status()["stale"] is False)

print("\n-- promote_release REFUSES a dirty tree and an unresolvable sha --")
STATE["dirty"] = True
_dirty_refused = False
try:
    release.promote_release()
except ValueError:
    _dirty_refused = True
record_script_check(fails, "a dirty working tree is refused (the pointer names only committed code)", _dirty_refused)
STATE["dirty"] = False
_badref_refused = False
try:
    release.promote_release("deadbeef")               # not in STATE['known'] → git cannot resolve
except ValueError:
    _badref_refused = True
record_script_check(fails, "a sha git cannot resolve is refused", _badref_refused)

print("\n-- promote to the SERVED sha → up to date; then advance → stale --")
rec = release.promote_release(gate="chunked_suite green")
record_script_check(fails, "promote wrote the pointer at the served sha", rec["sha"] == "a" * 40 and release.green_pointer()["sha"] == "a" * 40)
record_script_check(fails, "gate result is recorded on the pointer", release.green_pointer().get("gate") == "chunked_suite green")
record_script_check(fails, "served == green → NOT stale", release.stale_vs_green() is False)
record_script_check(fails, "release_status note reads up-to-date", "up to date" in release.release_status()["note"])

# a newer commit is deployed (HEAD moves to a KNOWN newer sha, tree clean) → the frozen process is now behind it
STATE["head"] = "b" * 40
rec2 = release.promote_release()
record_script_check(fails, "a second promote advances the pointer to the new sha", rec2["sha"] == "b" * 40 and release.green_pointer()["sha"] == "b" * 40)
record_script_check(fails, "update_json left a `~` backup of the previous pointer (non-destructive advance)",
   pathlib.Path(str(release.pointer_path()) + "~").exists())
record_script_check(fails, "served (a…) now BEHIND green (b…) → STALE", release.stale_vs_green() is True)
record_script_check(fails, "release_status note flags STALE with both short shas", "STALE" in release.release_status()["note"]
   and release.release_status()["stale"] is True)

print("\n-- should_respawn() is loop-safe: exit ONLY when a fresh process would land on green --")
# live HEAD == green (b) and this frozen process serves a → a respawn WOULD reach green → respawn.
record_script_check(fails, "should_respawn TRUE when live HEAD == green and the process is behind it", release.should_respawn() is True)
STATE["known"].add("d" * 40)
STATE["head"] = "d" * 40                              # HEAD moved PAST green (d) without a deploy; green stays b
record_script_check(fails, "should_respawn FALSE when HEAD != green (a respawn would serve d, not green — no exit loop)",
   release.should_respawn() is False)
record_script_check(fails, "... yet stale_vs_green stays True (the process IS behind green; it just cannot fix it by respawning)",
   release.stale_vs_green() is True)
STATE["head"] = "b" * 40                              # restore for any later assertions

print("\n-- unknown version (not a git tree) → served None, never stale --")
release._git_src = lambda *a: None
_reset_served()
record_script_check(fails, "served_sha() is None when git is unavailable", release.served_sha() is None)
record_script_check(fails, "stale_vs_green() is False when the served version is unknown", release.stale_vs_green() is False)
record_script_check(fails, "release_status note says version unknown, server keeps running", "unknown" in release.release_status()["note"])

print("\n-- the tag release cannot publish without exact-commit green CI proof --")
repository_root = pathlib.Path(__file__).resolve().parents[1]
release_workflow_path = repository_root / ".github" / "workflows" / "release.yml"
release_workflow = yaml.safe_load(release_workflow_path.read_text())
release_test_steps = release_workflow["jobs"]["test"]["steps"]
RELEASE_EXACT_SHA_GATE_STEP_NAME = "Require successful CI for the exact tagged commit"
RELEASE_VERSION_SSOT_STEP_NAME_PREFIX = "Version SSOT"
release_exact_sha_gate_step = next(
    (step for step in release_test_steps if step.get("name") == RELEASE_EXACT_SHA_GATE_STEP_NAME),
    None,
)
release_version_ssot_step = next(
    (step for step in release_test_steps
     if str(step.get("name", "")).startswith(RELEASE_VERSION_SSOT_STEP_NAME_PREFIX)),
    None,
)
RELEASE_EXACT_SHA_GATE_SCRIPT = "scripts/ci/require_successful_ci_for_tagged_commit.sh"
RELEASE_VERSION_SSOT_SCRIPT = "scripts/ci/assert_release_tag_matches_source_version.sh"
release_exact_sha_gate_command = (release_exact_sha_gate_step or {}).get("run")
release_version_ssot_command = (release_version_ssot_step or {}).get("run")

record_script_check(fails, "release exact-SHA gate step invokes its executable gate script",
    release_exact_sha_gate_command == f"bash {RELEASE_EXACT_SHA_GATE_SCRIPT}")
record_script_check(fails, "release version-SSOT step invokes its executable gate script",
    release_version_ssot_command == f"bash {RELEASE_VERSION_SSOT_SCRIPT}")
record_script_check(
    fails,
    "publish needs the test job, so exact-SHA CI gate failure blocks publication",
    release_workflow["jobs"]["publish"].get("needs") == "test",
)

with tempfile.TemporaryDirectory(prefix="release-gate-contract-") as release_gate_temp_directory:
    release_gate_temp_path = pathlib.Path(release_gate_temp_directory)
    fake_gh_path = release_gate_temp_path / "gh"
    fake_gh_path.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$FAKE_GH_SUCCESSFUL_RUN_COUNT\"\n")
    fake_gh_path.chmod(fake_gh_path.stat().st_mode | stat.S_IXUSR)
    release_gate_environment = os.environ.copy()
    release_gate_environment.update({
        "GITHUB_REPOSITORY": "test-owner/test-repository",
        "GITHUB_SHA": "1" * 40,
        "PATH": os.pathsep.join((
            str(release_gate_temp_path), str(pathlib.Path(sys.executable).parent), release_gate_environment["PATH"],
        )),
    })
    exact_sha_gate_script_path = repository_root / RELEASE_EXACT_SHA_GATE_SCRIPT

    zero_success_environment = release_gate_environment | {"FAKE_GH_SUCCESSFUL_RUN_COUNT": "0"}
    zero_success_result = subprocess.run(
        ["bash", str(exact_sha_gate_script_path)], cwd=repository_root, env=zero_success_environment,
        capture_output=True, text=True, check=False,
    )
    record_script_check(fails, "exact-SHA gate exits nonzero and annotates zero successful CI runs",
        zero_success_result.returncode != 0
        and "::error::" in zero_success_result.stdout + zero_success_result.stderr)

    successful_ci_environment = release_gate_environment | {"FAKE_GH_SUCCESSFUL_RUN_COUNT": "2"}
    successful_ci_result = subprocess.run(
        ["bash", str(exact_sha_gate_script_path)], cwd=repository_root, env=successful_ci_environment,
        capture_output=True, text=True, check=False,
    )
    record_script_check(fails, "exact-SHA gate exits zero when successful CI proof exists",
        successful_ci_result.returncode == 0)

    temporary_version_source_path = release_gate_temp_path / "_version.py"
    temporary_version_source_path.write_text('__version__ = "3.4.5"\n')
    version_gate_script_path = repository_root / RELEASE_VERSION_SSOT_SCRIPT
    matching_version_environment = release_gate_environment | {
        "GITHUB_REF_NAME": "v3.4.5",
        "SPENDGUARD_RELEASE_VERSION_SOURCE_PATH": str(temporary_version_source_path),
    }
    matching_version_result = subprocess.run(
        ["bash", str(version_gate_script_path)], cwd=repository_root, env=matching_version_environment,
        capture_output=True, text=True, check=False,
    )
    record_script_check(fails, "version-SSOT gate exits zero when tag and source version match",
        matching_version_result.returncode == 0)

    drifted_version_environment = matching_version_environment | {"GITHUB_REF_NAME": "v3.4.6"}
    drifted_version_result = subprocess.run(
        ["bash", str(version_gate_script_path)], cwd=repository_root, env=drifted_version_environment,
        capture_output=True, text=True, check=False,
    )
    record_script_check(fails, "version-SSOT gate exits nonzero and annotates tag/source drift",
        drifted_version_result.returncode != 0
        and "::error::" in drifted_version_result.stdout + drifted_version_result.stderr)

passing_outcome = {"id": "source-ratchet", "status": "PASS"}
environment_unreviewed_outcome = {"id": "env-parity-in-verification", "status": "UNREVIEWED"}
failing_outcome = {"id": "architecture-ratchet", "status": "FAIL"}
other_unreviewed_outcome = {"id": "another-check", "status": "UNREVIEWED"}
tolerated_environment_identifier = (environment_unreviewed_outcome["id"],)
record_script_check(fails, "named env-parity tolerance exits zero", capability_selfcheck_exit_code(
    {"items": [passing_outcome, environment_unreviewed_outcome]}, tolerated_environment_identifier) == 0)
record_script_check(fails, "untolerated env-parity exits nonzero", capability_selfcheck_exit_code(
    {"items": [passing_outcome, environment_unreviewed_outcome]}) == 1)
record_script_check(fails, "a FAIL remains blocking beside tolerated env-parity", capability_selfcheck_exit_code(
    {"items": [environment_unreviewed_outcome, failing_outcome]}, tolerated_environment_identifier) == 1)
record_script_check(fails, "another UNREVIEWED remains blocking beside tolerated env-parity", capability_selfcheck_exit_code(
    {"items": [environment_unreviewed_outcome, other_unreviewed_outcome]}, tolerated_environment_identifier) == 1)

print(f"\n{'[FAIL]' if fails else 'OK'} test_release_green_pointer: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
