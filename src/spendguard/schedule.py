"""Installable, cross-platform scheduler. spendguard is a pip package, so it can't assume a hand-edited crontab —
`spendguard schedule` wires the OS-native scheduler to run the roll-up on a cadence (snapshot GPU every run + push
when due), and `schedule --remove` tears it down. macOS → launchd LaunchAgent; Linux → crontab; Windows → schtasks.
Idempotent + removable. Same philosophy as `install-hook`: the package owns its own setup, zero extra deps."""
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


def install_schedule(interval="hourly", remove=False):
    plat = sys.platform
    if plat == "darwin":
        return _macos(interval, remove)
    if plat.startswith("linux"):
        return _linux(interval, remove)
    if plat in ("win32", "cygwin"):
        return _windows_schtasks(interval, remove)
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
    when = "00:00 daily" if interval == "daily" else "every 3600s"
    return {"installed": str(p), "scheduler": "launchd", "interval": interval, "when": when,
            "loaded": r.returncode == 0, "err": (r.stderr.strip()[:120] or None)}


def _linux(interval, remove):
    # `crontab -l` returns non-zero + empty stdout in TWO very different cases: the user has NO crontab (fine —
    # start empty) and a real failure (permissions, cron daemon down). Treating the second as "no crontab" and
    # then running `crontab -` would WIPE the user's existing jobs. Trust stdout only on exit 0 or the documented
    # "no crontab" message; any other failure aborts rather than clobber the crontab.
    try:
        _r = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        # No `crontab` binary at all (a minimal container / cron not installed). Report it honestly instead of
        # crashing with a traceback — nothing was scheduled, and the caller can choose another mechanism.
        return {"error": "no `crontab` binary found — cron is not installed; nothing scheduled", "scheduler": "cron"}
    if _r.returncode != 0 and "no crontab" not in (_r.stderr or "").lower():
        return {"error": f"crontab -l failed ({(_r.stderr or '').strip()[:120]}) — refusing to rewrite the "
                         f"crontab and risk wiping existing jobs", "scheduler": "cron"}
    cur = _r.stdout
    lines = [ln for ln in cur.splitlines() if _MARKER not in ln]
    if not remove:
        sched = "0 0 * * *" if interval == "daily" else "0 * * * *"
        lines.append(f"{sched} {' '.join(_cmd())}  {_MARKER}")
    out = ("\n".join(lines) + "\n") if lines else "\n"
    # The WRITE can fail too (cron daemon down, permissions). Its return code was ignored, so a failed install
    # still reported success — a schedule the user believes exists but doesn't. Check it.
    w = subprocess.run(["crontab", "-"], input=out, text=True, capture_output=True)
    if w.returncode != 0:
        return {"error": f"crontab write failed ({(w.stderr or '').strip()[:120]}) — schedule NOT installed",
                "scheduler": "cron"}
    return {"removed": True, "scheduler": "cron"} if remove else {"installed": "crontab", "scheduler": "cron", "interval": interval}


def _windows_schtasks(interval, remove):
    if remove:
        subprocess.run(["schtasks", "/delete", "/tn", "SpendguardSync", "/f"], capture_output=True)
        return {"removed": True, "scheduler": "schtasks"}
    sc = "DAILY" if interval == "daily" else "HOURLY"
    # Task Scheduler re-splits the /tr string on spaces, so the python path (commonly "C:\\Program Files\\...")
    # MUST be quoted or the task fails to start. Quote the executable; the rest of the argv has no spaces.
    exe, *rest = _cmd()
    tr = " ".join([f'"{exe}"', *rest])
    r = subprocess.run(["schtasks", "/create", "/tn", "SpendguardSync", "/sc", sc, "/tr", tr, "/f"],
                       capture_output=True, text=True)
    return {"installed": "schtasks", "scheduler": "schtasks", "interval": interval, "ok": r.returncode == 0}


def main(argv=None):
    argv = list(argv or [])
    interval = "daily" if "--daily" in argv else "hourly"
    r = install_schedule(interval=interval, remove="--remove" in argv)
    print("spendguard schedule:", r)
    if r.get("error"):
        return 1
    if not r.get("removed"):
        print(f"  → runs `saas sync --if-due` {interval}: snapshots GPU instances every run (so destroyed ones are")
        print("    captured), pushes the roll-up when due (per saas.sync_interval). Remove: `spendguard schedule --remove`.")
    return 0
