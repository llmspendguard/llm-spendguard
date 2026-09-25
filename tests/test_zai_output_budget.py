"""The zai/glm lane computes its OWN output budget (run_prompt takes no max_tokens). It now routes through the ONE
home (adapters.output_budget → pricing.output_ceiling; docs/CANONICAL_CONCERNS.json), so it cannot DRIFT from
_call_guarded, and a poison-prone learned max_output FACT can never truncate below the 32K FLOOR. (This also raised
the lane above its old sub-floor _FALLBACK of 16384, which could itself truncate below the floor.)

  (a) published ceiling known → used verbatim (the poison fact is IGNORED — no under-truncation);
  (b) published unknown + a poisoned-LOW fact → floored to the 32K FLOOR (the poison can't truncate the lane);
  (c) published unknown + a sane fact above the floor → that fact is used;
  (d) nothing known → the 32K FLOOR ("if it is not published, the floor is 32000").
Offline: pricing + catalog are stubbed; no network, no key.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-zaibudget-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import zai_exec, pricing, catalog, adapters                            # noqa: E402

fails = []


def check(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


FLOOR = adapters.TOKEN_FLOOR
catalog.model_ceiling = lambda vendor, rid: None       # stub the live-catalog tier OFF (offline; drive via published/fact)


def _budget(published, fact):
    pricing.max_output_tokens = lambda m: published
    pricing.max_output = lambda m: fact
    return zai_exec._output_budget("glm-5.3")


check("(a) published ceiling known → used, the poison fact ignored", _budget(128000, 2000) == 128000)
check("(b) published unknown + poisoned-LOW fact → floored to the 32K FLOOR (can't truncate)", _budget(None, 2000) == FLOOR)
check("(c) published unknown + a sane fact above the floor → that fact", _budget(None, 64000) == 64000)
check("(d) nothing known → the 32K FLOOR", _budget(None, None) == FLOOR)

print(f"\n{'[FAIL]' if fails else 'OK'} test_zai_output_budget: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
