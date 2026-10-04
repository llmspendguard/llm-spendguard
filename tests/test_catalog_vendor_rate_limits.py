"""Guard for the per-vendor rate-limit SSOT in the model catalog (model_catalog.vendors + rate_limit_for), offline+$0.

This data is the PROACTIVE cold-cap the dispatch governor paces against before any response header — so it must
live in the catalog (the one SSOT), be resolvable per model, and never silently vanish. The repeated pain was
re-researching published limits every time; this pins that they are now IN the catalog and queryable."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-vrl-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402
spendguard.require = lambda: None
from spendguard import model_catalog as mc  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


print("-- the vendors section exists in the catalog (the SSOT), with the behavioral flags --")
v = mc._load_vendors()
ck("anthropic + openai vendor records present", bool(v.get("anthropic")) and bool(v.get("openai")))
ck("behavioral flags saved (acceleration slow-start, cache-aware ITPM, OTPM realtime)",
   all(k in (v.get("_behaviors") or {}) for k in ("acceleration_slow_start", "cache_aware_itpm", "otpm_realtime")))

print("-- rate_limit_for resolves a model -> its family tier row (anthropic, per-family bucket) --")
op = mc.rate_limit_for("claude-opus-4-8")
ck("opus-4-8 -> Start-tier opus {1000 rpm, 2M itpm, 400k otpm}",
   op == {"rpm": 1000, "itpm": 2000000, "otpm": 400000}, extra=str(op))
ck("a 'provider:' prefix resolves the same", mc.rate_limit_for("anthropic:claude-opus-4-8") == op)
ck("haiku-4.5 -> its own family row", mc.rate_limit_for("claude-haiku-4-5") == {"rpm": 1000, "itpm": 2000000, "otpm": 400000})

print("-- per-key vendors (openai/gemini/zai) return their cold_floor (no tier table) --")
ck("gpt-5.6-sol -> openai cold_floor", (mc.rate_limit_for("gpt-5.6-sol") or {}).get("tpm") == 200000)
ck("glm-5.3 -> zai cold_floor", (mc.rate_limit_for("glm-5.3") or {}).get("rpm") == 60)

print("-- an UNKNOWN model returns None (no floor asserted the catalog does not have) --")
ck("unknown -> None (caller applies its own default, never a fabricated cap)", mc.rate_limit_for("totally-unknown-xyz") is None)

print("-- family specificity: most-specific family wins (opus-5.5 would beat opus) --")
ck("_family_of maps opus-4-8 to 'opus'", mc._family_of("anthropic", "claude-opus-4-8") == "opus")

print("-- a tier override selects a different row (Build tier opus = 5M itpm) --")
ck("tier='build' opus -> 5M itpm", (mc.rate_limit_for("claude-opus-4-8", tier="build") or {}).get("itpm") == 5000000)

print("-- the governor can read a vendor's 429/success header names from the catalog --")
hn = (mc.vendor_record("anthropic") or {}).get("header_names") or {}
ck("anthropic header_names include the input/output-token limit headers",
   hn.get("itpm") == "anthropic-ratelimit-input-tokens-limit" and hn.get("otpm") == "anthropic-ratelimit-output-tokens-limit")

print(f"\n{'OK' if not fails else 'FAIL'} test_catalog_vendor_rate_limits: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
