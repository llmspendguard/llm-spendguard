"""Offline test for per-model profiles — NO network, NO db writes.

Locks the empirically-verified family rules (the reasoning literal differs per model and the wrong one
is a hard 400) and the self-heal decision logic.
"""
from spendguard import models


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- _family rules (verified 2026-06-14) --")
check("gpt-5.5 → reasoning='none'", models._family("gpt-5.5")["reasoning"] == "none")
check("gpt-5-nano → reasoning='minimal'", models._family("gpt-5-nano")["reasoning"] == "minimal")
check("gpt-5-mini → reasoning='minimal'", models._family("gpt-5-mini")["reasoning"] == "minimal")
check("gpt-5 family → max_completion_tokens", models._family("gpt-5-nano")["tokens_param"] == "max_completion_tokens")
check("gpt-4o-mini → max_tokens (not reasoning)", models._family("gpt-4o-mini")["tokens_param"] == "max_tokens"
      and models._family("gpt-4o-mini")["reasoning"] is None)
check("claude-haiku → cache_min 2048", models._family("claude-haiku-4-5")["cache_min"] == 2048)
check("claude-opus → cache_min 1024 + explicit", models._family("claude-opus-4-8")["cache_min"] == 1024
      and models._family("claude-opus-4-8")["cache"] == "explicit")

print("-- heal_reasoning decision (no db write on these) --")
check("ignores unrelated errors", models.heal_reasoning("gpt-5.5", {"reasoning_effort": "minimal"}, "rate limit") is False)
check("ignores when no reasoning_effort in kw", models.heal_reasoning("gpt-5.5", {"max_tokens": 5}, "does not support") is False)
print("done.")
