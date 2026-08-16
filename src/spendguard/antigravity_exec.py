"""Antigravity (Gemini) subscription lane — run spendguard's GEMINI-model meta prompts on the Google AI plan
(Antigravity: free / Google One AI Pro / AI Ultra / Workspace AI Ultra for Business) via the Antigravity CLI
(`agy`), mirroring codex_exec (ChatGPT/Codex) and subscription_exec (Anthropic/Max).

  • `agy -p <prompt> --output-format json --disable-slash-commands` — one non-interactive print-mode run that
    emits a SINGLE json object `{status, response, usage:{input_tokens, output_tokens, ...}}`; the text contract
    is `response` and usage is read by field name (mechanical extraction, tolerant of schema drift);
  • GEMINI_API_KEY and GOOGLE_API_KEY are STRIPPED from the child env — the CLI runs on the Antigravity OAuth
    login (cached creds under ~/.gemini), so the call can never silently become a metered API charge (exactly the
    guarantee the claude-code and codex lanes give);
  • accounting stays two-axis: $0 BILLED (kind='subscription', executor 'gemini'); the plan VALUE is the usage the
    CLI reports. NOTE: `agy` is an agent CLI, so `input_tokens` includes its own system/agent scaffolding (~15k
    even for a one-word prompt) — that is plan quota, not billed $, so it never inflates real spend;
  • any failure — CLI missing, timeout, non-zero exit, status != SUCCESS, empty response, quota exhausted —
    returns {error} and the caller falls back (pool → API): the lane can degrade, the advisor cannot break.

Doctrine note: PRINT-mode only (`-p` + --disable-slash-commands), no agent loop / tools for meta work — same as
the claude-code and codex lanes. `agy`'s model ids carry the reasoning tier as a suffix (gemini-3.7-flash-high/
-medium/-low, gemini-3.1-pro-high/-low — see `agy models`); a requested id agy does not serve exits non-zero →
API fallback, so the lane never silently answers on a different model than the caller asked for. `agy`'s id
namespace differs from the metered Gemini API's, so to route real advisor calls here set the advisor's gemini
model to an `agy` id; a plain probe (model=None) runs on agy's default and needs no mapping.
"""
import json
import os
import subprocess
import time

TIMEOUT_S = 300               # meta prompts are small; a hung CLI must not stall the daily report


def _bin():
    """Host `agy` via config.resolve_cli ($SPENDGUARD_AGY_BIN pin → PATH → well-known user-local dirs like
    ~/.local/bin, where the Antigravity installer places it) — daemons run with a minimal PATH that misses it.
    Delegating outright (no shutil.which fast-path) so an explicit pin is never overridden by a stray PATH binary,
    matching codex_exec._bin()."""
    from . import config
    return config.resolve_cli("agy", "SPENDGUARD_AGY_BIN")


def available() -> bool:
    return _bin() is not None


def _result_obj(stdout):
    """The single json result object from `agy --output-format json`. Robust to any leading log lines: parse the
    whole thing, else the LAST json object line. Absent/unparseable → None (caller returns {error})."""
    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    for ln in reversed(raw.splitlines()):
        s = ln.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def _usage(obj):
    """(input_tokens, output_tokens) from agy's `usage` object, by field name; absent → (0, 0), never invented."""
    u = obj.get("usage")
    if not isinstance(u, dict):
        return 0, 0
    def _int(k):
        v = u.get(k)
        return int(v) if isinstance(v, (int, float)) else 0
    return _int("input_tokens"), _int("output_tokens")


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S):
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed Antigravity run. `system` is
    prepended to the prompt (print mode has no separate system slot). `model` IS forwarded to `agy --model` when
    given (an agy id like gemini-3.7-flash-high); an id agy does not serve exits non-zero → API fallback."""
    exe = _bin()
    if not exe:
        return {"error": "agy (Antigravity CLI) not found"}
    full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    env = {k: v for k, v in os.environ.items() if k not in ("GEMINI_API_KEY", "GOOGLE_API_KEY")}
    cmd = [exe, "-p", full, "--output-format", "json", "--disable-slash-commands"]
    if model:
        cmd += ["--model", model.split(":", 1)[-1]]   # forward the requested id; a bad one fails → API fallback
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"gemini lane timeout ({timeout}s)"}
    except Exception as e:
        return {"error": str(e)[:200]}
    if r.returncode != 0:
        return {"error": (r.stderr or r.stdout or "agy exited non-zero").strip()[:200]}
    obj = _result_obj(r.stdout)
    if obj is None:
        return {"error": "agy produced no parseable json result"}
    if obj.get("status") != "SUCCESS":
        return {"error": (obj.get("error") or f"agy status={obj.get('status')!r}").strip()[:200]}
    text = (obj.get("response") or "").strip()
    if not text:
        return {"error": "agy produced no response text"}
    in_tok, out_tok = _usage(obj)
    return {"text": text, "in_tok": in_tok, "out_tok": out_tok, "latency": time.time() - t0, "error": None}
