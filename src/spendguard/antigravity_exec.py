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
import re
import subprocess
import time

TIMEOUT_S = 300               # meta prompts are small; a hung CLI must not stall the daily report
_USAGE_TTL_S = 300            # /usage (the quota oracle) is re-read at most this often — weekly windows don't move fast
_LANE_FAMILY = "gemini"       # the model family this lane serves; the /usage bucket to consult when a probe has no model
_usage_cache = {"at": 0.0, "val": None}   # {at: last-read unix ts, val: parsed buckets or None} — see usage()


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


def _usage_from_result(obj):
    """(input_tokens, output_tokens) from agy's `usage` object, by field name; absent → (0, 0), never invented.
    Named for its SOURCE (agy's single result object), parallel to codex_exec._usage_from_events and distinct
    from litellm_adapter._usage(response_obj) — same job family, different inputs, so different names."""
    u = obj.get("usage")
    if not isinstance(u, dict):
        return 0, 0
    def _int(k):
        v = u.get(k)
        return int(v) if isinstance(v, (int, float)) else 0
    return _int("input_tokens"), _int("output_tokens")


def _reset_window_s(text):
    """Seconds until reset, PARSED from a rate/quota envelope's 'resets in Nh/Nm/Ns' (or 'retry after N…') token
    — a fixed-shape DURATION token, like a timestamp or a batch id. This is parsing, NOT a judgement about what
    an error means: there is deliberately no keyword classification of arbitrary error prose ('is this a quota
    error?' is a model's job, not a substring list). A provider states this window ONLY when it is rate/quota
    limiting, so the PRESENCE of a well-formed token is itself the structured signal the caller acts on, and its
    value is how long to back off. None when no such token is present — the caller then treats it as an ordinary
    lane failure, never guessing quota from wording."""
    t = str(text or "")
    # The reset/backoff WINDOW is a fixed-shape token: a temporal preposition ("in"/"after") followed by a
    # compound H/M/S duration — "in 95h39m1s", "after 30m", "in 2 hours". Keying on the TOKEN, not on a growing
    # list of quota verbs (resets / renews / retry / available / try again / …), is what keeps this a PARSE, not a
    # keyword classification of what an error MEANS (the docstring's rule). It also fixes the two real bugs: the
    # old `\d+\s*h\b` never matched the COMPOUND "95h39m1s" (the `h` is followed by a digit), and it only knew two
    # phrasings. A well-formed duration after in/after in an error envelope IS the structured signal; we sum every
    # part and take the largest window. None when no such token is present. A duration from a non-reset error
    # (e.g. a timeout "after 60s") merely cools+retries the lane briefly — transient, never a size limit — which
    # is the correct handling anyway.
    _UNIT = {"h": 3600, "m": 60, "s": 1}
    best = None
    _run = r"(?:\d+\s*(?:h(?:ou)?rs?|hrs?|h|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)\s*)+"
    for seg in re.findall(rf"(?:\bin|\bafter)\s+({_run})", t, re.I):
        total = 0
        for num, unit in re.findall(r"(\d+)\s*([hms])", seg, re.I):
            total += int(num) * _UNIT[unit.lower()]
        if total and (best is None or total > best):
            best = total
    return best


# ── agy /usage as a QUOTA-RESET ORACLE ───────────────────────────────────────────────────────────────────────
# agy reports Gemini quota exhaustion through the print/JSON path as a bare, window-LESS ERROR (status='ERROR',
# empty response), so _reset_window_s finds nothing and the lane would be blind-retried every few minutes. But
# `agy /usage` DOES expose the exact per-bucket weekly reset. Reading it turns that window-less ERROR into the SAME
# structured retry_after_s a parseable "resets in …" carries, so the EXISTING transient path cools the lane until
# its (capped, re-tested) reset instead of guessing — no adapter change needed. $0 (subscription OAuth), cached so
# a burst of failures costs one CLI round-trip, and it FAILS SAFE (any absence/parse gap → None → ordinary handling).
def _parse_usage(text):
    """PARSE (fixed-shape) the /usage table into quota buckets: [{bucket, remaining_pct, reset_ts}]. Each line
    carries a bucket label, an integer PERCENT, and an ISO-8601 reset timestamp; the percent and the timestamp are
    known-shape tokens pulled by regex, the label is the text before them. Extraction, NOT a meaning decision — no
    wording is judged; the % and the ISO stamp ARE the structured signal (mirrors _reset_window_s's parse rule)."""
    import datetime
    out = []
    for line in (text or "").splitlines():
        m_pct = re.search(r"(\d+)\s*%", line)
        m_ts = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", line)
        if not (m_pct and m_ts):
            continue
        try:
            reset = datetime.datetime.fromisoformat(m_ts.group(0).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        out.append({"bucket": line[:m_pct.start()].strip(), "remaining_pct": int(m_pct.group(1)),
                    "reset_ts": float(reset)})
    return out or None


def _fetch_usage():
    """Run `agy /usage` on the subscription OAuth (metered keys stripped) and parse it → buckets or None. The raw
    fetch behind usage(); lane_quota.cached_usage wraps it with the TTL + reset-boundary freshness bounds."""
    exe = _bin()
    if not exe:
        return None
    from . import config
    r = subprocess.run([exe, "-p", "/usage", "--output-format", "text", "--print-timeout", "30s"],
                       capture_output=True, text=True, timeout=45, env=config.lane_plan_env())
    return _parse_usage(r.stdout) if r.returncode == 0 else None


def usage():
    """agy's quota report parsed into buckets [{bucket, remaining_pct, reset_ts}], or None (agy absent / call or
    parse failed → ordinary lane handling). Cached via lane_quota.cached_usage: TTL + reset-boundary invalidation
    (past a cached snapshot's soonest reset the quota has refilled, so a stale-EXHAUSTED read never masks a
    recovered lane), so a burst of lane failures costs ONE $0 CLI round-trip."""
    from . import lane_quota
    return lane_quota.cached_usage(_usage_cache, _USAGE_TTL_S, _fetch_usage)


def _quota_reset_s():
    """Seconds until this account's quota next RESETS, when a quota bucket is EXHAUSTED — else None. Decision-FREE
    by design: it does NOT classify which bucket serves which model (that would be a semantic judgement made
    mechanically); it reads the STRUCTURED quota NUMBERS agy reports and returns the SOONEST reset among buckets at
    0% remaining. The lane consults this ONLY on a real FAILURE, so 'a bucket is exhausted' is strong evidence the
    failure is quota; the 30-min re-test cap (_max_quota_cool_s) means an over-cool from an unrelated exhausted
    bucket costs at most one early re-test, never a wrong answer; and it fails SAFE (no exhausted bucket → None →
    ordinary lane handling). Ground truth from /usage, never error wording."""
    rows = usage()
    if not rows:
        return None
    now = time.time()
    resets = [float(r.get("reset_ts") or 0) - now for r in rows if int(r.get("remaining_pct", 100)) <= 0]
    resets = [s for s in resets if s > 0]
    return int(min(resets)) if resets else None


def _error_result(text):
    """An error result. When the message carries a parseable reset-window TOKEN (a rate/quota envelope), attach a
    STRUCTURED `retry_after_s` so the caller can demote the lane until it resets — without ever reading this string.
    No inline token → consult /usage (ground truth): if any account quota bucket is exhausted, cool until its reset,
    so agy's window-LESS quota ERROR still demotes the lane. Neither present → a plain error, an ordinary lane miss."""
    out = {"error": (text or "").strip()[:200]}
    ra = _reset_window_s(text) or _quota_reset_s()
    if ra:
        out["retry_after_s"] = ra
    return out


def run_prompt(prompt, system=None, model=None, timeout=TIMEOUT_S, reasoning=None):   # reasoning: protocol-uniform; Gemini's effort rides the MODEL SUFFIX (…-low/-high), set upstream, so ignored here
    """→ {text, in_tok, out_tok, latency, error} from one headless plan-billed Antigravity run. `system` is
    prepended to the prompt (print mode has no separate system slot). `model` IS forwarded to `agy --model` when
    given (an agy id like gemini-3.7-flash-high); an id agy does not serve exits non-zero → API fallback."""
    exe = _bin()
    if not exe:
        return {"error": "agy (Antigravity CLI) not found"}
    full = (f"{system.strip()}\n\n{prompt}" if system else prompt)
    from . import config
    env = config.lane_plan_env()      # strip EVERY provider's metered key — the Antigravity OAuth serves this; the
    #                                   gemini lane must never carry ANTHROPIC_API_KEY (Claude tokens) or any other
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
        return _error_result(r.stderr or r.stdout or "agy exited non-zero")   # quota→a reset window rides retry_after_s
    obj = _result_obj(r.stdout)
    if obj is None:
        return {"error": "agy produced no parseable json result"}
    if obj.get("status") != "SUCCESS":
        return _error_result(obj.get("error") or f"agy status={obj.get('status')!r}")
    text = (obj.get("response") or "").strip()
    if not text:
        return {"error": "agy produced no response text"}
    in_tok, out_tok = _usage_from_result(obj)
    return {"text": text, "in_tok": in_tok, "out_tok": out_tok, "latency": time.time() - t0, "error": None}
