"""Inline spend receipts — make what spendguard tracked visible AS IT HAPPENS, plus the running tally.

After every gated FLOW (a `with spendguard.context(...)` block, a batch submit, or a CLI command) we emit one
compact summary: what ran, input/output tokens, estimate → actual cost, and the running daily/weekly/monthly tally.
Per-FLOW, never per-call — a chat loop fires hundreds of tiny calls and a receipt each would be noise; we aggregate
at the flow boundary (see `calls.context`).

TWO AXES, NEVER SUMMED (the invariant the whole system enforces):
  • ACTUAL-$   — billed workload (batch + realtime + GPU). Reconciles to provider truth. Source: the gate ledger
                 (`budget.spent_since`) — always-on, no admin key, zero LLM.
  • EST-VALUE  — Claude Code / claude.ai usage value: what it WOULD cost at API rates, NOT money billed. Source:
                 a small cache the heavier `cc`/`chat`/`sync` runs stamp (they already compute it), read here with
                 an "as-of" date so it stays honest.

Verbosity (config `receipts` / env `SPENDGUARD_RECEIPTS`): off | footer | flow | verbose.
  off     — emit nothing.
  footer  — only the running tally (used by the CLI / the Claude Code Stop hook).
  flow    — a per-flow block + the tally (default).
  verbose — flow + tally + a learned tip + the caller.

Auto-emit (flow boundaries) goes to STDERR so it never corrupts a script's stdout / piped data. The explicit
`spendguard receipt` command prints to STDOUT — that's what the Claude Code Stop hook captures to surface in-chat.
Cost: a local sqlite read + a small JSON read. Safe to emit always; it never spends and never needs a key.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from typing import Optional

from . import config

LEVELS = ("off", "footer", "flow", "verbose")
_CACHE = "receipt_cache.json"          # under SPENDGUARD_HOME — est-value windows stamped by cc/chat/sync


# ── verbosity ────────────────────────────────────────────────────────────────
def level() -> str:
    """Resolved receipts verbosity: env SPENDGUARD_RECEIPTS → config `receipts` → 'flow'. Unknown → 'flow'."""
    v = (os.getenv("SPENDGUARD_RECEIPTS") or config._cfg_get("receipts", "level", "flow") or "flow")
    v = str(v).strip().lower()
    return v if v in LEVELS else "flow"


# ── formatting ─────────────────────────────────────────────────────────────--
def _money(x: Optional[float]) -> str:
    return "—" if x is None else f"${x:,.2f}"


def _tok(n: Optional[int]) -> str:
    if not n:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _pct(est: Optional[float], actual: Optional[float]) -> str:
    """Signed variance of actual vs estimate, e.g. '−11%'. Empty when either side is missing/zero."""
    if not est or actual is None:
        return ""
    d = (actual - est) / est
    sign = "−" if d < 0 else "+"
    return f" ({sign}{abs(d) * 100:.0f}%)"


# ── date windows (UTC, matching budget's day strings) ─────────────────────────
def _utc_today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _windows():
    t = _utc_today()
    return (t.strftime("%Y-%m-%d"),
            (t - _dt.timedelta(days=6)).strftime("%Y-%m-%d"),   # rolling 7d (incl. today)
            t.replace(day=1).strftime("%Y-%m-%d"))


# ── est-value cache (stamped by the heavier flows; read cheaply here) ─────────
def _cache_path():
    return config.HOME / _CACHE


def stamp_est_value(rows, source: str = "claude-code") -> None:
    """Persist this SOURCE's est-value (billed=False) windows so the footer can show them without a heavy recompute.
    `rows` are day_totals-shaped dicts (day + spend_micros + billed). Stored per-source so claude-code and claude.ai
    sum instead of clobbering each other. Best-effort; never raises into the caller. Pass the FULL (unfiltered)
    history so the month window is complete regardless of any report's day filter."""
    try:
        today, week, month = _windows()
        acc = {"today": 0.0, "week": 0.0, "month": 0.0, "asof": today}
        for r in rows or []:
            if r.get("billed"):                       # only the est-value axis belongs in this cache
                continue
            day = r.get("day") or ""
            usd = (r.get("spend_micros") or 0) / 1_000_000
            if day >= today:
                acc["today"] += usd
            if day >= week:
                acc["week"] += usd
            if day >= month:
                acc["month"] += usd
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
        data.setdefault("est_value_by_source", {})[source] = acc
        p.write_text(json.dumps(data, indent=0))
    except Exception:
        pass


def _est_tally():
    """Cached est-value windows {today, week, month, asof} summed across sources, or None if never stamped."""
    try:
        data = json.loads(_cache_path().read_text())
        srcs = data.get("est_value_by_source")
        if srcs:
            return {"today": sum(s.get("today", 0) for s in srcs.values()),
                    "week": sum(s.get("week", 0) for s in srcs.values()),
                    "month": sum(s.get("month", 0) for s in srcs.values()),
                    "asof": max((s.get("asof", "") for s in srcs.values()), default="")}
        d = data.get("est_value")                     # back-compat: older single-blob stamp
        if isinstance(d, dict) and "today" in d:
            return d
    except Exception:
        pass
    return None


# ── the running tally (actual-$ always; est-value when cached) ────────────────
def tally() -> dict:
    """{'actual': {today, week, month}, 'est_value': {...}|None}. actual-$ from the gate ledger (cheap, local);
    est-value from the stamped cache (best-effort, carries its own as-of date). The two are returned separately and
    are NEVER added together."""
    today, week, month = _windows()
    actual = {"today": None, "week": None, "month": None}
    try:
        from . import budget
        actual = {"today": budget.spent_since(today),
                  "week": budget.spent_since(week),
                  "month": budget.spent_since(month)}
    except Exception:
        pass
    return {"actual": actual, "est_value": _est_tally()}


# ── rendering ─────────────────────────────────────────────────────────────--
_PREFIX = "spendguard ▸ "
_INDENT = " " * len("spendguard ▸ ")            # align continuation lines under the first


def _tally_lines(t: dict) -> list:
    """The running-tally line(s): actual-$ always; est-value when cached. The two axes are SEPARATE lines and are
    never combined into a single number."""
    a = t.get("actual") or {}
    lines = [f"actual-$ (billed): today {_money(a.get('today'))} · "
             f"7d {_money(a.get('week'))} · month {_money(a.get('month'))}"]
    ev = t.get("est_value")
    if ev:
        asof = f" (as of {ev['asof']})" if ev.get("asof") else ""
        lines.append(f"est-value (plan, not billed){asof}: today {_money(ev.get('today'))} · "
                     f"7d {_money(ev.get('week'))} · month {_money(ev.get('month'))}")
    return lines


def render_tally(t: Optional[dict] = None) -> str:
    lines = _tally_lines(t or tally())
    return _PREFIX + ("\n" + _INDENT).join(lines)


def _k(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def render_line(t: Optional[dict] = None) -> str:
    """One compact line for a status bar — both axes, still separate (billed vs plan), never summed."""
    t = t or tally()
    a = t.get("actual") or {}
    s = f"◈ billed {_k(a.get('today'))}/d · {_k(a.get('month'))}/mo"
    ev = t.get("est_value")
    if ev:
        s += f"  ·  plan {_k(ev.get('today'))}/d · {_k(ev.get('month'))}/mo"
    return s


def render_flow(flow: dict, lvl: str, t: Optional[dict] = None) -> str:
    intent = flow.get("intent") or "(flow)"
    n = flow.get("n") or 0
    head = f"{_PREFIX}{intent} · {n} call{'' if n == 1 else 's'}"
    if flow.get("in_tok") is not None or flow.get("out_tok") is not None:
        head += f" · in {_tok(flow.get('in_tok'))} / out {_tok(flow.get('out_tok'))}"
    est, act = flow.get("est"), flow.get("actual")
    if est is not None:
        head += f" · est {_money(est)} → actual {_money(act)}{_pct(est, act)}"
    else:
        head += f" · actual {_money(act)}"
    parts = [head] + [_INDENT + ln for ln in _tally_lines(t or tally())]
    if lvl == "verbose":
        tip = _learned_tip(intent)
        if tip:
            parts.append(_INDENT + f"tip: {tip}")
        if flow.get("caller"):
            parts.append(_INDENT + f"↳ {flow['caller']}")
    return "\n".join(parts)


def _learned_tip(intent: Optional[str]) -> Optional[str]:
    """A relevant learned insight for this intent, if the advisor has one cheaply. Best-effort; None otherwise."""
    try:
        from . import learn
        fn = getattr(learn, "quick_tip", None)
        if fn:
            return fn(intent)
    except Exception:
        pass
    return None


# ── emission ─────────────────────────────────────────────────────────────--
def _out(text: str) -> None:
    if text:
        print(text, file=sys.stderr, flush=True)


def emit_flow(intent, chain, start) -> None:
    """Emit a per-flow receipt at a flow boundary (calls.context exit). `start` is the (rowid, usd) snapshot taken
    at flow enter. Guarded + level-gated; only acts at 'flow'/'verbose'. Silent if the flow neither called nor spent.
    NEVER raises into the caller's code path."""
    try:
        lvl = level()
        if lvl not in ("flow", "verbose"):
            return
        start_rid, start_usd = (start or (0, 0.0))
        agg = None
        try:
            from . import calls
            agg = calls.flow_agg(start_rid, chain)
        except Exception:
            pass
        actual = None
        try:
            from . import budget
            actual = max(0.0, budget.spent_since("1970-01-01") - (start_usd or 0.0))
        except Exception:
            pass
        n = (agg or {}).get("n") or 0
        # if the flow did nothing measurable, stay quiet (don't spam empty receipts)
        if not n and not actual:
            return
        flow = {"intent": intent, "n": n,
                "in_tok": (agg or {}).get("in_tok"),
                "out_tok": (agg or {}).get("out_tok"),
                "actual": actual if actual else (agg or {}).get("cost"),
                "est": (agg or {}).get("est"),
                "caller": (agg or {}).get("caller")}
        _out(render_flow(flow, lvl))
    except Exception:
        pass


def cli(args) -> int:
    """`spendguard receipt [--footer|--flow|--json]` → prints the running tally to STDOUT (default --footer). This is
    what the Claude Code Stop hook runs to surface the tally in-chat; also handy to check the tally any time."""
    args = list(args or [])
    try:
        if "--json" in args:
            print(json.dumps(tally(), indent=2))
            return 0
        if "--stop-hook" in args:
            # Claude Code Stop hook: a `systemMessage` is the ONLY hook output the USER sees in the transcript
            # (plain stdout is debug-only). One concise line per turn-end. We don't read the event JSON on stdin.
            print(json.dumps({"systemMessage": render_line()}))
            return 0
        if "--statusline" in args:
            # Claude Code statusLine: session JSON arrives on stdin; we prepend cwd · model · ctx% to the tally.
            info = {}
            if not sys.stdin.isatty():           # guard: never block when run manually in a terminal
                try:
                    info = json.loads(sys.stdin.read() or "{}")
                except Exception:
                    info = {}
            bits = []
            cwd = (info.get("workspace") or {}).get("current_dir") or info.get("cwd") or ""
            if cwd:
                bits.append(os.path.basename(str(cwd).rstrip("/")))
            model = (info.get("model") or {}).get("display_name")
            if model:
                bits.append(str(model))
            cw = info.get("context_window") or info.get("contextWindow") or {}
            pct = cw.get("used_percentage") if isinstance(cw, dict) else None
            if pct is not None:
                try:
                    bits.append(f"{float(pct):.0f}% ctx")
                except Exception:
                    pass
            prefix = "  ·  ".join(bits)
            print((prefix + "  ·  " if prefix else "") + render_line())
            return 0
        if "--line" in args:                     # one compact line (manual / scripting)
            print(render_line())
            return 0
        print(render_tally())                    # default / --footer: the two-line block
        return 0
    except Exception:
        # A status line / hook must NEVER break the caller. Emit nothing rather than an error.
        return 0
