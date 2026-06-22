"""Offline tests for the installable scheduler (no real launchd/cron/schtasks touched).

`spendguard schedule` is a pip package wiring the OS-native scheduler, so the three backends must each emit a
correct, idempotent, removable spec. We mock subprocess + the home dir and assert the spec handed to launchd /
crontab / schtasks — including the Windows /tr quoting (a python path with spaces must not break the task).

Script-style (ck + sys.exit), like the rest of the suite: test_runner.py runs it as `python tests/test_schedule.py`
in an isolated SPENDGUARD_HOME, so pytest fixtures are NOT available — we patch + restore by hand."""
import os, sys, types, plistlib, pathlib, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-sched-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import schedule, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


class _Recorder:
    """Stand-in for subprocess.run: records every call, returns rc=0. `crontab -l` yields `crontab_out`."""
    def __init__(self, crontab_out=""):
        self.calls, self.crontab_out = [], crontab_out

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        out = self.crontab_out if list(args[:2]) == ["crontab", "-l"] else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


class _Patches:
    """Minimal save/restore (no monkeypatch fixture in script-style). Patches subprocess + platform + home."""
    def __init__(self, platform, exe="/usr/bin/python3", crontab_out="", home=None):
        self._undo = []
        self.rec = _Recorder(crontab_out)
        self._set(schedule.subprocess, "run", self.rec)
        self._set(schedule.sys, "platform", platform)
        self._set(schedule.sys, "executable", exe)
        self._set(config, "HOME", pathlib.Path(tempfile.mkdtemp(prefix="sg-home-")) / ".spendguard")
        if home is not None:
            self._set(schedule.pathlib.Path, "home", classmethod(lambda cls, _h=home: _h))

    def _set(self, obj, attr, val):
        self._undo.append((obj, attr, getattr(obj, attr)))
        setattr(obj, attr, val)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        for obj, attr, old in reversed(self._undo):
            setattr(obj, attr, old)


def _create(rec):
    return next(c for c in rec.calls if list(c[0][:2]) == ["schtasks", "/create"])

def _crontab_write(rec):
    return next(c for c in rec.calls if list(c[0]) == ["crontab", "-"])[1]["input"]


# ── macOS: daily = wall-clock StartCalendarInterval (no drift); hourly = StartInterval ──
home = pathlib.Path(tempfile.mkdtemp(prefix="sg-mac-"))
with _Patches("darwin", home=home) as p:
    r = schedule.install(interval="daily")
    plist = plistlib.loads(pathlib.Path(r["installed"]).read_bytes())
    ck("macOS daily → StartCalendarInterval 00:00 (clock-anchored, no drift)",
       plist.get("StartCalendarInterval") == {"Hour": 0, "Minute": 0} and "StartInterval" not in plist)
    ck("macOS → ProgramArguments end with the saas sync --if-due command",
       plist["ProgramArguments"][-3:] == ["saas", "sync", "--if-due"] and r["scheduler"] == "launchd")
with _Patches("darwin", home=home) as p:
    plist = plistlib.loads(pathlib.Path(schedule.install(interval="hourly")["installed"]).read_bytes())
    ck("macOS hourly → StartInterval 3600 (no StartCalendarInterval)",
       plist.get("StartInterval") == 3600 and "StartCalendarInterval" not in plist)
with _Patches("darwin", home=home) as p:
    schedule.install(interval="daily")
    r = schedule.install(remove=True)
    ck("macOS remove → unloads + deletes the plist",
       "removed" in r and not pathlib.Path(r["removed"]).exists()
       and any(list(c[0][:2]) == ["launchctl", "unload"] for c in p.rec.calls))

# ── Windows: the /tr command MUST quote the executable (path-with-spaces is the common case) ──
with _Patches("win32", exe=r"C:\Program Files\Python311\python.exe") as p:
    schedule.install(interval="hourly")
    cmd = _create(p.rec)[0]
    tr = cmd[cmd.index("/tr") + 1]
    ck("Windows /tr quotes a python path WITH SPACES (else schtasks splits it and the task fails)",
       tr.startswith('"C:\\Program Files\\Python311\\python.exe"') and tr.endswith("saas sync --if-due"))
    ck("Windows hourly → /sc HOURLY", cmd[cmd.index("/sc") + 1] == "HOURLY")
with _Patches("win32", exe=r"C:\py\python.exe") as p:
    schedule.install(interval="daily")
    cmd = _create(p.rec)[0]
    ck("Windows daily → /sc DAILY", cmd[cmd.index("/sc") + 1] == "DAILY")

# ── Linux: marker-tagged crontab line, idempotent install, clean removal that spares other entries ──
with _Patches("linux", crontab_out="0 9 * * * /usr/bin/backup\n") as p:
    schedule.install(interval="hourly")
    w = _crontab_write(p.rec)
    ck("Linux install → appends our marked line + PRESERVES the user's existing crontab",
       "/usr/bin/backup" in w and schedule._MARKER in w and "0 * * * *" in w)
with _Patches("linux", crontab_out=f"0 0 * * * {' '.join(schedule._cmd())}  {schedule._MARKER}\n") as p:
    schedule.install(interval="daily")
    ck("Linux install is idempotent → our marker appears exactly once (no duplicate)",
       _crontab_write(p.rec).count(schedule._MARKER) == 1)
with _Patches("linux", crontab_out=f"0 9 * * * /usr/bin/backup\n0 0 * * * x  {schedule._MARKER}\n") as p:
    schedule.install(remove=True)
    w = _crontab_write(p.rec)
    ck("Linux remove → strips ONLY our line, keeps the user's other entries",
       "/usr/bin/backup" in w and schedule._MARKER not in w)

# ── Unsupported platform: no crash, returns the manual command, main() signals failure ──
with _Patches("freebsd13") as p:
    r = schedule.install()
    ck("unsupported platform → error carries the manual command to run by hand",
       "error" in r and "saas" in r["error"] and "sync" in r["error"])
    ck("main() returns 1 on unsupported platform", schedule.main(["--daily"]) == 1)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"schedule: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
