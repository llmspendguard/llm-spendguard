"""Offline tests for the installable scheduler (no real launchd/cron/schtasks touched).

`spendguard schedule` is a pip package wiring the OS-native scheduler, so the three backends must each emit a
correct, idempotent, removable spec. We mock subprocess + the home dir and assert the spec that gets handed to
launchd / crontab / schtasks — including the Windows /tr quoting (a python path with spaces must not break)."""
import plistlib
import types
import pathlib
import pytest
from spendguard import schedule, config


class _Recorder:
    """Stand-in for subprocess.run: records every call, returns rc=0. `crontab -l` yields `crontab_out`."""
    def __init__(self, crontab_out=""):
        self.calls = []
        self.crontab_out = crontab_out

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        out = self.crontab_out if args[:2] == ["crontab", "-l"] else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


@pytest.fixture
def rec(monkeypatch, tmp_path):
    r = _Recorder()
    monkeypatch.setattr(schedule.subprocess, "run", r)
    monkeypatch.setattr(config, "HOME", tmp_path / ".spendguard")
    return r


def _force(monkeypatch, platform, exe="/usr/bin/python3"):
    monkeypatch.setattr(schedule.sys, "platform", platform)
    monkeypatch.setattr(schedule.sys, "executable", exe)


# ── macOS: daily = wall-clock StartCalendarInterval; hourly = StartInterval ──
def test_macos_daily_uses_calendar_interval(rec, monkeypatch, tmp_path):
    _force(monkeypatch, "darwin")
    monkeypatch.setattr(schedule.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    r = schedule.install(interval="daily")
    plist_path = pathlib.Path(r["installed"])
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["StartCalendarInterval"] == {"Hour": 0, "Minute": 0}, "daily must anchor to wall-clock 00:00"
    assert "StartInterval" not in plist, "daily must NOT drift on a relative interval"
    assert plist["ProgramArguments"][-3:] == ["saas", "sync", "--if-due"]
    assert r["scheduler"] == "launchd"


def test_macos_hourly_uses_start_interval(rec, monkeypatch, tmp_path):
    _force(monkeypatch, "darwin")
    monkeypatch.setattr(schedule.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    r = schedule.install(interval="hourly")
    plist = plistlib.loads(pathlib.Path(r["installed"]).read_bytes())
    assert plist["StartInterval"] == 3600
    assert "StartCalendarInterval" not in plist


def test_macos_remove_unloads_and_deletes(rec, monkeypatch, tmp_path):
    _force(monkeypatch, "darwin")
    monkeypatch.setattr(schedule.pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    schedule.install(interval="daily")                       # create
    r = schedule.install(remove=True)                        # remove
    assert "removed" in r and not pathlib.Path(r["removed"]).exists()
    assert any(c[0][:2] == ["launchctl", "unload"] for c in rec.calls)


# ── Windows: the /tr command MUST quote the executable (path-with-spaces is the common case) ──
def test_windows_quotes_executable_with_spaces(rec, monkeypatch):
    _force(monkeypatch, "win32", exe=r"C:\Program Files\Python311\python.exe")
    schedule.install(interval="hourly")
    create = next(c for c in rec.calls if c[0][:2] == ["schtasks", "/create"])
    tr = create[0][create[0].index("/tr") + 1]
    assert tr.startswith('"C:\\Program Files\\Python311\\python.exe"'), f"exe not quoted: {tr}"
    assert tr.endswith("saas sync --if-due")
    assert "/sc" in create[0] and create[0][create[0].index("/sc") + 1] == "HOURLY"


def test_windows_daily_sc(rec, monkeypatch):
    _force(monkeypatch, "win32", exe=r"C:\py\python.exe")
    schedule.install(interval="daily")
    create = next(c for c in rec.calls if c[0][:2] == ["schtasks", "/create"])
    assert create[0][create[0].index("/sc") + 1] == "DAILY"


# ── Linux: marker-tagged crontab line, idempotent install, clean removal ──
def test_linux_install_appends_marked_line(rec, monkeypatch):
    rec.crontab_out = "0 9 * * * /usr/bin/backup\n"          # a pre-existing unrelated entry
    _force(monkeypatch, "linux")
    schedule.install(interval="hourly")
    write = next(c for c in rec.calls if c[0] == ["crontab", "-"])
    written = write[1]["input"]
    assert "/usr/bin/backup" in written, "must preserve the user's existing crontab entries"
    assert schedule._MARKER in written and "0 * * * *" in written


def test_linux_install_is_idempotent(rec, monkeypatch):
    # second install over a crontab that already has our marked line must not duplicate it
    rec.crontab_out = f"0 0 * * * {' '.join(schedule._cmd())}  {schedule._MARKER}\n"
    _force(monkeypatch, "linux")
    schedule.install(interval="daily")
    written = next(c for c in rec.calls if c[0] == ["crontab", "-"])[1]["input"]
    assert written.count(schedule._MARKER) == 1, "marker line must appear exactly once"


def test_linux_remove_strips_only_our_line(rec, monkeypatch):
    rec.crontab_out = f"0 9 * * * /usr/bin/backup\n0 0 * * * x  {schedule._MARKER}\n"
    _force(monkeypatch, "linux")
    schedule.install(remove=True)
    written = next(c for c in rec.calls if c[0] == ["crontab", "-"])[1]["input"]
    assert "/usr/bin/backup" in written and schedule._MARKER not in written


# ── Unsupported platform: no crash, returns the manual command to run ──
def test_unsupported_platform_returns_manual_command(monkeypatch):
    monkeypatch.setattr(schedule.sys, "platform", "freebsd13")
    r = schedule.install()
    assert "error" in r and "saas" in r["error"] and "sync" in r["error"]
    assert schedule.main(["--daily"]) == 1, "main() signals failure on unsupported platform"
