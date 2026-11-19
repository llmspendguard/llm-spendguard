"""Offline coverage for MCP launchd installation and migration safety."""
import ast
import pathlib
import plistlib
import xml.parsers.expat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from spendguard.platform import mcp_http_launchd as jobs

# A launchd plist that is valid to Apple's parser and runs under launchd, but whose header comment
# contains a `--` sequence (`--http`), which the XML spec forbids inside a comment. Python's strict
# expat (plistlib) rejects it; Apple's `plutil` accepts it. This is the real shape measured on the
# live com.ashdamle.spendguard-mcp-http.plist that crashed the migration before the plutil fix.
DASHDASH_COMMENT_PLIST = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<!-- spendguard MCP: --http is a SINGLETON, do NOT remove -->\n'
    b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    b'<plist version="1.0"><dict><key>Label</key><string>com.test.dashdash</string></dict></plist>\n'
)


def fake_plutil_run(label=None, returncode=0, stderr=""):
    """Stand in for `plutil -extract Label raw` without the (Linux-absent) binary."""
    def run(args, **kwargs):
        assert args[0] == jobs.MCP_PLUTIL_COMMAND and args[1:4] == ["-extract", "Label", "raw"]
        return SimpleNamespace(returncode=returncode, stdout=("" if label is None else f"{label}\n"),
                               stderr=stderr)
    return run


def launchd_fixture(monkeypatch, tmp_path):
    path = tmp_path / "owned.plist"
    monkeypatch.setattr(jobs, "mcp_http_launchd_plist_path", lambda: path)
    monkeypatch.setattr(jobs, "mcp_http_install_pythonpath", lambda: str(tmp_path / "checkout" / "src"))
    monkeypatch.setattr(jobs.sys, "platform", "darwin")
    monkeypatch.setattr(jobs.fleet, "fleet_control_restarts_enabled", lambda: True)
    monkeypatch.setattr(jobs.fleet, "bootout_declared_launchd_consumer", Mock(return_value={"ok": True}))
    monkeypatch.setattr(jobs.fleet, "kickstart_declared_launchd_consumer", Mock(return_value={"ok": True}))
    launchctl = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(jobs, "mcp_http_launchctl", launchctl)
    monkeypatch.setattr(jobs.distribution_daemon, "distribution_daemon_settings",
                        lambda settings=None: {"mcp_endpoint": "http://localhost:12345/mcp"})
    return path, launchctl


def test_http_installer_absent_uses_resolved_settings(monkeypatch, tmp_path):
    from spendguard import receipt
    path, launchctl = launchd_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(receipt, "_spendguard_bin", lambda: "/resolved/spendguard")
    monkeypatch.setattr(jobs.distribution_daemon, "inspect_live_mcp_service",
                        lambda settings=None: {"listener_present": False, "package_owned": None})
    result = jobs.install_mcp_http_launchd_daemon()
    document = plistlib.loads(path.read_bytes())
    assert result["status"] == "installed"
    assert document["ProgramArguments"] == ["/resolved/spendguard", "mcp", "--http", "--port", "12345"]
    assert document["Label"] == jobs.MCP_HTTP_LAUNCHD_LABEL
    assert document["EnvironmentVariables"]["PYTHONPATH"] == result["pythonpath"]
    assert document["KeepAlive"] and document["RunAtLoad"]
    assert launchctl.call_args.args[0] == "bootstrap"


def test_http_installer_healthy_noop_and_dry_run(monkeypatch, tmp_path):
    path, launchctl = launchd_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs.distribution_daemon, "inspect_live_mcp_service",
                        lambda settings=None: {"package_owned": True, "listener_present": True})
    monkeypatch.setattr(jobs.distribution_daemon, "read_live_mcp_release_status", lambda settings=None: {})
    assert jobs.install_mcp_http_launchd_daemon()["status"] == "unchanged"
    assert jobs.install_mcp_http_launchd_daemon(apply=False)["status"] == "planned"
    assert not path.exists()
    launchctl.assert_not_called()


def test_http_installer_editable_and_wheel_paths(monkeypatch):
    import spendguard
    monkeypatch.setattr(jobs.manifest, "inspect_current_interpreter_install_kind",
                        lambda: {"install_kind": jobs.manifest.MANIFEST_EDITABLE_KIND})
    assert jobs.mcp_http_install_pythonpath() == str(pathlib.Path(spendguard.__file__).resolve().parent.parent)
    monkeypatch.setattr(jobs.manifest, "inspect_current_interpreter_install_kind", lambda: {"install_kind": "wheel"})
    assert jobs.mcp_http_install_pythonpath() is None


def test_http_installer_wheel_omits_pythonpath(monkeypatch, tmp_path):
    path, _ = launchd_fixture(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "mcp_http_install_pythonpath", lambda: None)
    monkeypatch.setattr(jobs.distribution_daemon, "inspect_live_mcp_service",
                        lambda settings=None: {"listener_present": False, "package_owned": None})
    assert jobs.install_mcp_http_launchd_daemon()["status"] == "installed"
    assert "PYTHONPATH" not in plistlib.loads(path.read_bytes())["EnvironmentVariables"]


def migration_fixture(monkeypatch, launchd_fixture, tmp_path):
    foreign = tmp_path / "foreign.plist"
    foreign.write_bytes(plistlib.dumps({"Label": "foreign.test"}))
    monkeypatch.setattr(jobs.distribution_daemon, "inspect_live_mcp_service",
                        lambda settings=None: {"package_owned": False, "label_used": "foreign.test"})
    monkeypatch.setattr(jobs, "mcp_http_foreign_plist_path", lambda label: foreign)
    monkeypatch.setattr(jobs.release, "_git_src", lambda *args: "source-sha")
    monkeypatch.setattr(jobs, "install_mcp_http_launchd_daemon", lambda **kwargs: {"status": "installed"})
    monkeypatch.setattr(jobs.distribution_daemon, "restart_and_verify_live_mcp",
                        lambda **kwargs: {"verified": False, "package_owned": True, "reason": "stale"})
    return foreign


def assert_unverified_foreign_preserved(migrate, foreign):
    result = migrate(apply=True)
    assert not result["migrated"]
    assert not result["verified"]
    assert result["restored"]
    assert foreign.exists()


def test_migration_failure_preserves_foreign_and_restores(monkeypatch, tmp_path):
    fixture = launchd_fixture(monkeypatch, tmp_path)
    foreign = migration_fixture(monkeypatch, fixture, tmp_path)
    assert_unverified_foreign_preserved(jobs.migrate_mcp_http_to_package_owned, foreign)
    assert jobs.fleet.bootout_declared_launchd_consumer.call_count == 2
    assert fixture[1].call_args.args[0] == "bootstrap"


def test_migration_verification_guard_mutation_is_caught(monkeypatch, tmp_path):
    fixture = launchd_fixture(monkeypatch, tmp_path)
    foreign = migration_fixture(monkeypatch, fixture, tmp_path)
    source = pathlib.Path(jobs.__file__).read_text()
    original = 'verification.get("verified") is not True'
    assert source.count(original) == 1
    mutant = source.replace(original, 'verification.get("verified") is not False')
    namespace = dict(vars(jobs))
    exec(compile(ast.parse(mutant), jobs.__file__, "exec"), namespace)
    with pytest.raises(AssertionError):
        assert_unverified_foreign_preserved(namespace["migrate_mcp_http_to_package_owned"], foreign)


def test_migration_dry_run_has_no_side_effects(monkeypatch, tmp_path):
    fixture = launchd_fixture(monkeypatch, tmp_path)
    foreign = migration_fixture(monkeypatch, fixture, tmp_path)
    before = foreign.read_bytes()
    result = jobs.migrate_mcp_http_to_package_owned()
    assert [step["action"] for step in result["plan"]] == ["backup", "bootout", "install", "verify", "remove"]
    assert foreign.read_bytes() == before
    assert list(tmp_path.glob("*.bak")) == []
    jobs.fleet.bootout_declared_launchd_consumer.assert_not_called()
    fixture[1].assert_not_called()


def test_migration_removes_foreign_only_after_verified_owned_replacement(monkeypatch, tmp_path):
    fixture = launchd_fixture(monkeypatch, tmp_path)
    foreign = migration_fixture(monkeypatch, fixture, tmp_path)
    monkeypatch.setattr(jobs.distribution_daemon, "restart_and_verify_live_mcp",
                        lambda **kwargs: {"verified": True, "package_owned": True})
    result = jobs.migrate_mcp_http_to_package_owned(apply=True)
    assert result["migrated"] and result["verified"]
    assert not foreign.exists()
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_foreign_plist_resolved_from_launchd(monkeypatch, tmp_path):
    path = tmp_path / "unusual.plist"
    path.write_bytes(plistlib.dumps({"Label": "foreign.test"}))
    monkeypatch.setattr(jobs, "mcp_http_launchctl", lambda *args: SimpleNamespace(
        returncode=0, stdout=f"path = {path}\n", stderr=""))
    monkeypatch.setattr(jobs.subprocess, "run", fake_plutil_run(label="foreign.test"))
    assert jobs.mcp_http_foreign_plist_path("foreign.test") == path
    with pytest.raises(ValueError):
        jobs.mcp_http_foreign_plist_path("another.test")


def test_foreign_plist_label_read_via_apple_parser_not_strict_xml(monkeypatch, tmp_path):
    # The real failure shape: a launchd-valid plist whose comment carries `--`, which strict
    # expat rejects. The migration read path must NOT depend on plistlib for the foreign plist.
    path = tmp_path / "dashdash.plist"
    path.write_bytes(DASHDASH_COMMENT_PLIST)
    with pytest.raises((xml.parsers.expat.ExpatError, plistlib.InvalidFileException)):
        plistlib.loads(path.read_bytes())  # proves the pre-fix strict path would crash
    monkeypatch.setattr(jobs.subprocess, "run", fake_plutil_run(label="com.test.dashdash"))
    assert jobs.mcp_http_read_plist_label(path) == "com.test.dashdash"
    monkeypatch.setattr(jobs, "mcp_http_launchctl", lambda *args: SimpleNamespace(
        returncode=0, stdout=f"path = {path}\n", stderr=""))
    assert jobs.mcp_http_foreign_plist_path("com.test.dashdash") == path


def test_foreign_plist_label_read_fails_closed_on_plutil_error(monkeypatch, tmp_path):
    path = tmp_path / "broken.plist"
    path.write_bytes(DASHDASH_COMMENT_PLIST)
    monkeypatch.setattr(jobs.subprocess, "run",
                        fake_plutil_run(returncode=1, stderr="could not extract Label"))
    with pytest.raises(ValueError):
        jobs.mcp_http_read_plist_label(path)


def test_migration_dry_run_survives_dashdash_comment_plist(monkeypatch, tmp_path):
    # The whole-migration guarantee: a foreign listener whose plist breaks strict XML must still
    # produce an ordered plan from apply=False, never an uncaught ExpatError.
    fixture = launchd_fixture(monkeypatch, tmp_path)
    foreign = tmp_path / "foreign-dashdash.plist"
    foreign.write_bytes(DASHDASH_COMMENT_PLIST)
    monkeypatch.setattr(jobs.distribution_daemon, "inspect_live_mcp_service",
                        lambda settings=None: {"package_owned": False, "label_used": "com.test.dashdash"})
    monkeypatch.setattr(jobs, "mcp_http_launchctl", lambda *args: SimpleNamespace(
        returncode=0, stdout=f"path = {foreign}\n", stderr=""))
    monkeypatch.setattr(jobs.subprocess, "run", fake_plutil_run(label="com.test.dashdash"))
    monkeypatch.setattr(jobs.release, "_git_src", lambda *args: "source-sha")
    result = jobs.migrate_mcp_http_to_package_owned()
    assert [step["action"] for step in result["plan"]] == ["backup", "bootout", "install", "verify", "remove"]
    assert foreign.read_bytes() == DASHDASH_COMMENT_PLIST  # untouched by a dry run


if __name__ == "__main__":
    from _pytest_fixture_bridge import run_pytest_style
    raise SystemExit(run_pytest_style(globals()))
