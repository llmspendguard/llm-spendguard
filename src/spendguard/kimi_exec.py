"""Kimi Code subscription lane — run spendguard's MOONSHOT (Kimi) meta prompts on the flat-fee Kimi Code plan, not
the metered Moonshot API. Mirrors subscription_exec (Claude Max) / codex_exec (ChatGPT) over the Kimi Code CLI.

  • `kimi -p <prompt> --output-format stream-json -m kimi-code/<model>` — one non-interactive run; the final
    assistant message is the text contract (stream-json cleanly separates role=assistant from meta/tool events);
  • MOONSHOT_API_KEY is STRIPPED from the child env — the CLI runs on the Kimi Code OAuth login (managed:kimi-code),
    so the call can never silently become a metered Moonshot API charge (the claude-code / codex lane guarantee);
  • accounting stays two-axis: $0 BILLED (kind='subscription', executor 'kimi-code'); plan VALUE tracked separately;
  • ALL plan models are supported — the -m alias is looked up LIVE from ~/.kimi-code/config.toml (k3, k3-256k,
    kimi-for-coding, kimi-for-coding-highspeed, and anything Moonshot adds), never a hardcoded list;
  • any failure — CLI missing, timeout, non-zero exit, empty output, plan window exhausted — returns {error} and the
    caller falls back to the metered Moonshot API: the lane can degrade, the advisor cannot break.

The CLI emits NO token usage, so tokens are ESTIMATED from character length (len//4) for the est-value axis ONLY (an
explicit proxy, marked at the call site), never a measured count — the same contract as codex's warm-daemon path.
$0 billed either way (a flat-fee plan served it). Prompt-mode only (no agent loop for meta work): a self-contained
meta prompt needs no tools, and the run happens in a throwaway CWD so an agentic step can never touch the repo.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

TIMEOUT_S = 300


def _bin():
    """Host kimi CLI via config.resolve_cli ($SPENDGUARD_KIMI_BIN pin → PATH → well-known user-local dirs, incl.
    ~/.kimi-code/bin) — daemons run with a minimal PATH that misses it. No shutil.which fast-path (the
    resolve-cli-binary drift): resolve_cli already does pin → PATH → well-known dirs, so a pin at a missing binary
    fails LOUD instead of falling through to whatever `kimi` is on PATH."""
    from . import config
    return config.resolve_cli("kimi", "SPENDGUARD_KIMI_BIN")


def available() -> bool:
    return _bin() is not None


def _kimi_plan_aliases():
    """The plan's -m aliases from ~/.kimi-code/config.toml ([models."kimi-code/*"] keys — the EXACT form the CLI
    requires; a bare 'k3' or 'kimi-for-coding' is rejected 'not configured in config.toml'). Read LIVE so the lane
    covers EVERY model the plan serves today and anything Moonshot adds, with no hardcoded list. {} on any read gap."""
    try:
        import tomllib
        with open(os.path.expanduser("~/.kimi-code/config.toml"), "rb") as f:
            return dict(tomllib.load(f).get("models") or {})
    except Exception:
        return {}


def _kimi_model_alias(model):
    """Requested model id → the Kimi Code CLI -m alias, by EXACT identity against the plan's DECLARED models (looked
    up live from config.toml): the alias key (kimi-code/k3), its bare `model` field (k3), or the requested id with a
    leading 'kimi-' provider prefix stripped (the catalog-known metered id kimi-k3 → the plan's bare model k3). Exact
    identity over a fixed candidate set is a LOOKUP, not a substring/meaning judgement — it can never silently
    resolve one model to another; an unknown or ambiguous id returns None → the CLI's own default_model, never a
    guessed model. Supports ALL plan models via their ids, and a model added to the plan works with no code change."""
    req = (model or "").split(":", 1)[-1].strip().lower()
    if not req:
        return None
    cands = {req}
    if req.startswith("kimi-"):
        cands.add(req[len("kimi-"):])                        # metered moonshot id → its plan bare-model form (kimi-k3 → k3)
    for k, rec in _kimi_plan_aliases().items():
        if k.lower() in cands or str((rec or {}).get("model", "")).lower() in cands:
            return k
    return None


def _kimi_assistant_text(stdout):
    """The final assistant answer from `kimi -p --output-format stream-json`: JSONL where the answer rides
    {"role":"assistant","content":…} and meta/tool events ride role='meta'. Keep the LAST non-empty assistant
    content (the final message, like codex's --output-last-message); content is a string or a list of text blocks.
    Mechanical extraction of a fixed event shape; '' when no assistant message is present."""
    text = ""
    for ln in (stdout or "").splitlines():
        s = ln.strip()
        if not s.startswith("{"):
            continue
        try:
            d = json.loads(s)
        except Exception:
            continue
        if d.get("role") != "assistant":
            continue
        c = d.get("content")
        if isinstance(c, list):
            c = "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") in (None, "text"))
        if isinstance(c, str) and c.strip():
            text = c                                          # keep the latest non-empty assistant message (final answer)
    return text


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None, max_tokens=None):   # reasoning/max_tokens: protocol-uniform; kimi -p has no one-shot effort/output-cap flag → accepted, not enforced
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed Kimi Code run. `system` is prepended
    to the prompt (kimi -p has no separate system slot). `model` is mapped to the matching plan alias and forwarded
    to `-m` (an unknown id → the CLI default; a bad alias makes kimi exit non-zero → the caller falls back to the
    metered API). Tokens are ESTIMATED (len//4) — the CLI emits no usage — for the est-value axis only; $0 billed."""
    exe = _bin()
    if not exe:
        return {"error": "kimi CLI not found"}
    full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    cmd = [exe, "-p", full, "--output-format", "stream-json"]
    alias = _kimi_model_alias(model)
    if alias:
        cmd += ["-m", alias]
    from . import config
    env = config.lane_plan_env()      # strip EVERY provider's metered key — the Kimi Code OAuth login serves this; a
    #                                   kimi lane must never carry MOONSHOT_API_KEY (that would bill the metered API)
    t0 = time.time()
    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="spendguard-kimi-")   # neutral CWD: kimi is an agentic CLI; a self-contained
        #                                                        meta prompt needs no repo, and a throwaway dir keeps an
        #                                                        agentic step from ever touching the caller's working tree
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=tmpdir)
        except subprocess.TimeoutExpired:
            return {"error": f"kimi lane timeout ({timeout}s)"}
        except Exception as e:
            return {"error": str(e)[:200]}
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout or "kimi exited non-zero").strip()[:200]}
        text = _kimi_assistant_text(r.stdout)
        if not text.strip():
            return {"error": "kimi produced no assistant message"}
        return {"text": text, "in_tok": len(full) // 4, "out_tok": len(text) // 4,   # est: the CLI returns no usage
                "latency": time.time() - t0, "error": None}
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass
