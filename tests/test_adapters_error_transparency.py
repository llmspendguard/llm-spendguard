"""adapters.call must be TRANSPARENT to its consumers (7thsense/comprehend et al.): a failure has to say WHICH
path failed (lane vs api) and WHY — the real transport cause behind a generic 'Connection error.', not just the
opaque wrapper. Regression guard for the masked-error class that cost a consumer a day of debugging.

Offline: the anthropic SDK is replaced with a stub whose client raises a WRAPPED connection error (a generic
outer with a real inner cause), so we exercise the real _call_once except-path without a network call.
"""
import os
import sys
import types
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-errtrans-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"          # get past the key check to reach the (stubbed) client
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters                                                       # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


print("-- _exc_cause: the REAL transport error behind a generic wrapper is surfaced --")
try:
    try:
        raise TimeoutError("timed out reading from api.anthropic.com")
    except Exception as _inner:
        raise RuntimeError("Connection error.") from _inner
except Exception as e:
    chain = adapters._exc_cause(e)
ck("cause chain names the underlying error, not just 'Connection error.'",
   chain and "TimeoutError" in chain and "timed out" in chain)
ck("a bare exception with no cause → None (nothing invented)", adapters._exc_cause(ValueError("x")) is None)


# stub the anthropic SDK: its client raises a WRAPPED connection error, exactly the shape that masked the cause
class _BoomStream:
    def __enter__(self):
        try:
            raise TimeoutError("connection to api.anthropic.com timed out after 1800s")
        except Exception as _inner:
            raise RuntimeError("Connection error.") from _inner

    def __exit__(self, *a):
        return False


class _BoomMessages:
    def stream(self, **kw):
        return _BoomStream()


class _BoomClient:
    def __init__(self, *a, **k):
        self.messages = _BoomMessages()


_fake = types.ModuleType("anthropic")
_fake.Anthropic = _BoomClient
sys.modules["anthropic"] = _fake

print("-- a failed API call reports WHICH path (executor) and the real cause, not an opaque string --")
r = adapters._call_once("claude-haiku-4-5", "hi", max_tokens=8, timeout_s=1800, _skip_lane=True)
ck("the call did NOT raise — it returned a dict (contract: never raises)", isinstance(r, dict))
ck("error is populated (the call failed)", bool(r.get("error")))
ck("executor='api' — the caller can tell it was the metered API path, not a lane", r.get("executor") == "api")
ck("error_type is the exception CLASS (a structured signal, not prose)", r.get("error_type") == "RuntimeError")
ck("cause surfaces the REAL reason behind the generic wrapper ('Connection error.')",
   r.get("cause") and "TimeoutError" in r["cause"] and "timed out" in r["cause"])
ck("provider + model are on the result so the caller knows what was attempted",
   r.get("provider") == "anthropic" and r.get("model") == "claude-haiku-4-5")

print(f"\n{'[FAIL]' if fails else 'OK'} test_adapters_error_transparency: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
