"""The de-identification boundary must fail toward WITHHOLDING, never toward sending.

FOUND BY WAVE 3, confirmed by both validators. redact() is the single chokepoint every egress path goes
through before text leaves this machine, and it read:

    try:
        out = _floor(out, entities)
    except Exception:
        pass                       # <- `out` is still the ORIGINAL, un-redacted text

so any crash inside the redactor sent the raw payload — emails, SSNs, keys, whatever was in it — straight
to a cloud LLM. Its own docstring, four lines above, promises the opposite:

    "Fails open toward privacy: never raises; on any error the deterministic floor still applies"

It failed open toward EXPOSURE. That is the one direction a pre-egress boundary must never fail in, and
nothing about it was visible: the call succeeded, the text went out, and the only difference from a
correct run was that the PII was still in it.

WHY WITHHOLDING IS THE ONLY OPTION. If the floor did not run, NOTHING in that string has been checked —
there is no partially-safe subset to send. The caller gets a marker and a loud warning, which they can
see and recover from. An unnoticed disclosure cannot be recovered from at all.

THE VERIFICATION MATTERS AS MUCH AS THE FIX. The first attempt at this fix silently did not apply — the
edit script raised before writing — and the run that proved it printed `PII leaked: True`. A fix reported
without being exercised is a claim, not a change.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-deid-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import deid                                                 # noqa: E402

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


SECRET = "my email is alice@example.com and my SSN is 123-45-6789"

print("  when the redactor works:")
clean = deid.redact(SECRET)
check("PII is redacted on the normal path",
      "alice@example.com" not in clean and "123-45-6789" not in clean, clean)

print("\n  when the redactor CRASHES:")
_real = deid._floor
deid._floor = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("redactor blew up"))
try:
    out = deid.redact(SECRET)
finally:
    deid._floor = _real
check("NOTHING of the original text is returned",
      "alice@example.com" not in out and "123-45-6789" not in out and SECRET not in out,
      f"returned {out!r} — this is the payload that would have gone to a cloud LLM")
check("...and the caller gets a marker, not an empty string",
      out == deid.DEID_FAILED,
      f"{out!r}: empty reads as 'there was nothing here'; this means 'there was something and it was "
      f"not checked'")

print("\n  and the escape hatch still works:")
# engine=off is a STATED choice to send raw text, and must stay possible — the fix withholds on FAILURE,
# not on a deliberate opt-out.
check("engine='off' still passes text through, as documented",
      "alice@example.com" in deid.redact(SECRET, engine="off"))

print(f"\n{'[FAIL]' if failures else 'OK'} test_the_privacy_boundary_fails_closed: {failures} failure(s)")
sys.exit(1 if failures else 0)
