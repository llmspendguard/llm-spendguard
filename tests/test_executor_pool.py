"""Executor POOL routing (adapters._lane_for + call) — N subscriptions at once, provider-respecting:
`pool` serves anthropic-model prompts on the claude-code lane AND openai-model prompts on the codex
lane, NEVER cross-provider (the recorded model must be the model that answered). A lane failure cools
that lane (advisor.pool_cooldown_s) so bursts go straight to the API; single-lane settings only ever
touch their own provider. Offline: both lane modules stubbed, no CLI, no network.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-pool-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import adapters, subscription_exec, codex_exec

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


calls_made = {"claude": 0, "codex": 0}


def claude_ok(prompt, system=None, model=None, timeout=None):
    calls_made["claude"] += 1
    return {"text": "CLAUDE LANE", "in_tok": 10, "out_tok": 5, "latency": 0.1, "error": None}


def codex_ok(prompt, system=None, model=None, timeout=None):
    calls_made["codex"] += 1
    return {"text": "CODEX LANE", "in_tok": 11, "out_tok": 6, "latency": 0.1, "error": None}


subscription_exec.run_prompt = claude_ok
codex_exec.run_prompt = codex_ok
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "pool"
os.environ["SPENDGUARD_POOL_COOLDOWN_S"] = "3600"

print("-- pool: each provider's prompts ride its OWN plan lane --")
r = adapters.call("claude-haiku-4-5-20251001", "classify…")
ck("anthropic model → claude-code lane, $0 billed",
   r.get("executor") == "claude-code" and r["text"] == "CLAUDE LANE" and r["cost"] == 0.0)
r = adapters.call("openai:gpt-5.5", "classify…")
ck("openai model → codex lane, $0 billed",
   r.get("executor") == "codex" and r["text"] == "CODEX LANE" and r["cost"] == 0.0)
ck("no cross-lane leakage", calls_made == {"claude": 1, "codex": 1})

print("-- lane failure → cooldown → straight to API until it expires --")
def codex_down(prompt, system=None, model=None, timeout=None):
    calls_made["codex"] += 1
    return {"error": "plan window exhausted"}
codex_exec.run_prompt = codex_down
r = adapters.call("openai:gpt-5.5", "classify…")
ck("failed lane falls back to the API path (its error, not the lane's)",
   r.get("executor") is None and r.get("error") and "plan window" not in (r.get("error") or ""))
n = calls_made["codex"]
r2 = adapters.call("openai:gpt-5.5", "classify…")
ck("cooling lane is SKIPPED (no second lane attempt)", calls_made["codex"] == n and r2.get("executor") is None)
ck("the OTHER lane is unaffected by the cooldown",
   adapters.call("claude-haiku-4-5-20251001", "x").get("executor") == "claude-code")
adapters._lane_cooldown.clear()
codex_exec.run_prompt = codex_ok
ck("after cooldown clears the lane serves again",
   adapters.call("openai:gpt-5.5", "x").get("executor") == "codex")

print("-- single-lane settings never touch the other provider --")
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "claude-code"
n = calls_made["codex"]
r = adapters.call("openai:gpt-5.5", "x")
ck("executor=claude-code + openai model → API (codex lane never called)",
   calls_made["codex"] == n and r.get("executor") is None)
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "codex"
n = calls_made["claude"]
r = adapters.call("claude-haiku-4-5-20251001", "x")
ck("executor=codex + anthropic model → API (claude lane never called)",
   calls_made["claude"] == n and r.get("executor") is None)
os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"
n = dict(calls_made)
adapters.call("claude-haiku-4-5-20251001", "x"); adapters.call("openai:gpt-5.5", "x")
ck("executor=api touches no lane at all", calls_made == n)

print("-- subscription rows recorded per lane (billed axis honest at $0) --")
from spendguard import calls as callstore
rows = [r for r in (callstore.recent(20) or []) if r.get("kind") == "subscription"] if hasattr(callstore, "recent") else None
if rows is None:
    print("  (calls.recent not available — recording covered by test_subscription_exec)")
else:
    ck("subscription rows exist for both providers",
       {r.get("provider") for r in rows} >= {"anthropic", "openai"})

print(f"\n{'[FAIL]' if fails else 'OK'} test_executor_pool: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
