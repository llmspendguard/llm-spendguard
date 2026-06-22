"""Installable, cross-platform scheduler. spendguard is a pip package, so it can't assume a hand-edited crontab —
`spendguard schedule` wires the OS-native scheduler to run the roll-up on a cadence (snapshot GPU every run + push
when due), and `schedule --remove` tears it down. macOS → launchd LaunchAgent; Linux → crontab; Windows → schtasks.
Idempotent + removable. Same philosophy as `install-hook`: the package owns its own setup, zero extra deps."""
import os
import sys
import subprocess
import pathlib

LABEL = "com.spendguard.sync"
_MARKER = "# spendguard-schedule (managed by `spendguard schedule`)"


def _cmd():
    # snapshot is free + idempotent and runs INSIDE saas sync (records GPU even when the push isn't due)
    return [sys.executable, "-m", "spendguard.cli", "saas", "sync", "--if-due"]


def _logpath():
    from . import config
    config.HOME.mkdir(parents=True, exist_ok=True)
    return str(config.HOME / "schedule.log")


def install(interval="hourly", remove=False):
    plat = sys.platform
    if plat == "darwin":
        return _macos(interval, remove)
    if plat.startswith("linux"):
        return _linux(interval, remove)
    if plat in ("win32", "cygwin"):
        return _windows(interval, remove)
    return {"error": f"unsupported platform {plat} — run `{' '.join(_cmd())}` from your own scheduler"}


def _macos(interval, remove):
    import plistlib
    p = pathlib.Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "unload", str(p)], capture_output=True)   # always unload an existing one first
    if remove:
        if p.exists():
            p.unlink()
        return {"removed": str(p)}
    plist = {"Label": LABEL, "ProgramArguments": _cmd(), "RunAtLoad": False,
             "StandardErrorPath": _logpath(), "StandardOutPath": _logpath()}
    if interval == "daily":
        # fire at a fixed wall-clock time (00:00), matching cron `0 0 * * *` / schtasks /sc DAILY. StartInterval
        # would drift from load time and pause across sleep; StartCalendarInterval is anchored to the clock.
        plist["StartCalendarInterval"] = {"Hour": 0, "Minute": 0}
    else:
        plist["StartInterval"] = 3600   # hourly: simplest faithful equivalent of cron `0 * * * *`
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "load", str(p)], capture_output=True, text=True)
    return {"installed": str(p), "scheduler": "launchd", "interval": interval, "every_s": secs,
            "loaded": r.returncode == 0, "err": (r.stderr.strip()[:120] or None)}


def _linux(interval, remove):
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    lines = [ln for ln in cur.splitlines() if _MARKER not in ln]
    if not remove:
        sched = "0 0 * * *" if interval == "daily" else "0 * * * *"
        lines.append(f"{sched} {' '.join(_cmd())}  {_MARKER}")
    out = ("\n".join(lines) + "\n") if lines else "\n"
    subprocess.run(["crontab", "-"], input=out, text=True)
    return {"removed": True, "scheduler": "cron"} if remove else {"installed": "crontab", "scheduler": "cron", "interval": interval}


def _windows(interval, remove):
    if remove:
        subprocess.run(["schtasks", "/delete", "/tn", "SpendguardSync", "/f"], capture_output=True)
        return {"removed": True, "scheduler": "schtasks"}
    sc = "DAILY" if interval == "daily" else "HOURLY"
    r = subprocess.run(["schtasks", "/create", "/tn", "SpendguardSync", "/sc", sc, "/tr", " ".join(_cmd()), "/f"],
                       capture_output=True, text=True)
    return {"installed": "schtasks", "scheduler": "schtasks", "interval": interval, "ok": r.returncode == 0}


def main(argv=None):
    argv = list(argv or [])
    interval = "daily" if "--daily" in argv else "hourly"
    r = install(interval=interval, remove="--remove" in argv)
    print("spendguard schedule:", r)
    if r.get("error"):
        return 1
    if not r.get("removed"):
        print(f"  → runs `saas sync --if-due` {interval}: snapshots GPU instances every run (so destroyed ones are")
        print("    captured), pushes the roll-up when due (per saas.sync_interval). Remove: `spendguard schedule --remove`.")
    return 0
