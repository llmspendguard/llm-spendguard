"""Standard ordinal reasoning knob — ONE `minimal|low|medium|high` maps to each OpenAI model's VERIFIED effort value,
so the gpt family's inconsistent naming (gpt-5.5 wants 'none', o-series/mini want 'minimal') hides behind one knob.
Only the FLOOR varies; low/medium/high are the OpenAI API's universal values. Non-reasoning models drop the param.
Anthropic thinking / Gemini suffix use different mechanisms and are normalised once MEASURED, not guessed — so those
models return None here (honest), never a guessed effort. Offline: pure family-fact lookup, no LLM.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-reason-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import models                                                          # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
nr = models.normalize_reasoning

print("-- the FLOOR varies per OpenAI model; low/medium/high are universal --")
fails += ck("gpt-5.5 minimal → its verified floor 'none'", nr("gpt-5.5", "minimal") == "none")
fails += ck("gpt-5.5 high → 'high' (universal)", nr("gpt-5.5", "high") == "high")
fails += ck("o3 minimal → 'minimal' (its verified floor)", nr("o3", "minimal") == "minimal")
fails += ck("gpt-5-mini minimal → 'minimal'", nr("gpt-5-mini", "minimal") == "minimal")
fails += ck("gpt-5-nano medium → 'medium'", nr("gpt-5-nano", "medium") == "medium")

print("\n-- non-reasoning / not-yet-wired models drop the param; the ordinal never 400s them --")
fails += ck("gpt-4o any level → None (not a reasoning model)", nr("gpt-4o", "high") is None)
fails += ck("claude-haiku → None", nr("claude-haiku-4-5", "medium") is None)
fails += ck("claude-opus → None (Anthropic thinking not wired yet — honest, not a guess)",
            nr("claude-opus-4-8", "high") is None)

print("\n-- an unrecognised level is the caller's explicit choice → passed through unchanged --")
fails += ck("unknown level passes through", nr("gpt-5.5", "turbo") == "turbo")

print(f"\n{'[FAIL]' if fails else 'OK'} test_reasoning_normalize: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
