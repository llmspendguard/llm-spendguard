"""Subscription executor — run spendguard's OWN meta prompts on the flat-fee plan, not metered API.

The advisor's work (insight synthesis, weekly auto-fresh, quality judging) is low-volume, batched and
latency-tolerant — exactly what an Anthropic Max plan covers at zero marginal cost. With
`advisor.executor = claude-code`, adapters.call routes those prompts through a ONE-SHOT headless
Claude Code session instead of the SDK:

  • `claude -p … --output-format json --max-turns 1` — a pure completion: no agent loop, no tools,
    no persistent conversation (nothing polluted, nothing retained beyond the normal session log);
  • ANTHROPIC_API_KEY is STRIPPED from the child env — the CLI runs on the PLAN login, so the call
    can never silently become a metered API charge;
  • accounting stays two-axis and honest: the call is recorded in the corpus at $0 BILLED
    (kind='subscription'); its plan VALUE is counted by the existing claude-code est-value pipeline
    from the session transcript — value is never summed into real $;
  • any failure (CLI missing, timeout, non-zero exit, plan window exhausted) returns {error} and the
    caller FALLS BACK to the caged API path — the executor can degrade, the advisor cannot break.

Doctrine note: prompt-mode ONLY. The meta tasks keep meaning→LLM / mechanics→code — deterministic
code reads the corpus and writes the sqlite; this executor never gets tool access to do so itself.
"""
import json
import os
import shutil
import subprocess
import time

TIMEOUT_S = 300               # meta prompts are small; a hung CLI must not stall the daily report


def _bin():
    """Host claude CLI via config.resolve_cli ($SPENDGUARD_CLAUDE_BIN pin → PATH → well-known user-local
    dirs) — daemons (launchd/cron) run with a minimal PATH that misses nvm/~/.local installs. NOTE: the
    desktop app's embedded claude-code-vm binary is a Linux VM executable, NOT host-runnable — only real
    host installs resolve."""
    if shutil.which("claude"):                    # fast path (also what the offline tests stub)
        return shutil.which("claude")
    from . import config
    return config.resolve_cli("claude", "SPENDGUARD_CLAUDE_BIN")


def available() -> bool:
    return _bin() is not None


def _model_alias(model):
    """Requested API model id → Claude Code `--model` family alias (mechanical substring extraction, not a
    meaning decision). PLAN-WINDOW SMARTNESS: the advisor already picks the cheapest adequate tier for each
    meta task on the API path — the executor must honor that same tier, or every meta prompt silently runs on
    the CLI's DEFAULT model (the top tier) and burns the scarcest plan window. Unknown family → None (CLI
    default), so a new model family degrades to today's behavior rather than erroring."""
    m = (model or "").lower()
    for family in ("haiku", "sonnet", "opus"):
        if family in m:
            return family
    return None


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S):
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed completion. `model` = the
    API model id the caller would have used — mapped to the matching plan tier so subscription execution
    never upgrades a haiku-class meta prompt to the default (top) tier."""
    exe = _bin()
    if not exe:
        return {"error": "claude CLI not found"}
    cmd = [exe, "-p", prompt, "--output-format", "json", "--max-turns", "1"]
    alias = _model_alias(model)
    if alias:
        cmd += ["--model", alias]
    if system:
        cmd += ["--append-system-prompt", system]
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"subscription executor timeout ({timeout}s)"}
    except Exception as e:
        return {"error": str(e)[:200]}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "claude exited non-zero").strip()[:200]}
    try:
        d = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"error": "unparseable claude -p output"}
    if d.get("is_error"):
        return {"error": str(d.get("result") or "claude reported an error")[:200]}
    u = d.get("usage") or {}
    return {"text": d.get("result") or "", "in_tok": int(u.get("input_tokens") or 0),
            "out_tok": int(u.get("output_tokens") or 0), "latency": time.time() - t0, "error": None}
