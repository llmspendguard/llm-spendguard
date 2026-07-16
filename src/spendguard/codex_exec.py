"""Codex subscription lane — run spendguard's OPENAI-model meta prompts on the ChatGPT plan (Pro/Plus),
mirroring subscription_exec (the Anthropic/Max lane) over the Codex CLI.

  • `codex exec <prompt> --json --output-last-message <file>` — one non-interactive run: the final
    agent message lands in the file (the text contract), the JSONL event stream on stdout carries
    token usage when the CLI version emits it;
  • OPENAI_API_KEY is STRIPPED from the child env — the CLI runs on the ChatGPT plan login, so the
    call can never silently become a metered API charge (exactly the claude-code lane's guarantee);
  • accounting stays two-axis: $0 BILLED (kind='subscription', executor 'codex'); plan VALUE is
    counted by the existing codex est-value pipeline from the session logs;
  • any failure — CLI missing, timeout, non-zero exit, empty output, plan window exhausted — returns
    {error} and the caller falls back (pool → API): the lane can degrade, the advisor cannot break.

LIVE-VERIFY PENDING: the Codex CLI is not installed on the dev machine, so this lane is verified
structurally (offline stubs encode the documented `codex exec` interface); the defensive parse +
fallback mean an interface mismatch degrades to the API path rather than erroring. Usage extraction
matches FIELD NAMES anywhere in the event stream (mechanical extraction, tolerant of event-schema
drift across CLI versions) — absent usage records 0 tokens, never a guess.

Doctrine note: prompt-mode ONLY, same as the claude-code lane — no tools, no agent loop for meta work.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

TIMEOUT_S = 300               # meta prompts are small; a hung CLI must not stall the daily report


def available() -> bool:
    return shutil.which("codex") is not None


def _usage_from_events(stdout):
    """Best-effort (input_tokens, output_tokens) from the --json event stream: scan every JSON line for
    the usage field names wherever they appear, keep the LARGEST seen (events report cumulative totals).
    Mechanical extraction only — absent/unparseable usage is (0, 0), never invented."""
    in_tok = out_tok = 0

    def _scan(d):
        nonlocal in_tok, out_tok
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            if isinstance(v, dict):
                _scan(v)
            elif k == "input_tokens" and isinstance(v, (int, float)):
                in_tok = max(in_tok, int(v))
            elif k == "output_tokens" and isinstance(v, (int, float)):
                out_tok = max(out_tok, int(v))
    for ln in (stdout or "").splitlines():
        s = ln.strip()
        if not s.startswith("{"):
            continue
        try:
            _scan(json.loads(s))
        except Exception:
            continue
    return in_tok, out_tok


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S):
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed Codex run. `system` is
    prepended to the prompt (codex exec has no separate system slot for one-shot prompt mode); `model`
    is accepted for interface parity with the claude lane but not forwarded — the plan's default model
    serves meta prompts (Codex model selection is plan-managed; forcing ids couples us to CLI churn)."""
    if not available():
        return {"error": "codex CLI not found"}
    full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
    t0 = time.time()
    out_file = None
    try:
        fd, out_file = tempfile.mkstemp(prefix="spendguard-codex-", suffix=".txt")
        os.close(fd)
        cmd = ["codex", "exec", full, "--json", "--output-last-message", out_file]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"error": f"codex lane timeout ({timeout}s)"}
        except Exception as e:
            return {"error": str(e)[:200]}
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout or "codex exited non-zero").strip()[:200]}
        try:
            text = open(out_file).read().strip()
        except Exception:
            text = ""
        if not text:
            return {"error": "codex produced no final message"}
        in_tok, out_tok = _usage_from_events(r.stdout)
        return {"text": text, "in_tok": in_tok, "out_tok": out_tok,
                "latency": time.time() - t0, "error": None}
    finally:
        if out_file:
            try:
                os.unlink(out_file)
            except Exception:
                pass
