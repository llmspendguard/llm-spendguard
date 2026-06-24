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
import pathlib
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
        acc = {"today": 0.0, "week": 0.0, "month": 0.0, "asof": today, "projects": {}}
        for r in rows or []:
            if r.get("billed"):                       # only the est-value axis belongs in this cache
                continue
            day = r.get("day") or ""
            usd = (r.get("spend_micros") or 0) / 1_000_000
            proj = (r.get("project") or "").strip().lower()
            pa = acc["projects"].setdefault(proj, {"today": 0.0, "week": 0.0, "month": 0.0})
            for k, lo in (("today", today), ("week", week), ("month", month)):
                if day >= lo:
                    acc[k] += usd                     # global window
                    pa[k] += usd                      # per-project window (for scoped receipts)
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


def _est_tally(project=None):
    """Cached est-value windows {today, week, month, asof} summed across sources. If `project` is given, scope to
    that project's per-source buckets; else the global sum. None if never stamped."""
    try:
        data = json.loads(_cache_path().read_text())
        srcs = data.get("est_value_by_source")
        if srcs:
            if project is None:
                pick = lambda s, k: s.get(k, 0)
            else:
                pl = str(project).strip().lower()
                pick = lambda s, k: (s.get("projects", {}).get(pl) or {}).get(k, 0)
            return {"today": sum(pick(s, "today") for s in srcs.values()),
                    "week": sum(pick(s, "week") for s in srcs.values()),
                    "month": sum(pick(s, "month") for s in srcs.values()),
                    "asof": max((s.get("asof", "") for s in srcs.values()), default="")}
        d = data.get("est_value")                     # back-compat: older single-blob stamp (global only)
        if project is None and isinstance(d, dict) and "today" in d:
            return d
    except Exception:
        pass
    return None


# ── the running tally (actual-$ always; est-value when cached) ────────────────
def tally(project=None, conv=None) -> dict:
    """{'actual': {today, week, month}, 'est_value': {...}|None, 'scope': project}. actual-$ from the gate ledger
    (cheap, local); est-value from the stamped cache. Optionally SCOPE to `project` (repo) and/or `conv`
    (conversation) so the receipt shows what's relevant to where you are, not a global sum. The two axes are
    returned separately and are NEVER added together."""
    today, week, month = _windows()
    actual = {"today": None, "week": None, "month": None}
    try:
        from . import budget
        actual = {"today": budget.spent_since(today, project=project, conv=conv),
                  "week": budget.spent_since(week, project=project, conv=conv),
                  "month": budget.spent_since(month, project=project, conv=conv)}
    except Exception:
        pass
    return {"actual": actual, "est_value": _est_tally(project), "scope": project}


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
    t = t or tally()
    scope = f"[{t['scope']}] " if t.get("scope") else ""
    return _PREFIX + scope + ("\n" + _INDENT).join(_tally_lines(t))


def _project_for_cwd(cwd):
    """Derive the project (repo) tag for a cwd the SAME way the gate tags charges: repo-local .spendguard.json
    `project` → git-root basename → cwd basename. So the scoped tally matches what the ledger recorded."""
    if not cwd:
        return None
    try:
        p = pathlib.Path(cwd) / ".spendguard.json"
        if p.exists():
            proj = (json.loads(p.read_text()).get("project") or "").strip().lower()
            if proj:
                return proj
    except Exception:
        pass
    try:
        import subprocess
        root = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=2).stdout.strip()
        if root:
            return os.path.basename(root).lower()
    except Exception:
        pass
    return (os.path.basename(str(cwd).rstrip("/")) or "").lower() or None


def _k(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def render_line(t: Optional[dict] = None) -> str:
    """One compact line for a status bar — both axes, still separate (billed vs plan), never summed."""
    t = t or tally()
    a = t.get("actual") or {}
    scope = f"[{t['scope']}] " if t.get("scope") else ""
    s = f"◈ {scope}billed {_k(a.get('today'))}/d · {_k(a.get('month'))}/mo"
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
def _sinks():
    """WHERE auto-emitted receipts go: env SPENDGUARD_RECEIPTS_SINK → config receipts.sinks → 'stderr'.
    Comma-separated; each is 'stderr' | 'stdout' | 'file:<path>'. The file sink is how a host with no in-chat hook
    (Codex, an editor, a tmux/menubar widget) can display the tally — point it at a log and tail/render that."""
    v = os.getenv("SPENDGUARD_RECEIPTS_SINK") or config._cfg_get("receipts", "sinks", "stderr") or "stderr"
    return [s.strip() for s in str(v).split(",") if s.strip()]


def _out(text: str) -> None:
    if not text:
        return
    for sink in _sinks():
        try:
            if sink == "stderr":
                print(text, file=sys.stderr, flush=True)
            elif sink == "stdout":
                print(text, file=sys.stdout, flush=True)
            elif sink.startswith("file:"):
                p = pathlib.Path(os.path.expanduser(sink[5:]))
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "a") as f:
                    f.write(text + "\n")
        except Exception:
            pass


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
        proj = None                                  # scope the tally to THIS flow's repo (the relevant scope)
        try:
            from . import budget
            proj = budget._project() or None
        except Exception:
            pass
        _out(render_flow(flow, lvl, tally(project=proj)))
    except Exception:
        pass


def cli(args) -> int:
    """`spendguard receipt [--footer|--flow|--json]` → prints the running tally to STDOUT (default --footer). This is
    what the Claude Code Stop hook runs to surface the tally in-chat; also handy to check the tally any time."""
    args = list(args or [])

    def _arg(flag):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    def _cur_project():
        try:
            from . import budget
            return budget._project() or None
        except Exception:
            return None

    try:
        if "--json" in args:
            print(json.dumps(tally(project=_arg("--project")), indent=2))
            return 0
        if "--stop-hook" in args:
            # Claude Code Stop hook: a `systemMessage` is the ONLY hook output the USER sees in the transcript
            # (plain stdout is debug-only). Scope to the current repo — the relevant scope, not a global sum.
            print(json.dumps({"systemMessage": render_line(tally(project=_cur_project()))}))
            return 0
        if "--statusline" in args:
            # Claude Code statusLine: session JSON arrives on stdin; prepend cwd · model · ctx% and SCOPE the tally
            # to the repo the session is in (derived from its cwd) so it shows what's relevant where you are.
            info = {}
            if not sys.stdin.isatty():           # guard: never block when run manually in a terminal
                try:
                    info = json.loads(sys.stdin.read() or "{}")
                except Exception:
                    info = {}
            cwd = (info.get("workspace") or {}).get("current_dir") or info.get("cwd") or ""
            bits = []
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
            print((prefix + "  ·  " if prefix else "") + render_line(tally(project=_project_for_cwd(cwd))))
            return 0
        # manual: --project X or --cwd P scopes to a repo; default = global overview
        proj = _arg("--project") or (_project_for_cwd(_arg("--cwd")) if _arg("--cwd") else None)
        t = tally(project=proj)
        if "--line" in args:                     # one compact line (manual / scripting)
            print(render_line(t))
            return 0
        print(render_tally(t))                   # default / --footer: the two-line block
        return 0
    except Exception:
        # A status line / hook must NEVER break the caller. Emit nothing rather than an error.
        return 0


# ── host installers — manage WHERE the always-on tally surfaces (reproducible, removable) ─────────
def _spendguard_bin():
    import shutil
    return shutil.which("spendguard") or str(pathlib.Path(sys.prefix) / "bin" / "spendguard")


def _install_claude_code(remove=False):
    import shutil
    sg = _spendguard_bin()
    p = pathlib.Path.home() / ".claude" / "settings.json"
    cfg = {}
    if p.exists():
        try:
            cfg = json.loads(p.read_text())
        except Exception:
            cfg = {}
        shutil.copy(p, p.with_suffix(".json.bak"))      # reversible
    if remove:
        if (cfg.get("statusLine") or {}).get("command", "").endswith("receipt --statusline"):
            cfg.pop("statusLine", None)
        hooks = cfg.get("hooks") or {}
        stop = [g for g in (hooks.get("Stop") or [])
                if not any(h.get("command", "").endswith("receipt --stop-hook") for h in (g.get("hooks") or []))]
        if "Stop" in hooks:
            if stop:
                hooks["Stop"] = stop
            else:
                hooks.pop("Stop", None)
            if not hooks:
                cfg.pop("hooks", None)
        action = "removed"
    else:
        cfg["statusLine"] = {"type": "command", "command": f"{sg} receipt --statusline", "padding": 0}
        stop = cfg.setdefault("hooks", {}).setdefault("Stop", [])
        if not any(h.get("command", "").endswith("receipt --stop-hook")
                   for g in stop for h in (g.get("hooks") or [])):
            stop.append({"hooks": [{"type": "command", "command": f"{sg} receipt --stop-hook", "timeout": 5}]})
        action = "installed"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"{action} spendguard receipts for Claude Code → {p}")
    print("  restart Claude Code to apply" + ("" if remove else "  (status-line footer + per-turn notice)")
          + (f"  ·  backup: {p.with_suffix('.json.bak').name}" if p.with_suffix('.json.bak').exists() else ""))
    return 0


def install_cli(args):
    """`spendguard install-receipts [--host claude-code|codex] [--remove]` — manage the always-on in-chat tally."""
    args = list(args or [])
    host = "claude-code"
    if "--host" in args:
        try:
            host = args[args.index("--host") + 1]
        except IndexError:
            pass
    remove = "--remove" in args
    if host in ("claude-code", "claude", "cc"):
        return _install_claude_code(remove=remove)
    if host == "codex":
        # Codex has no in-chat hook: its `notify` runs a program but does NOT render output in the transcript
        # (and is usually already taken). Use the inline receipt + a file sink any pane/editor can show.
        print("Codex has no Claude-Code-style in-chat hook. spendguard already TRACKS Codex (channel=codex, "
              "billed=false → est-value) via `spendguard codex show`. To surface the tally, use a file sink:")
        print("  spendguard config set receipts.sinks 'stderr,file:~/.spendguard/receipt.log'")
        print("then tail/show ~/.spendguard/receipt.log in a pane or editor. `spendguard receipt` prints it on demand.")
        return 0
    print(f"unknown host '{host}' (supported: claude-code | codex)")
    return 1
