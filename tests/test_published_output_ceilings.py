"""Guardrail E — authoritative OUTPUT-CEILING overrides. The synced LiteLLM cache copied the CONTEXT window into
max_output for the GLM family (256K context → read as 'output==context' → treated as unpublished → floored to 32K), so
those models were conservatively capped at the floor instead of their real, much larger ceiling. pricing._OUTPUT_CEILING_
OVERRIDES pins the REAL provider-documented ceilings (sourced from docs.z.ai), checked BEFORE the synced cache and immune
to a re-sync. Kimi is DELIBERATELY left at the safe floor — its public data is context-poisoned (LiteLLM #22478) and no
authoritative output max is confirmed, and a floor never truncates below itself while a guessed 256K would 400.

Offline: pure resolver arithmetic; no network, no model call."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-ceiloverride-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import pricing, adapters, catalog   # noqa: E402

FLOOR = pricing.OUTPUT_FLOOR
BACKSTOP = adapters.MAX_TOKEN_CEILING
catalog.model_ceiling = lambda vendor, rid: None       # stub the live-catalog tier OFF (offline)

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

print("-- (1) GLM resolves to its REAL provider-documented ceiling, not the 32K floor it was poisoned to --")
for m, expect in [("glm-4.6", 131072), ("glm-5.3", 131072), ("glm-5.2", 131072), ("glm-4.5-air", 98304), ("glm-4.5", 98304)]:
    ck(f"max_output_tokens({m}) == {expect} (provider-doc-sourced, was floored to {FLOOR})",
       pricing.max_output_tokens(m) == expect)
    ck(f"output_budget(zai:{m}) sends the real ceiling {expect}", adapters.output_budget(f"zai:{m}") == expect)

print("\n-- (2) the override is IMMUNE to a poison learned fact (it is tier-0, above the learned tier) --")
_real_learned = pricing.max_output
try:
    pricing.max_output = lambda rid: 2000              # a poison learned fact for everything
    ck("output_ceiling(glm-5.3) still resolves to the override 131072, not the poison 2000",
       pricing.output_ceiling("zai", "glm-5.3", BACKSTOP) == 131072)
finally:
    pricing.max_output = _real_learned

print("\n-- (3) Kimi has NO override → the SAFE 32K floor (never a guessed/context-poisoned ceiling) --")
for m in ["kimi-k3", "kimi-k2.6", "kimi-k2.7-code"]:
    ck(f"{m} has no authoritative output max → stays at the floor",
       pricing.max_output_tokens(m) is None and adapters.output_budget(f"moonshot:{m}") == FLOOR)

print(f"\n{'[FAIL]' if fails else 'OK'} test_published_output_ceilings: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
