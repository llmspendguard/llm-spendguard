"""doctor BINDS an unpriced advisor cap ONLY when the advisor is the active default picker (magnitude-aware).

An unpriced advisor model means the meta ESTIMATE/cap cannot be computed ("a broken cap"). Whether that genuinely
blocks depends on magnitude — and the magnitude is NOT a dollar figure (the model being unpriced is exactly why no
dollar estimate exists). It is whether the advisor is on the HOT PATH: adapters._default_reasoning_is_best_value()
(env SPENDGUARD_DEFAULT_REASONING, else config advisor.default_reasoning). This guards:
  • advisor UNPRICED + advisor NOT the active default picker -> LATENT: doctor prints the red and stays exit 0;
  • advisor UNPRICED + advisor IS the active default picker -> every unlabelled call routes through the uncomputable
    meta-cap -> doctor EXITS NON-ZERO, naming the fix and the SPENDGUARD_ALLOW_UNPRICED_ADVISOR escape hatch;
  • the escape hatch (SPENDGUARD_ALLOW_UNPRICED_ADVISOR=1) waives the bind (exit 0) but STILL prints the red.

Offline, isolated SPENDGUARD_HOME. Every other doctor bind source is stubbed benign so only the advisor cap varies.
Mirrors tests/test_doctor_binds_stale_editable_install.py."""
import os
import sys
import io
import tempfile
import contextlib
import pathlib

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-advisor-cap-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_ALLOW_UNPRICED_ADVISOR", None)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check  # noqa: E402

from spendguard import config, fleet  # noqa: E402
from spendguard.manage.admission import gate  # noqa: E402
from spendguard import metadata_audit as _ma  # noqa: E402
from spendguard.generate.execution import adapters  # noqa: E402

config.HOME = pathlib.Path(os.environ["SPENDGUARD_HOME"])

fails = []
UNPRICED_PROBLEMS = ["advisor.model='fake-unpriced': PriceUnknownError(\"no catalog price\")"]


def _run_doctor():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gate._cli("doctor")
    return rc, buf.getvalue()


# Capture originals; stub every OTHER doctor bind source benign so only the advisor cap can bind.
_orig = {
    "backbone_health": _ma.backbone_health,
    "install": gate.install,
    "consumer_install_drift": fleet.consumer_install_drift,
    "registration_status": fleet.enforcing_interpreter_registration_status,
    "stale_editable_install": gate._stale_editable_install,
    "validate_advisor": gate.validate_advisor,
    "default_reasoning_is_best_value": adapters._default_reasoning_is_best_value,
}
try:
    _ma.backbone_health = lambda: {"cache": {"present": True, "models": 1, "age_days": 0}, "ok": True, "drift": []}
    gate.install = lambda *a, **k: None
    fleet.consumer_install_drift = lambda: []
    fleet.enforcing_interpreter_registration_status = lambda: {
        "registered": True, "error": None, "python_executable": sys.executable}
    gate._stale_editable_install = lambda: (False, "9.9.9", "9.9.9")
    gate.validate_advisor = lambda: list(UNPRICED_PROBLEMS)

    # (a) UNPRICED + advisor NOT the active default picker -> LATENT, exit 0.
    adapters._default_reasoning_is_best_value = lambda: False
    rc_a, out_a = _run_doctor()
    record_script_check(fails, "unpriced advisor + not active default -> doctor exits 0 (latent)", rc_a == 0,
                        extra=f"rc={rc_a}")
    record_script_check(fails, "latent case still prints the UNPRICED red clearly, named latent",
                        "UNPRICED" in out_a and "latent" in out_a, extra=out_a)

    # (b) UNPRICED + advisor IS the active default picker -> BIND, names the escape hatch.
    adapters._default_reasoning_is_best_value = lambda: True
    rc_b, out_b = _run_doctor()
    record_script_check(fails, "unpriced advisor + active default picker -> doctor exits non-zero (binds)", rc_b == 1,
                        extra=f"rc={rc_b}")
    record_script_check(fails, "bind message names the SPENDGUARD_ALLOW_UNPRICED_ADVISOR escape hatch",
                        gate.ALLOW_UNPRICED_ADVISOR_ENV in out_b, extra=out_b)

    # (c) same as (b) but the escape hatch is set -> waived (exit 0) and the red still prints.
    os.environ["SPENDGUARD_ALLOW_UNPRICED_ADVISOR"] = "1"
    try:
        rc_c, out_c = _run_doctor()
    finally:
        os.environ.pop("SPENDGUARD_ALLOW_UNPRICED_ADVISOR", None)
    record_script_check(fails, "escape hatch waives the bind -> doctor exits 0", rc_c == 0, extra=f"rc={rc_c}")
    record_script_check(fails, "waived case still prints the UNPRICED red and says it was waived",
                        "UNPRICED" in out_c and "waived" in out_c, extra=out_c)
finally:
    _ma.backbone_health = _orig["backbone_health"]
    gate.install = _orig["install"]
    fleet.consumer_install_drift = _orig["consumer_install_drift"]
    fleet.enforcing_interpreter_registration_status = _orig["registration_status"]
    gate._stale_editable_install = _orig["stale_editable_install"]
    gate.validate_advisor = _orig["validate_advisor"]
    adapters._default_reasoning_is_best_value = _orig["default_reasoning_is_best_value"]

print(f"\n{'OK' if not fails else 'FAIL'} test_doctor_binds_advisor_unpriced_cap: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
