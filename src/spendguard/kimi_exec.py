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
_USAGE_TTL_S = 300
_usage_cache = {"at": 0.0, "val": None}


def usage():
    """Kimi Code exposes no plan-quota/status surface. Cache the honest unknown so repeated cross-lane refreshes do
    no work and, critically, never manufacture headroom from token estimates or call counts."""
    from . import lane_quota
    return lane_quota.cached_usage(_usage_cache, _USAGE_TTL_S, lambda: None)


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


def _kimi_default_model():
    """The plan's own default model id (config.toml top-level `default_model`, e.g. kimi-code/kimi-for-coding), read
    LIVE — never a hardcoded id, so a plan whose default changes is tracked with no code edit. None on any read gap.
    This is the model `kimi acp` answers on (it has no model flag), so the warm lane is only used when the REQUESTED
    model resolves to it — otherwise the recorded model would not match the model that ran."""
    try:
        import tomllib
        with open(os.path.expanduser("~/.kimi-code/config.toml"), "rb") as f:
            v = tomllib.load(f).get("default_model")
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _daemon_enabled():
    """Use the WARM `kimi acp` server (kimi_daemon) instead of cold-starting `kimi -p` per call? Env
    SPENDGUARD_KIMI_DAEMON wins, else config advisor.kimi_daemon. Default ON so batch/comprehension fan-out pays the
    ACP startup once and subsequent turns are warm; either setting can opt out. A daemon failure falls through to the
    cold `-p` path, so enabling it changes LATENCY, never AVAILABILITY."""
    from . import config
    v = os.getenv("SPENDGUARD_KIMI_DAEMON")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(config._cfg_get("advisor", "kimi_daemon", True))


def _warm_model_eligible(model):
    """The warm ACP lane answers on the plan default_model (kimi acp has no model flag). It is SAFE for this call
    only when the requested model resolves to that default — i.e. the alias is unresolved (unknown/unpinned → the CLI
    uses its default on BOTH paths) or it equals the default. A specific OTHER plan model must take the cold `-p -m`
    path so the model that runs is the model that was requested (and recorded). None default → not eligible (can't
    prove equality → stay cold)."""
    default = _kimi_default_model()
    if default is None:
        return False
    alias = _kimi_model_alias(model)
    return alias is None or alias == default


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
    # WARM DAEMON PATH (default-on, explicit opt-out): reuse a persistent `kimi acp` server instead of cold-starting
    # `kimi -p` each call. Used ONLY when the requested model resolves to the plan default (kimi acp has no model
    # flag) — else the cold `-p -m <alias>` path runs below so the recorded model matches the model that ran. Falls
    # THROUGH to the cold path on ANY daemon failure — degrade, never break. Tokens are ESTIMATED (len//4) exactly as
    # the cold path: the ACP stream carries no usage, $0 billed either way (the flat-fee plan served it).
    if _daemon_enabled() and _warm_model_eligible(model):
        from . import kimi_daemon
        _t0 = time.time()
        try:
            _r = kimi_daemon.run_warm(full, model=model, reasoning=reasoning, timeout=timeout)
        except Exception as _e:                            # an exception must NEVER bypass the cold/API fallback below
            _r = {"error": f"kimi daemon raised: {str(_e)[:150]}"}
        if _r.get("text") and not _r.get("error"):
            _txt = _r["text"]
            return {"text": _txt, "in_tok": len(full) // 4, "out_tok": len(_txt) // 4,   # est: the ACP stream has no usage
                    "latency": round(time.time() - _t0, 2), "error": None}
        if _r.get("tool_error"):
            # A HARD refusal/cancel. A cold `kimi -p` would refuse the same way, so return the error NOW and let the
            # adapter fall back to the metered API + back off the lane. NEVER surface the refusal text as `text`. (A
            # merely TRANSIENT daemon problem — would-not-start / dead pipe / timeout — has no tool_error, so it still
            # falls through to one cold `kimi -p` below.)
            return {"error": (_r.get("error") or "kimi acp refused the request")[:200]}
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
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=tmpdir,
                               stdin=subprocess.DEVNULL)
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
