"""Guard for the unknown-provider fix (2026-09-27): a call to an unregistered provider (e.g. a caller passed
'google:…' when the registered id is 'gemini'/'agy') must NOT raise a bare KeyError from PROVIDERS[prov] — which used
to surface as transport_error and get RETRIED (a config bug re-run against the same bad id). It must return a clear,
actionable error naming the registered providers, MARKED preflight so vendor_call._classify → PREFLIGHT_UNMET
(deterministic, non-retryable). Honest, never a silent alias (auto-mapping google→gemini would hide the mistake).

Offline + deterministic ($0 — the guard returns BEFORE any provider client is built or any call is made).
Isolation: SPENDGUARD_HOME → tempfile.mkdtemp before importing spendguard.
"""
import os, sys, tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-unkprov-")

from spendguard import adapters, vendor_call as vc   # noqa: E402


class Checks:
    def __init__(self):
        self.fails = 0

    def ck(self, label, cond, extra=""):
        if not cond:
            self.fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def main():
    c = Checks()

    crashed = None
    r = None
    try:
        r = adapters._call_once("google:gemini-2.5-pro", "hi", max_tokens=50)   # 'google' is NOT a registered provider
    except KeyError as e:
        crashed = e
    c.ck("unknown provider does NOT raise KeyError", crashed is None, "KeyError %s" % crashed)
    c.ck("returns an error result (not a crash, not a silent success)", isinstance(r, dict) and bool(r.get("error")),
         str(r)[:80] if r else "None")
    c.ck("error is actionable — names the registered providers", "not registered" in (r or {}).get("error", "")
         and "gemini" in (r or {}).get("error", ""), (r or {}).get("error", "")[:100])
    c.ck("marked preflight_unmet", (r or {}).get("preflight_unmet") is True)

    k, _ = vc._classify(r or {})
    c.ck("classifies as PREFLIGHT_UNMET (deterministic)", k == vc.PREFLIGHT_UNMET, "got %s" % k)
    c.ck("PREFLIGHT_UNMET is NOT retryable (the queue won't re-run a config bug)",
         vc.PREFLIGHT_UNMET not in vc.RETRYABLE)

    # a KNOWN provider still resolves (the fix is narrow to the unregistered case) — provider_for returns it, no error path
    c.ck("a registered provider still resolves normally", adapters.provider_for("gemini:gemini-2.5-pro") == "gemini")

    print(f"\n{'[FAIL]' if c.fails else 'OK'} test_unknown_provider_preflight: {c.fails} failure(s)")
    return 1 if c.fails else 0


if __name__ == "__main__":
    sys.exit(main())
