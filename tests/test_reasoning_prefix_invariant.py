"""GUARD — models.reasons_by_default is PREFIX-INVARIANT. A model's reasoning nature drives the output FLOOR that
stops a reasoning model from spending its whole cap on hidden thinking and TRUNCATING; family rules + measured facts
are keyed by the BARE id, so a provider-qualified id (moonshot:kimi-k3, openai:gpt-5.5) used to miss them and NOT get
floored → it truncated. Measured: moonshot:kimi-k3 burned ~$7.10 in cut-off calls while bare kimi-k3 was floored.

Pinned with gpt-5.5 (a FAMILY-RULE reasoning model — recognised in any home, no stored fact needed, unlike kimi-k3
whose reasoning is a measured fact that lives only in the real store). Hermetic: isolated home, no network."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rbd-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import models   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


print("-- a reasoning model is recognised whether bare or provider-qualified (the regression) --")
ck("gpt-5.5 (bare) reasons_by_default", models.reasons_by_default("gpt-5.5") is True)
ck("openai:gpt-5.5 (provider-qualified) ALSO reasons_by_default — floored, won't truncate",
   models.reasons_by_default("openai:gpt-5.5") is True)

print("\n-- bare and provider-qualified forms AGREE for any model (invariance holds both ways) --")
for bare, prov in (("gpt-5.5", "openai"), ("claude-opus-4-8", "anthropic")):
    ck(f"{bare}: bare == {prov}:{bare}",
       models.reasons_by_default(bare) == models.reasons_by_default(f"{prov}:{bare}"))

print(f"\n{'[FAIL]' if fails else 'OK'} test_reasoning_prefix_invariant: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
