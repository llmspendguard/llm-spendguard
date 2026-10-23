"""#2 FRESHNESS — `spendguard doctor` BINDS the stale-editable-install skew instead of only printing a yellow note.

A stale editable install (running code != this venv's install-time metadata) is dangerous because its frozen DEP SET
is also stale: a dependency a newer version added is NOT installed, so a lazy import fails at runtime (the "reviewer
unavailable" / ImportError class). This guards:
  • _stale_editable_install() DETECTS code!=metadata (and NEVER flags on an unreadable metadata read — unknown != skew);
  • `doctor` EXITS NON-ZERO on the skew, so CI / an install-verify / a `doctor && <next>` chain catches the broken
    install instead of proceeding — while `status` stays exit-0 (a casual glance);
  • the remediation is the CORRECT one — `pip install -e .` WITH deps (NOT --no-deps, which leaves the missing deps).

Offline, isolated SPENDGUARD_HOME (the metadata-sync auto-heal + SDK patch are stubbed so the full doctor runs offline)."""
import os
import sys
import io
import tempfile
import contextlib
import pathlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-freshness-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
from spendguard import config, fleet, gate  # noqa: E402

config.HOME = pathlib.Path(os.environ["SPENDGUARD_HOME"])

fails = []
PINNED_CONSUMER_VENV = "/isolated/consumer"
PINNED_CONSUMER_VERSION = "0.14.0"
RESOLVED_REPOSITORY_PATH = "/resolved/repo"


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ── A. _stale_editable_install(): code != metadata -> STALE; == -> fresh; unreadable -> NOT a skew ───────────────────
import importlib.metadata as _im  # noqa: E402
_orig_ver = _im.version
try:
    _im.version = lambda n: "0.0.1-ancient"
    stale, code, meta = gate._stale_editable_install()
    ck("code != metadata -> STALE", stale is True and meta == "0.0.1-ancient" and code == spendguard.__version__,
       extra=f"stale={stale} code={code} meta={meta}")
    _im.version = lambda n: spendguard.__version__
    stale2, _, _ = gate._stale_editable_install()
    ck("code == metadata -> fresh (not stale)", stale2 is False)

    def _raise(n):
        raise _im.PackageNotFoundError(n)
    _im.version = _raise
    stale3, _, meta3 = gate._stale_editable_install()
    ck("metadata UNREADABLE -> NOT a skew (meta None; never flag on a failed read)", stale3 is False and meta3 is None)
finally:
    _im.version = _orig_ver

# ── B. doctor EXITS NON-ZERO on a skew (binds); status does not; a fresh install exits 0 ─────────────────────────────
from spendguard import metadata_audit as _ma  # noqa: E402
_o_sei, _o_install, _o_bh = gate._stale_editable_install, gate.install, _ma.backbone_health
_o_consumer_drift = fleet.consumer_install_drift
_o_registration_status = fleet.enforcing_interpreter_registration_status
try:
    _ma.backbone_health = lambda: {"cache": {"present": True, "models": 1, "age_days": 0}, "ok": True, "drift": []}
    gate.install = lambda *a, **k: None                    # don't patch the real SDKs in the test process
    fleet.consumer_install_drift = lambda: []
    fleet.enforcing_interpreter_registration_status = lambda: {
        "registered": True, "error": None, "python_executable": sys.executable}
    gate._stale_editable_install = lambda: (True, "9.9.9", "0.0.1")   # simulate a stale editable install

    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf):
        rc = gate._cli("doctor")
    out = _buf.getvalue()
    ck("doctor EXITS NON-ZERO on a stale editable install", rc == 1, extra=f"rc={rc}")
    ck("doctor names it STALE EDITABLE INSTALL", "STALE EDITABLE INSTALL" in out)
    ck("doctor gives the CORRECT remediation — `pip install -e .` WITH deps, NOT --no-deps",
       "pip install -e ." in out and "NOT --no-deps" in out, extra=out)

    with contextlib.redirect_stdout(io.StringIO()):
        rc_status = gate._cli("status")
    ck("status does NOT bind (exit 0) even on a skew — only the diagnostic doctor binds", rc_status == 0,
       extra=f"rc_status={rc_status}")

    gate._stale_editable_install = lambda: (False, "9.9.9", "9.9.9")   # a FRESH install (code == metadata)
    with contextlib.redirect_stdout(_fresh_doctor_output := io.StringIO()):
        rc_fresh = gate._cli("doctor")
    ck("doctor on a FRESH install exits 0", rc_fresh == 0,
       extra=f"rc_fresh={rc_fresh}; output={_fresh_doctor_output.getvalue()}")

    fleet.consumer_install_drift = lambda: [{
        "venv": PINNED_CONSUMER_VENV, "metadata_version": PINNED_CONSUMER_VERSION,
        "remediation": (f"{PINNED_CONSUMER_VENV}/bin/python -m pip install -e "
                        f"{RESOLVED_REPOSITORY_PATH}")}]
    with contextlib.redirect_stdout(_buf := io.StringIO()):
        rc_pinned = gate._cli("doctor")
    pinned_out = _buf.getvalue()
    ck("doctor binds a pinned consumer install", rc_pinned == 1)
    ck("doctor names venv, version skew, and exact editable remediation",
       PINNED_CONSUMER_VENV in pinned_out and f"install pinned v{PINNED_CONSUMER_VERSION}" in pinned_out
       and f"{PINNED_CONSUMER_VENV}/bin/python -m pip install -e {RESOLVED_REPOSITORY_PATH}" in pinned_out)
finally:
    gate._stale_editable_install, gate.install, _ma.backbone_health = _o_sei, _o_install, _o_bh
    fleet.consumer_install_drift = _o_consumer_drift
    fleet.enforcing_interpreter_registration_status = _o_registration_status

print(f"\n{'OK' if not fails else 'FAIL'} test_doctor_binds_stale_editable_install: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
