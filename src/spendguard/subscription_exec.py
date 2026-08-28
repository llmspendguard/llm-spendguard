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
_USAGE_TTL_S = 300            # `claude /usage` is re-read at most this often (the shared cache adds reset-boundary invalidation)
_usage_cache = {"at": 0.0, "val": None}


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
    """Is this lane's CLI on the host? codex_exec.available is the identical check for its own binary — the
    duplication is the two-line body, and it stays two lines because each resolves a DIFFERENT binary."""
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


# ── claude /usage as this lane's QUOTA surface (a pure status command, $0) ────────────────────────────────────
# Claude Code exposes real plan limits in print mode: `claude -p "/usage"` prints, per window, "<label>: N% used ·
# resets <date>". USED %, so remaining = 100 - N. Parsed into the shared bucket shape so the cross-lane headroom
# view and (later) routing read every lane the same way. STATUS-POLL like agy — no model call, no metered spend.
def _parse_reset(text):
    """Best-effort unix ts from a Claude /usage 'resets …' phrase ('Aug 28 at 12:10am (America/Los_Angeles)',
    'Sep 3 at 9am (…)'). A PARSE of a known human-date shape (month, day, clock time, optional tz); the YEAR is
    absent so the NEXT occurrence is taken. None on any gap — remaining_pct (the primary signal) never needs it."""
    import re
    import datetime
    m = re.search(r"resets\s+([A-Za-z]{3,9})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\s*(?:\(([^)]+)\))?",
                  text, re.I)
    if not m:
        return None
    mon_s, day_s, hh_s, mm_s, ampm, tzname = m.groups()
    try:
        month = datetime.datetime.strptime(mon_s[:3].title(), "%b").month
        hh = int(hh_s) % 12 + (12 if (ampm or "").lower() == "pm" else 0)
        tz = None
        if tzname:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tzname)
            except Exception:
                tz = None
        nowdt = datetime.datetime.now(tz)
        cand = datetime.datetime(nowdt.year, month, int(day_s), hh, int(mm_s or 0), tzinfo=tz)
        if cand < nowdt - datetime.timedelta(days=1):     # the date already well past → it is next year's occurrence
            cand = cand.replace(year=nowdt.year + 1)
        return cand.timestamp()
    except Exception:
        return None


def _parse_usage_claude(text):
    """PARSE (fixed-shape) Claude Code's /usage into buckets [{bucket, remaining_pct, reset_ts}]. Each quota line is
    '<label>: <N>% used[ · resets <date>]'; the percent is USED, so remaining = 100 - used. Extraction of known
    tokens (a percent, a date) from a fixed layout, NOT a wording judgement (mirrors antigravity_exec._parse_usage)."""
    import re
    out = []
    for line in (text or "").splitlines():
        m = re.search(r"([^:]+):\s*(\d+)%\s*used", line)
        if not m:
            continue
        out.append({"bucket": m.group(1).strip(), "remaining_pct": max(0, 100 - int(m.group(2))),
                    "reset_ts": _parse_reset(line)})
    return out or None


def _fetch_usage():
    """Run `claude -p /usage` on the plan LOGIN (metered keys stripped) and parse it → buckets or None. The raw
    fetch behind usage(); lane_quota.cached_usage adds the TTL + reset-boundary freshness bounds."""
    exe = _bin()
    if not exe:
        return None
    from . import config
    r = subprocess.run([exe, "-p", "/usage"], capture_output=True, text=True, timeout=45, env=config.lane_plan_env())
    return _parse_usage_claude((r.stdout or "") + "\n" + (r.stderr or "")) if r.returncode == 0 else None


def usage():
    """This lane's plan quota parsed into buckets [{bucket, remaining_pct, reset_ts}], or None (claude CLI absent /
    call or parse failed → quota UNKNOWN, ordinary handling). Cached via lane_quota.cached_usage (TTL +
    reset-boundary invalidation). $0 — a pure status command, no model call."""
    from . import lane_quota
    return lane_quota.cached_usage(_usage_cache, _USAGE_TTL_S, _fetch_usage)


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None):   # reasoning: protocol-uniform; the Claude CLI has no one-shot effort flag → ignored for now
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
    from . import config
    env = config.lane_plan_env()      # strip EVERY provider's metered key/auth-token/base-url — the Max plan LOGIN
    #                                   serves this; the claude-code lane must never fall back onto a metered
    #                                   ANTHROPIC key (that would bill Claude for "$0 plan" work), nor carry another
    #                                   provider's key. (config.lane_plan_env centralizes what _PLAN_STRIP_ENV did.)
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
