"""generation_support is the measurement-determinism SSOT: the knobs (temperature/top_p/seed) a provider/model's
METERED API actually honors. It must reflect REALITY, not an assumption — the bug (2026-10-09) was that it granted
{temperature, top_p} to claude-haiku-5-5, which 400s '`temperature` is deprecated for this model' (and the same for
top_p). The fix: generation_support SUBTRACTS knobs a MEASURED fact records as unsupported
(models.mark_gen_unsupported), so a knob the vendor rejects is never advertised. This guards:
  • a recorded gen_unsupported fact removes that knob from generation_support (per provider/model),
  • the fact resolves for a provider-qualified id too (bare-id fallback),
  • unaffected knobs/models still report their default support,
  • seed stays OpenAI-only.
A measured fact (the vendor's own 400, recorded) drives it — never regex over error prose. Offline, isolated HOME."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-gensup-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, models  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

# A non-reasoning anthropic model defaults to {temperature, top_p} (no seed — anthropic has no seed param).
M = "claude-haiku-5-5"
base = adapters.generation_support("anthropic", M)
ck("default (pre-fact) grants temperature+top_p for a non-reasoning model", base == {"temperature", "top_p"})

# Record the MEASURED facts (the vendor 400'd both as 'deprecated for this model', confirmed 2026-10-09).
models.mark_gen_unsupported(M, "temperature")
models.mark_gen_unsupported(M, "top_p")
after = adapters.generation_support("anthropic", M)
ck("after recording BOTH as unsupported, generation_support grants NEITHER (no lie)", after == set())
ck("unsupported_gen_knobs reports both", models.unsupported_gen_knobs(M) == {"temperature", "top_p"})

# The fact resolves for a provider-qualified id too (bare-id fallback).
ck("a provider-qualified id resolves the same fact", adapters.generation_support("anthropic", "anthropic:" + M) == set())

# A DIFFERENT model is unaffected — only the measured model loses the knobs.
ck("an unrecorded model keeps its default support", adapters.generation_support("anthropic", "claude-3-5-sonnet") == {"temperature", "top_p"})

# seed stays OpenAI-only, and a recorded temperature fact there still leaves seed.
models.mark_gen_unsupported("gpt-x-test", "temperature")
sup = adapters.generation_support("openai", "gpt-x-test")
ck("openai keeps seed even when temperature is recorded unsupported", "seed" in sup and "temperature" not in sup)

print(f"\n{'[FAIL]' if fails else 'OK'} test_generation_support_measured: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
