"""adapters.deadline_for — the PUBLIC deadline advisor, so a consumer sizes deadlines WITHOUT importing vendor_call.

Q7 of warden's audit found the measured deadline sizing (vendor_call.time_budget) had no public door — the code even
told callers to reach into the internal. This wraps it on the public adapters surface: pass a model (bare or
'provider:model'), a job intent, and the input size; the vendor + call-class are derived here. Read-only, $0.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-tbudget-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters
import spendguard.vendor_call as vc

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


ck("deadline_for is a PUBLIC adapters attribute (no vendor_call import needed by a consumer)",
   callable(getattr(adapters, "deadline_for", None)))

# the public door delegates to the internal with the DERIVED (vendor, model, class sig, in_chars)
seen = {}
_orig = vc.time_budget


def _spy(vendor, model, sig=None, default_s=None, in_chars=None):
    seen.update(vendor=vendor, model=model, sig=sig, in_chars=in_chars)
    return (123.0, "measured:class(n=9)")


vc.time_budget = _spy
sec, basis = adapters.deadline_for("openai:gpt-5.5", intent="warden:gold", in_chars=4000)
vc.time_budget = _orig
ck("derives the vendor from a 'provider:model' spec", seen.get("vendor") == "openai")
ck("strips the provider prefix for the model id", seen.get("model") == "gpt-5.5")
ck("derives the class sig from model+intent (== vendor_call.class_sig)", seen.get("sig") == vc.class_sig("gpt-5.5", "warden:gold"))
ck("passes the input size (chars) through", seen.get("in_chars") == 4000)
ck("returns the internal's (seconds, basis) unchanged", (sec, basis) == (123.0, "measured:class(n=9)"))

# real integration (no monkeypatch): no measurement → a NON-measured basis, never an invented number
su, bu = adapters.deadline_for("gpt-5.5", intent="never-seen-intent-xyz")
ck("no measurement → basis is 'unknown' or 'lane-floor' (never a fabricated measured number)", bu in ("unknown", "lane-floor"))
ck("no measurement, no default → seconds is None or a real lane floor, not a guess", su is None or su >= 30.0)

# a caller default is honored (the 'answer it yourself' path when there is no measurement)
sd, bd = adapters.deadline_for("gpt-5.5", intent="x", default_s=77)
ck("a caller default_s is honored (>= the default)", sd is not None and sd >= 77)

# a bare model id (no prefix) resolves its vendor via provider_for and never raises
sb, _bb = adapters.deadline_for("claude-sonnet-4-5", intent="x", default_s=50)
ck("a bare model id sizes without raising", sb is not None and sb >= 50)

print(("[OK]" if not fails else "[FAIL]") + " public time_budget: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
