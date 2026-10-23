"""The cross-session warm-server orphan reaper must reap ONLY spendguard's own servers whose recorded owner is
dead — and must NEVER touch a live pool's servers or the user's own codex/kimi processes.

WHY THIS GUARD EXISTS. The reaper kills processes. The one unacceptable failure is a WRONGFUL kill: ending a
server a live pool is still using, or one of the user's own codex sessions. The authorship-time coding hook already
caught two real defects here (a silently-dropped unreadable process; cross-run shared state). This test pins the
selection CONTRACT so it cannot regress to a command-line guess or a bare-pid check: selection is purely
STRUCTURAL — a spendguard-stamped owner marker (pid, create_time) — and liveness is recycle-proof. Every case
asserts BOTH that the right process is reaped AND that the wrong ones are left alone; the safety direction is the
point. All fake state is built fresh per test (no shared module-level accumulator), so runs never bleed.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import warm_stdio_daemon as wd


class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


class _FakeZombieProcess(Exception):
    pass


class _FakeEnvironmentReadFailure(Exception):
    pass


class _FakeProc:
    def __init__(self, pid, environ=None, environ_exc=None):
        self.pid = pid
        self._environ = environ or {}
        self._environ_exc = environ_exc

    def environ(self):
        if self._environ_exc is not None:
            raise self._environ_exc
        return self._environ


class _FakeOwner:
    def __init__(self, create_time):
        self._create_time = create_time

    def create_time(self):
        return self._create_time


def _marker(lane, owner_pid, boot):
    env = {wd.WARM_OWNER_LANE_ENV: lane, wd.WARM_OWNER_PID_ENV: str(owner_pid)}
    if boot is not None:
        env[wd.WARM_OWNER_BOOT_ENV] = repr(boot)
    return env


# Immutable fixture identities referenced by the markers below.
LIVE_OWNER_PID, LIVE_OWNER_BOOT = 8002, 1000.5
RECYCLED_PID, OLD_BOOT, NEW_BOOT = 8003, 1000.5, 9999.9   # pid still exists but is now a DIFFERENT process
ORPHAN_CODEX, LIVE_CODEX, RECYCLED_CODEX = 7001, 7002, 7003
UNMARKED, CROSS_UID, ORPHAN_KIMI, UNREADABLE = 7004, 7005, 7006, 7007


def _scenario():
    """A fresh mixed process table + its living-owner registry for one test (never shared between tests)."""
    procs = [
        _FakeProc(ORPHAN_CODEX, _marker("codex", 8001, 500.0)),       # owner 8001 is DEAD → orphan → reap
        _FakeProc(LIVE_CODEX, _marker("codex", LIVE_OWNER_PID, LIVE_OWNER_BOOT)),  # owner alive, boot matches → keep
        _FakeProc(RECYCLED_CODEX, _marker("codex", RECYCLED_PID, OLD_BOOT)),       # pid reused, boot differs → reap
        _FakeProc(UNMARKED, {"PATH": "/usr/bin", "FOO": "bar"}),      # the user's own codex: no marker → untouched
        _FakeProc(CROSS_UID, environ_exc=_FakeAccessDenied()),        # other uid → counted, never a candidate
        _FakeProc(ORPHAN_KIMI, _marker("kimi", 8009, 500.0)),         # a DIFFERENT lane's orphan
        _FakeProc(UNREADABLE, environ_exc=_FakeEnvironmentReadFailure()),  # same-uid gap → visible, never reaped
    ]
    living_owners = {LIVE_OWNER_PID: _FakeOwner(LIVE_OWNER_BOOT), RECYCLED_PID: _FakeOwner(NEW_BOOT)}
    return procs, living_owners


def _install(procs, living_owners):
    """Point warm_stdio_daemon at fake process machinery and return this test's own `killed` signal log plus a
    `restore` callable that puts the real psutil / os.killpg back. No real process is ever signalled; os.killpg is
    replaced with a recorder that simulates the group dying on the first real signal (signal 0 is the module's
    liveness probe). The fakes are PER-TEST and MUST be restored in a finally — a global monkeypatch that outlives
    the test would make a later test observe this synthetic process table (the exact leak the coding hook flagged)."""
    killed, dead = [], set()
    real_psutil, real_killpg = wd.psutil, wd.os.killpg

    def fake_killpg(pgid, sig):
        if sig == 0:
            if pgid in dead:
                raise ProcessLookupError(pgid)
            return
        killed.append((pgid, sig))
        dead.add(pgid)

    def fake_process_iter(attrs=None):
        return list(procs)

    def fake_process(pid):
        owner = living_owners.get(pid)
        if owner is None:
            raise _FakeNoSuchProcess(pid)
        return owner

    fake_psutil = type("_FakePsutil", (), {
        "NoSuchProcess": _FakeNoSuchProcess, "AccessDenied": _FakeAccessDenied,
        "ZombieProcess": _FakeZombieProcess,
        "process_iter": staticmethod(fake_process_iter), "Process": staticmethod(fake_process)})
    wd.psutil = fake_psutil
    wd.os.killpg = fake_killpg

    def restore():
        wd.psutil = real_psutil
        wd.os.killpg = real_killpg

    return killed, restore


def test_reaps_exactly_the_marked_orphans_of_the_named_lane():
    killed, restore = _install(*_scenario())
    try:
        report = wd.reap_orphaned_warm_servers(lane="codex", execute=True)
        reaped = set(report["reaped"])
        assert ORPHAN_CODEX in reaped, report
        assert RECYCLED_CODEX in reaped, report
        assert LIVE_CODEX not in reaped and LIVE_CODEX not in report["orphans"], "a LIVE-owner server was reaped"
        assert UNMARKED not in reaped and UNMARKED not in report["orphans"], "an UNMARKED user process was reaped"
        assert ORPHAN_KIMI not in reaped and ORPHAN_KIMI not in report["orphans"], "a different lane's orphan was reaped"
        assert {pid for pid, _ in killed} == {ORPHAN_CODEX, RECYCLED_CODEX}, killed
        assert report["cross_uid_skipped"] == 1
        assert not any(str(CROSS_UID) in entry for entry in report["unreadable"]), report["unreadable"]
        assert any(str(UNREADABLE) in entry for entry in report["unreadable"]), report["unreadable"]
        assert CROSS_UID not in reaped and UNREADABLE not in reaped
    finally:
        restore()


def test_lane_none_reaps_every_lanes_orphans():
    _, restore = _install(*_scenario())
    try:
        report = wd.reap_orphaned_warm_servers(lane=None, execute=True)
        reaped = set(report["reaped"])
        assert {ORPHAN_CODEX, RECYCLED_CODEX} <= reaped, report
        assert ORPHAN_KIMI in reaped, report
        assert LIVE_CODEX not in reaped and UNMARKED not in reaped, "lane=None touched a live or unmarked server"
    finally:
        restore()


def test_dry_run_lists_orphans_but_signals_nothing():
    killed, restore = _install(*_scenario())
    try:
        report = wd.reap_orphaned_warm_servers(lane="codex", execute=False)
        assert {ORPHAN_CODEX, RECYCLED_CODEX} == set(report["orphans"]), report
        assert report["reaped"] == []
        assert killed == [], killed
    finally:
        restore()


def test_owner_death_is_recycle_proof_and_never_conflates_unknown_with_dead():
    _, restore = _install(*_scenario())
    try:
        # Provably dead ONLY in the two provable cases: a gone pid, or a pid recycled by a different process.
        assert wd._warm_owner_provably_dead(8001, repr(500.0)) is True             # gone pid → reap
        assert wd._warm_owner_provably_dead(RECYCLED_PID, repr(OLD_BOOT)) is True   # pid reused (create_time differs)
        # Not provably dead ⇒ never reaped: alive+matching, or any UNKNOWN (missing / malformed identity).
        assert wd._warm_owner_provably_dead(LIVE_OWNER_PID, repr(LIVE_OWNER_BOOT)) is False  # alive, matching identity
        assert wd._warm_owner_provably_dead(LIVE_OWNER_PID, None) is False          # pid exists, no recorded identity
        assert wd._warm_owner_provably_dead(RECYCLED_PID, "unreadable") is False    # malformed boot ⇒ unknown, not dead
        assert wd._warm_owner_provably_dead(LIVE_OWNER_PID, "unreadable") is False  # malformed boot ⇒ unknown, not dead
    finally:
        restore()


if __name__ == "__main__":
    cases = [fn for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    for case in cases:
        case()
        print(f"  [OK] {case.__name__}")
    print(f"\nOK test_warm_server_orphan_reaper: {len(cases)} test(s) passed")
