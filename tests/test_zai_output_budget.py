"""The zai/glm lane computes its OWN output budget (run_prompt takes no max_tokens), and it used to read the
poison-prone max_output FACT first — so a poisoned-low learned fact (the auto-heal 2000/7 class) would truncate
every glm answer, and because the outer doubling-retry recomputes the same value, more budget could never heal
it. This pins the corrected authority order: the AUTHORITATIVE published ceiling first, and the fact FLOORED to
the fallback so it can never truncate below it.

  (a) published ceiling known → used verbatim (the poison fact is IGNORED — no under-truncation);
  (b) published unknown + a poisoned-LOW fact → floored to the fallback (the poison can't truncate the lane);
  (c) published unknown + a sane fact → that fact (above the fallback) is used;
  (d) nothing known → the fallback.
Offline: pricing is stubbed; no network, no key.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-zaibudget-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import zai_exec, pricing                                               # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


FB = zai_exec._FALLBACK_MAX_TOKENS


def _budget(published, fact):
    pricing.max_output_tokens = lambda m: published
    pricing.max_output = lambda m: fact
    return zai_exec._output_budget("glm-5.3")


check("(a) published ceiling known → used, the poison fact ignored", _budget(128000, 2000) == 128000)
check("(b) published unknown + poisoned-LOW fact → floored to the fallback (can't truncate)", _budget(None, 2000) == FB)
check("(c) published unknown + a sane fact above the fallback → that fact", _budget(None, 64000) == 64000)
check("(d) nothing known → the fallback", _budget(None, None) == FB)

print(f"\n{'[FAIL]' if fails else 'OK'} test_zai_output_budget: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
