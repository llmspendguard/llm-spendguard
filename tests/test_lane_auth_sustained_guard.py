"""F2 guard: an auth-status refresh-race blip is recorded but only a sustained unresolved logout notifies."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-auth-sustained-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import reliability  # noqa: E402


def main():
    failures = []
    notifications = []
    original_notify = reliability._notify_macos
    reliability._notify_macos = lambda *args: notifications.append(args)
    db = reliability._health_db()

    def check(label, condition):
        print(f"  [{'OK' if condition else 'FAIL'}] {label}")
        if not condition:
            failures.append(label)

    try:
        lane = "auth-sustained-guard"
        reliability.note_lane_auth_down(lane, "login-command")
        row = db.execute("SELECT reachable, source FROM lane_health WHERE resource=?", (lane,)).fetchone()
        check("a lone auth-down is durably recorded as event-auth", row == (0, "event-auth"))
        check("a lone auth-down does not notify", notifications == [])

        reliability.note_lane_auth_down(lane, "login-command")
        check("a second unresolved auth-down notifies", len(notifications) == 1)

        db.execute("DELETE FROM lane_health WHERE resource=?", (lane,))
        db.commit()
        notifications.clear()
        reliability.note_lane_auth_down(lane, "login-command")
        reliability.note_lane_ok(lane)
        reliability.note_lane_auth_down(lane, "login-command")
        check("a success clears the unresolved state, so the next lone miss does not notify", notifications == [])

        authoritative = "auth-authoritative-guard"
        reliability.note_lane_auth_down(authoritative, "login-command", sustained_only=False)
        check("an authoritative caller can notify on first detection", len(notifications) == 1)
    finally:
        reliability._notify_macos = original_notify

    print(f"\n{'[FAIL]' if failures else 'OK'} test_lane_auth_sustained_guard: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
