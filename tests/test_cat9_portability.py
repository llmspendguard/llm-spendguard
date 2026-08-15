"""Cat-9 portability — path handling that assumed POSIX separators now works cross-platform:

  * history._under_git — the .git exclusion used a POSIX-only '/.git/' SUBSTRING, so on Windows (where glob /
    os.walk yield '\\'-separated paths) .git internals were scanned as if they were batch artifacts. Now a
    portable '.git' path-COMPONENT check, which also won't misfire on a '.github' sibling.

(chat._decrypt_cookies now also copies the -wal/-shm sidecars so a WAL-mode Cookies snapshot is complete; that
path is macOS-only + Keychain-bound, so it is compile-checked in the suite rather than executed here.)
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cat9-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import history          # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


ck("POSIX path under .git is excluded", history._under_git("/home/u/repo/.git/objects/ab/cd") is True)
ck("WINDOWS path under .git is excluded (tested on this host)",
   history._under_git("C:\\Users\\u\\repo\\.git\\config") is True)
ck("a .github sibling is NOT treated as .git", history._under_git("/home/u/repo/.github/workflows/ci.yml") is False)
ck("an ordinary source path is not under .git", history._under_git("/home/u/repo/src/main.py") is False)
ck("a relative windows path under .git is excluded", history._under_git("repo\\.git\\HEAD") is True)
ck("'.git' as the final component (a gitfile) is excluded", history._under_git("/repo/sub/.git") is True)

print(f"\n{'[FAIL]' if fails else 'OK'} test_cat9_portability: {fails} failure(s)")
sys.exit(1 if fails else 0)
