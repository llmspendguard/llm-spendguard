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

TWO PATHS, TWO USAGE CONTRACTS (so the "never a guess" line is not read too broadly): the EXEC path
(`codex exec --json`) carries real usage in the event stream → measured, or 0 when absent, never guessed. The
WARM-DAEMON path (`codex mcp-server`) returns the answer TEXT but NO token usage, so it ESTIMATES tokens from
character length (len//4) for the est-value axis ONLY — an explicit proxy, marked at the call site, never a
measured count. $0 billed either way (a flat-fee plan served it).

Doctrine note: prompt-mode ONLY, same as the claude-code lane — no tools, no agent loop for meta work.
"""
import json
import os
import subprocess
import tempfile
import time

TIMEOUT_S = 300               # meta prompts are small; a hung CLI must not stall the daily report
_USAGE_TTL_S = 300            # the codex rate-limit log is re-read at most this often (shared cache adds reset-boundary)
_usage_cache = {"at": 0.0, "val": None}


def _bin():
    """Host codex CLI via config.resolve_cli ($SPENDGUARD_CODEX_BIN pin → PATH → well-known user-local
    dirs) — daemons run with a minimal PATH that misses nvm/~/.local installs."""
    # NO shutil.which FAST PATH. It ran BEFORE the pin was consulted, so $SPENDGUARD_CODEX_BIN was silently
    # ignored whenever a `codex` existed on PATH — an explicit pin overridden by whatever was lying around, in
    # a tool whose whole premise is never silently substituting one thing for another. resolve_cli already
    # does the PATH lookup (pin → PATH → well-known dirs), so nothing is lost by delegating outright, and a
    # pin that points at a missing binary now fails LOUD instead of falling through to a different binary.
    from . import config
    return config.resolve_cli("codex", "SPENDGUARD_CODEX_BIN")


def available() -> bool:
    return _bin() is not None


def _usage_from_events(stdout):
    """Best-effort (input_tokens, output_tokens) from the --json event stream: scan every JSON line for
    the usage field names wherever they appear, keep the LARGEST seen (events report cumulative totals).
    Mechanical extraction only — absent/unparseable usage is (0, 0), never invented."""
    in_tok = out_tok = 0

    def _scan(d):
        nonlocal in_tok, out_tok
        if isinstance(d, list):                       # usage can sit inside ARRAYS too (a choices/items/events list) —
            for it in d:                               # the contract is "field names ANYWHERE in the stream", so descend
                _scan(it)                              # into lists as well as dicts, else array-nested usage reads 0.
            return
        if not isinstance(d, dict):
            return
        for k, v in d.items():
            if isinstance(v, (dict, list)):
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


def _plugin_disable_flags():
    """`codex exec` COLD-STARTS every call, and the dominant cost is loading the user's enabled plugins / MCP servers
    (MEASURED 2026-08-19: a real one-shot went from >75s to **5s** with them off). A headless completion needs none
    of them, so disable each ENABLED plugin for THIS invocation — read live from ~/.codex/config.toml so it adapts to
    whatever the user has (no hardcoded plugin names). NB: a single `-c 'plugins={}'` does NOT work — the per-plugin
    `[plugins."x@y"]` tables win — so each must be turned off by name. Best-effort → [] on any parse problem."""
    cfg = os.path.expanduser("~/.codex/config.toml")
    try:
        import tomllib
        with open(cfg, "rb") as f:
            d = tomllib.load(f)
    except Exception:
        return []
    flags = []
    for name, rec in (d.get("plugins") or {}).items():
        if isinstance(rec, dict) and rec.get("enabled"):
            flags += ["-c", f'plugins."{name}".enabled=false']
    return flags


def _daemon_enabled():
    """Use the WARM `codex mcp-server` (codex_daemon) instead of cold-starting `codex exec` per call? Env
    SPENDGUARD_CODEX_DAEMON wins, else config advisor.codex_daemon. Default OFF: the daemon is proven + available,
    but arming a per-process persistent subprocess on the hot lane is a deliberate opt-in (the exec path stays the
    safe default, and offline tests that stub `codex exec` are untouched)."""
    from . import config
    v = os.getenv("SPENDGUARD_CODEX_DAEMON")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(config._cfg_get("advisor", "codex_daemon", False))


def _codex_effort(level):
    """Map a STANDARD ordinal reasoning level → the Codex plan model's OWN scale. The Codex model accepts
    none|low|medium|high|xhigh|max and has NO 'minimal' (that is the OpenAI *API* scale — MEASURED 2026-08-19:
    sending 'minimal' is a hard 400 "not supported with the ... model. Supported values are: none, low, medium,
    high, xhigh, max"). So 'minimal' → 'none' (its floor); the rest pass through and codex validates them (a bad
    value fails fast → the lane degrades to API). Empty → None (leave codex's own default)."""
    lv = (level or "").strip().lower()
    if not lv:
        return None
    return "none" if lv == "minimal" else lv


# ── codex QUOTA: read the rate-limit events codex records in its logs sqlite (opportunistic, FILE-based) ───────
# The ChatGPT plan exposes no quota status command, and `codex exec --json` emits only token counts (verified: no
# rate-limit field). But the CLI LOGS the rate-limit envelope it receives on every real call into its logs sqlite,
# as `codex.rate_limits` — {primary/secondary: {used_percent, window_minutes, reset_at (unix ts)}}. Reading the
# freshest one (read-only) gives real plan headroom, refreshed by traffic, with no extra call. None on any gap.
def _codex_home():
    """Codex's state dir: $CODEX_HOME, else the documented default ~/.codex. Not hardcoded to one path — an override
    or a relocated home still resolves (and _plugin_disable_flags already reads config.toml from the same home)."""
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _parse_rate_limits(obj):
    """A codex.rate_limits payload → buckets [{bucket, remaining_pct, reset_ts}]. primary/secondary each carry
    used_percent (remaining = 100 - used) and reset_at (absolute unix ts; reset_after_seconds is the relative
    fallback). PARSE of a fixed provider schema, not a judgement. None when neither bucket is present."""
    rl = (obj or {}).get("rate_limits") or {}
    out = []
    for name in ("primary", "secondary"):
        b = rl.get(name)
        if not isinstance(b, dict) or b.get("used_percent") is None:
            continue
        reset = b.get("reset_at")
        if reset is None and b.get("reset_after_seconds") is not None:
            reset = time.time() + float(b["reset_after_seconds"])
        # The event is a point-in-time snapshot; codex logs it per call, so it can be days old and its window may
        # have CYCLED. Roll a past reset forward by whole windows → the NEXT actual reset, so a stale record shows a
        # meaningful future date (not a past one) and the reset-boundary cache does not thrash. window_minutes is
        # the period; without it a past reset is left as-is (best effort).
        win = float(b.get("window_minutes") or 0) * 60.0
        if reset is not None and win > 0 and float(reset) < time.time():
            import math
            reset = float(reset) + math.ceil((time.time() - float(reset)) / win) * win
        out.append({"bucket": f"{name} ({int(b.get('window_minutes') or 0)}m window)",
                    "remaining_pct": max(0, 100 - int(round(float(b["used_percent"])))),
                    "reset_ts": (float(reset) if reset is not None else None)})
    return out or None


def _fetch_usage():
    """The FRESHEST codex.rate_limits event across every logs*.sqlite in the codex home → buckets or None. Freshness
    is decided by each event's OWN `ts` column, NOT the file's mtime — a restore/rsync/iCloud sync/`touch` can
    scramble mtimes and make a stale logs_1.sqlite outrank the current logs_2.sqlite, but the event timestamp
    cannot lie. Read-ONLY (codex is writing live); any absence/lock/parse gap → None (fail safe, quota UNKNOWN)."""
    import glob
    import sqlite3
    best = None                                        # (event_ts, body) with the greatest event_ts seen
    for db in glob.glob(os.path.join(_codex_home(), "logs*.sqlite")):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
            try:
                row = con.execute("SELECT ts, feedback_log_body FROM logs WHERE feedback_log_body LIKE "
                                  "'%codex.rate_limits%' ORDER BY id DESC LIMIT 1").fetchone()
            finally:
                con.close()
        except Exception:
            continue
        if row and row[1] and (best is None or (row[0] or 0) > best[0]):
            best = ((row[0] or 0), row[1])
    if not best:
        return None
    i = best[1].find("{")
    if i < 0:
        return None
    try:
        return _parse_rate_limits(json.loads(best[1][i:]))
    except Exception:
        return None


def usage():
    """This lane's ChatGPT-plan quota parsed into buckets [{bucket, remaining_pct, reset_ts}], or None (quota
    UNKNOWN). Read from the rate-limit events codex records in its logs sqlite — reflecting codex's LAST real call,
    refreshed by traffic. Cached via lane_quota.cached_usage (TTL + reset-boundary invalidation). $0, no call."""
    from . import lane_quota
    return lane_quota.cached_usage(_usage_cache, _USAGE_TTL_S, _fetch_usage)


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None):
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed Codex run. `system` is
    prepended to the prompt (codex exec has no separate system slot for one-shot prompt mode). `model` IS
    forwarded to `codex -m` when given (e.g. gpt-5.5), so the recorded model is the one that actually ran —
    and an id the plan does not serve (MEASURED: gpt-5.6 is rejected 400 "not supported ... with a ChatGPT
    account") makes codex exit non-zero, so adapters cools the lane and falls back to the metered API: the
    lane degrades, it never silently answers on a different model than the caller asked for.
    `--skip-git-repo-check` because a headless one-shot is not an interactive session that needs the
    working-tree guard, and honestreview may run it from any directory (measured: /tmp tripped the guard)."""
    # WARM DAEMON PATH (opt-in): reuse a persistent codex mcp-server instead of cold-starting `codex exec` each call
    # (>75s → ~5s, reliable). Falls THROUGH to the exec path on any daemon failure — degrade, never break. Stateless
    # here (the lane carries no thread); persistent context is a higher-level feature (codex_daemon.run(thread=…)).
    if _daemon_enabled():
        from . import codex_daemon
        _full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
        _t0 = time.time()
        try:
            _r = codex_daemon.run_warm(_full, model=model, reasoning=reasoning)
        except Exception as _e:                        # an exception must NEVER bypass the exec/API fallback below
            _r = {"error": f"codex daemon raised: {str(_e)[:150]}"}
        if _r.get("text") and not _r.get("error"):
            _txt = _r["text"]
            return {"text": _txt, "in_tok": len(_full) // 4, "out_tok": len(_txt) // 4,   # est: the tool returns no usage
                    "latency": round(time.time() - _t0, 2), "error": None}
        if _r.get("tool_error"):
            # HARD request rejection (model not served on the plan, etc.). A cold `codex exec` would fail the same
            # way, so return the error NOW and let the adapter fall back to the metered API + back off this
            # (lane, model). NEVER surface the rejection text as `text` — that is the very bug that recorded a codex
            # 400 as content. (A merely TRANSIENT daemon problem — would-not-start / dead pipe — has no tool_error,
            # so it still falls through to one cold `codex exec` below.)
            return {"error": (_r.get("error") or "codex rejected the request")[:200]}
    exe = _bin()
    if not exe:
        return {"error": "codex CLI not found"}
    full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    from . import config
    env = config.lane_plan_env()      # strip EVERY provider's metered key — the ChatGPT plan login serves it; a codex
    #                                   lane must never carry ANTHROPIC_API_KEY (Claude tokens) or any other metered key
    t0 = time.time()
    out_file = None
    try:
        fd, out_file = tempfile.mkstemp(prefix="spendguard-codex-", suffix=".txt")
        os.close(fd)
        cmd = [exe, "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "--output-last-message", out_file]
        cmd += _plugin_disable_flags()                     # the TWO per-call cold-start costs a headless completion needs
        #                                                    NEITHER: the writable-workspace sandbox (-s read-only above)
        #                                                    AND loading enabled plugins/MCP servers. Both off: >75s → ~5s.
        _eff = _codex_effort(reasoning)
        if _eff:
            cmd += ["-c", f"model_reasoning_effort={_eff}"]   # Codex's OWN scale (none|low|…); 'minimal'→'none' upstream
        if model:
            cmd += ["-m", model.split(":", 1)[-1]]     # forward the requested id; a bad one fails → API fallback
        cmd += ["--", full]      # `--` end-of-options: a prompt/system beginning with '-' is a positional, not a flag
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
