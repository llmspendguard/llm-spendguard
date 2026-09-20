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
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-release-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import release   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── a controllable fake git: HEAD sha, dirty flag, and ref resolution are all dials ──
STATE = {"head": "a" * 40, "dirty": False, "known": {"a" * 40, "b" * 40}}


def _fake_git(*args):
    if args[:2] == ("rev-parse", "HEAD"):
        return STATE["head"]
    if args[:1] == ("rev-parse",):                    # resolve/validate an arbitrary ref → full sha or None
        ref = args[1]
        return ref if ref in STATE["known"] else None
    if args[:2] == ("status", "--porcelain"):
        return "M src/spendguard/x.py" if STATE["dirty"] else ""
    if args[:1] == ("describe",):
        return "v0.10.0-1-g" + STATE["head"][:7]
    return None


release._git_src = _fake_git


def _reset_served():
    release.served_sha.cache_clear()                  # served identity is frozen per-process; clear between setups


print("-- served_sha() is the frozen identity of THIS process --")
_reset_served()
s = release.served_sha()
ck("served_sha reports HEAD (sha/short/describe/dirty)",
   s and s["sha"] == "a" * 40 and s["short"] == "a" * 7 and s["dirty"] is False)
STATE["head"] = "c" * 40                              # HEAD 'moves' but the process identity must NOT
ck("served_sha is FROZEN (memoised) — it does not follow a later HEAD change", release.served_sha()["sha"] == "a" * 40)
STATE["head"] = "a" * 40

print("\n-- no pointer yet → not stale, and status says so --")
ck("green_pointer() is None before any deploy", release.green_pointer() is None)
ck("stale_vs_green() is False with no pointer (never respawn on an unknown)", release.stale_vs_green() is False)
ck("release_status: served known, green None, not stale", release.release_status()["green"] is None
   and release.release_status()["stale"] is False)

print("\n-- promote_release REFUSES a dirty tree and an unresolvable sha --")
STATE["dirty"] = True
_dirty_refused = False
try:
    release.promote_release()
except ValueError:
    _dirty_refused = True
ck("a dirty working tree is refused (the pointer names only committed code)", _dirty_refused)
STATE["dirty"] = False
_badref_refused = False
try:
    release.promote_release("deadbeef")               # not in STATE['known'] → git cannot resolve
except ValueError:
    _badref_refused = True
ck("a sha git cannot resolve is refused", _badref_refused)

print("\n-- promote to the SERVED sha → up to date; then advance → stale --")
rec = release.promote_release(gate="chunked_suite green")
ck("promote wrote the pointer at the served sha", rec["sha"] == "a" * 40 and release.green_pointer()["sha"] == "a" * 40)
ck("gate result is recorded on the pointer", release.green_pointer().get("gate") == "chunked_suite green")
ck("served == green → NOT stale", release.stale_vs_green() is False)
ck("release_status note reads up-to-date", "up to date" in release.release_status()["note"])

# a newer commit is deployed (HEAD moves to a KNOWN newer sha, tree clean) → the frozen process is now behind it
STATE["head"] = "b" * 40
rec2 = release.promote_release()
ck("a second promote advances the pointer to the new sha", rec2["sha"] == "b" * 40 and release.green_pointer()["sha"] == "b" * 40)
ck("update_json left a `~` backup of the previous pointer (non-destructive advance)",
   pathlib.Path(str(release.pointer_path()) + "~").exists())
ck("served (a…) now BEHIND green (b…) → STALE", release.stale_vs_green() is True)
ck("release_status note flags STALE with both short shas", "STALE" in release.release_status()["note"]
   and release.release_status()["stale"] is True)

print("\n-- should_respawn() is loop-safe: exit ONLY when a fresh process would land on green --")
# live HEAD == green (b) and this frozen process serves a → a respawn WOULD reach green → respawn.
ck("should_respawn TRUE when live HEAD == green and the process is behind it", release.should_respawn() is True)
STATE["known"].add("d" * 40)
STATE["head"] = "d" * 40                              # HEAD moved PAST green (d) without a deploy; green stays b
ck("should_respawn FALSE when HEAD != green (a respawn would serve d, not green — no exit loop)",
   release.should_respawn() is False)
ck("... yet stale_vs_green stays True (the process IS behind green; it just cannot fix it by respawning)",
   release.stale_vs_green() is True)
STATE["head"] = "b" * 40                              # restore for any later assertions

print("\n-- unknown version (not a git tree) → served None, never stale --")
release._git_src = lambda *a: None
_reset_served()
ck("served_sha() is None when git is unavailable", release.served_sha() is None)
ck("stale_vs_green() is False when the served version is unknown", release.stale_vs_green() is False)
ck("release_status note says version unknown, server keeps running", "unknown" in release.release_status()["note"])

print(f"\n{'[FAIL]' if fails else 'OK'} test_release_green_pointer: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
