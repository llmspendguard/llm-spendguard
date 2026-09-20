"""GUARD — the deploy/release CLI wires the green pointer correctly. `deploy` REFUSES a dirty tree (the pointer
must name only COMMITTED code) and otherwise promotes through release.promote_release; `release` renders
served-vs-green + staleness. The load-bearing logic lives in release.py (unit-tested in
test_release_green_pointer); this pins the CLI GLUE so the deploy path cannot silently stop refusing a dirty tree
or stop promoting.

Hermetic: isolated home; release.{_tracked_dirty,promote_release,release_status} stubbed; --no-gate so the real
suite never runs; stdout/stderr captured. No network, no ledger, no git writes."""
import contextlib
import io
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-deploycli-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import cli, release   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def _run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = cli._dispatch(argv)
    return rc, buf.getvalue()


_promoted = {"n": 0, "gate": None}


def _fake_promote(**kw):
    _promoted["n"] += 1
    _promoted["gate"] = kw.get("gate")
    return {"short": "abc1234", "describe": "v0.10.0-1-gabc1234", "gate": kw.get("gate", "")}


release.promote_release = _fake_promote

print("-- deploy REFUSES a dirty tree and does not promote --")
release._tracked_dirty = lambda: True
rc, out = _run(["deploy", "--no-gate"])
ck("deploy on a DIRTY tree exits non-zero (refuses)", rc == 2)
ck("... and does NOT promote", _promoted["n"] == 0)
ck("... telling the user to commit first", "uncommitted" in out.lower() or "commit first" in out.lower())

print("\n-- deploy --no-gate on a CLEAN tree promotes exactly once --")
release._tracked_dirty = lambda: False
rc, out = _run(["deploy", "--no-gate"])
ck("deploy --no-gate on a clean tree exits 0", rc == 0)
ck("... promotes exactly once, through release.promote_release", _promoted["n"] == 1)
ck("... and records that the gate was skipped (--no-gate), not faked as green", "no-gate" in (_promoted["gate"] or ""))
ck("... reporting the promoted commit", "promoted" in out.lower())

print("\n-- release renders served / green / staleness --")
release.release_status = lambda: {"served": {"short": "aaaaaaa", "describe": "v0.10.0-9-gaaaaaaa"},
                                  "green": {"short": "bbbbbbb", "describe": "v0.10.0-9-gbbbbbbb"},
                                  "stale": True, "served_dirty": False,
                                  "pointer_path": "/x/current_release.json", "note": "STALE: a… behind b…"}
rc, out = _run(["release"])
ck("release exits 0", rc == 0)
ck("release shows served, green, and a STALE status", "aaaaaaa" in out and "bbbbbbb" in out and "STALE" in out)

print(f"\n{'[FAIL]' if fails else 'OK'} test_deploy_release_cli: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
