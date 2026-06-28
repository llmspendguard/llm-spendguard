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
        # ORG → TEAM → PROJECT attribution (the useful axis, same as the server). Stored as flat cells keyed
        # org|team|project; the tree is built on render. (Each row carries the agentic classification from cls.)
        acc = {"today": 0.0, "week": 0.0, "month": 0.0, "asof": today, "cells": {}}
        for r in rows or []:
            if r.get("billed"):                       # only the est-value axis belongs in this cache
                continue
            day = r.get("day") or ""
            usd = (r.get("spend_micros") or 0) / 1_000_000
            org = (r.get("org") or "").strip().lower()
            team = (r.get("team") or "").strip().lower()
            proj = (r.get("project") or "").strip().lower()
            ca = acc["cells"].setdefault(f"{org}|{team}|{proj}",
                                         {"org": org, "team": team, "project": proj, "today": 0.0, "week": 0.0, "month": 0.0})
            for k, lo in (("today", today), ("week", week), ("month", month)):
                if day >= lo:
                    acc[k] += usd
                    ca[k] += usd
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


def _est_cells():
    """All est-value cells across sources: [{org, team, project, today, week, month}]. The flat store the tree is
    built from."""
    out = []
    try:
        for s in (json.loads(_cache_path().read_text()).get("est_value_by_source") or {}).values():
            out.extend((s.get("cells") or {}).values())
    except Exception:
        pass
    return out


def _est_tally(org=None, team=None, project=None):
    """Est-value windows {today, week, month, asof} summed across sources, optionally scoped to an org / team /
    project (the org→team→project attribution). No filter = global. None if never stamped."""
    try:
        data = json.loads(_cache_path().read_text())
        srcs = data.get("est_value_by_source")
        if srcs:
            if org is None and team is None and project is None:
                return {"today": sum(s.get("today", 0) for s in srcs.values()),
                        "week": sum(s.get("week", 0) for s in srcs.values()),
                        "month": sum(s.get("month", 0) for s in srcs.values()),
                        "asof": max((s.get("asof", "") for s in srcs.values()), default="")}
            ol = None if org is None else org.strip().lower()
            tl = None if team is None else team.strip().lower()
            pl = None if project is None else project.strip().lower()
            agg = {"today": 0.0, "week": 0.0, "month": 0.0}
            for c in _est_cells():
                if ol is not None and c.get("org") != ol:
                    continue
                if tl is not None and c.get("team") != tl:
                    continue
                if pl is not None and c.get("project") != pl:
                    continue
                for k in agg:
                    agg[k] += c.get(k, 0)
            agg["asof"] = max((s.get("asof", "") for s in srcs.values()), default="")
            return agg
        d = data.get("est_value")                     # back-compat: older single-blob stamp (global only)
        if org is None and team is None and project is None and isinstance(d, dict) and "today" in d:
            return d
    except Exception:
        pass
    return None


def _est_tree(scope_org=None):
    """Nested ORG → TEAM → PROJECT plan-value tree (month) from the cells, optionally one org.
    {org: {month, teams: {team: {month, projects: {project: month}}}}}."""
    tree = {}
    sl = None if scope_org is None else str(scope_org).strip().lower()
    for c in _est_cells():
        if sl is not None and c.get("org") != sl:
            continue
        m = c.get("month", 0)
        if m <= 0:
            continue
        o = tree.setdefault(c.get("org") or "", {"month": 0.0, "teams": {}})
        t = o["teams"].setdefault(c.get("team") or "", {"month": 0.0, "projects": {}})
        o["month"] += m
        t["month"] += m
        t["projects"][c.get("project") or ""] = t["projects"].get(c.get("project") or "", 0.0) + m
    return tree


# ── remote-compute (GPU) cache — the billed REMOTE component, stamped by resources.sync ───────────
def stamp_remote(rows) -> None:
    """Persist BILLED remote-compute (GPU/vast.ai) windows so the receipt can show the Remote component without
    re-hitting the provider. `rows` are day_totals-shaped (day + spend_micros + billed); only billed remote rows
    count. Best-effort; never raises. Mirrors stamp_est_value (which holds the NON-billed est-value axis)."""
    try:
        today, week, month = _windows()
        acc = {"today": 0.0, "week": 0.0, "month": 0.0, "asof": today}
        for r in rows or []:
            if r.get("billed") is False:               # remote compute is real billed $ (default billed=True)
                continue
            day = r.get("day") or ""
            usd = (r.get("spend_micros") or 0) / 1_000_000
            for k, lo in (("today", today), ("week", week), ("month", month)):
                if day >= lo:
                    acc[k] += usd
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
        data["remote"] = acc
        p.write_text(json.dumps(data, indent=0))
    except Exception:
        pass


def _remote_tally():
    """Billed remote-compute windows {today, week, month, asof}, or None if resources.sync never stamped it."""
    try:
        d = json.loads(_cache_path().read_text()).get("remote")
        if isinstance(d, dict) and "month" in d:
            return d
    except Exception:
        pass
    return None


# ── the running tally — REAL $ (API + Subscription + Remote) and est-value, NEVER summed ─────────
def tally(project=None, conv=None) -> dict:
    """The two axes, returned SEPARATELY and never added together:
      • REAL $ (money out the door) = `api` (per-token API billing, from the gate ledger) + `subscription` (the flat
        plan fee — Anthropic Max, OpenAI Pro, …) + `remote` (billed GPU/vast.ai, cached by resources.sync).
      • `est_value` = the value of subscription-covered usage (Claude Code/claude.ai/Codex), NOT billed.
    `real_month` = api.month + subscription + remote.month. `actual` is kept as an alias of `api` for back-compat."""
    today, week, month = _windows()
    api = {"today": None, "week": None, "month": None}
    try:
        from . import budget
        api = {"today": budget.spent_since(today), "week": budget.spent_since(week), "month": budget.spent_since(month)}
    except Exception:
        pass
    remote = _remote_tally()                       # billed GPU/remote compute (None until resources.sync stamps it)
    sub, sub_assumed = _plan_usd()                 # flat monthly subscription fee — a REAL cost (out the door)
    out = {"api": api, "actual": api, "remote": remote, "subscription": sub, "subscription_assumed": sub_assumed,
           "est_value": _est_tally()}
    out["real_month"] = (api.get("month") or 0) + (sub or 0) + ((remote or {}).get("month") or 0)
    ev = out["est_value"]
    if ev and (ev.get("month") or 0) > 0 and sub:
        out["plan_usd"] = sub
        out["plan_assumed"] = sub_assumed
        out["plan_mult"] = (ev.get("month") or 0) / sub      # est-value as a multiple of the subscription FEE (ROI)
    return out


# Default subscription mix — MIRRORS the server's ASSUMED (app/page.tsx): Anthropic Max (20×) + an OpenAI/ChatGPT
# Pro seat. These flat fees are REAL costs (out the door). Declare your real mix in config `subscription.plans`
# (a list of {name, usd}) or set `subscription.plan_usd` / SPENDGUARD_PLAN_USD to override.
_DEFAULT_PLANS = (("Anthropic Max", 200.0), ("OpenAI Pro", 200.0))


def _plan_usd():
    """Monthly subscription $ for the proportional plan slice → (usd, assumed). Order: explicit
    `subscription.plan_usd`/SPENDGUARD_PLAN_USD → sum of configured `subscription.plans` → DEFAULT (Anthropic Max +
    OpenAI Pro, $400; assumed=True so the UI can say so, like the server does — never silently wrong)."""
    v = os.getenv("SPENDGUARD_PLAN_USD") or config._cfg_get("subscription", "plan_usd", None)
    if v:
        try:
            return float(v), False
        except (TypeError, ValueError):
            pass
    plans = config._cfg_get("subscription", "plans", None)
    if isinstance(plans, list) and plans:
        try:
            return float(sum(float(p.get("usd", 0)) for p in plans)), False
        except Exception:
            pass
    return float(sum(u for _, u in _DEFAULT_PLANS)), True


# ── rendering ─────────────────────────────────────────────────────────────--
_PREFIX = "spendguard ▸ "
_INDENT = " " * len("spendguard ▸ ")            # align continuation lines under the first


def _gate_blocks_line():
    """Make the test-first ENFORCEMENT visible (spec §9): this-month counts of blocked / would-block / overridden
    bulk runs from gate_blocks.jsonl. Returns a line, or None if there's nothing to show (no events / no file)."""
    try:
        import time as _t
        path = os.path.join(os.path.dirname(config.db_path()), "gate_blocks.jsonl")
        if not os.path.exists(path):
            return None
        mstart = _t.mktime(_t.strptime(_t.strftime("%Y-%m-01"), "%Y-%m-%d"))
        c = {"blocked": 0, "would-block": 0, "override": 0}
        for ln in open(path):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if (o.get("ts") or 0) >= mstart and o.get("decision") in c:
                c[o["decision"]] += 1
        if not any(c.values()):
            return None
        return ("test-first gate (mo): %d blocked · %d would-block · %d overridden"
                % (c["blocked"], c["would-block"], c["override"]))
    except Exception:
        return None


def _tally_lines(t: dict) -> list:
    """The running-tally line(s). HARD RULE: REAL $ (money out the door) is shown as NAMED components — API
    (per-token) + Subscription (flat plan fee) + Remote (GPU/compute) — then est-value (plan usage, NOT billed) on
    a SEPARATE line after '::'. The two axes are never combined into one number (that mixed total is the confusion
    this exists to prevent)."""
    api = t.get("api") or t.get("actual") or {}
    rem = t.get("remote") or {}
    sub = t.get("subscription") or 0
    am, rm = api.get("month"), rem.get("month")
    real = (am or 0) + (sub or 0) + (rm or 0)
    parts = [f"API {_money(am)}"]
    if sub:
        parts.append(f"subs {_money(sub)}{'*' if t.get('subscription_assumed') else ''}")
    parts.append(f"remote {_money(rm)}" if rm is not None else "remote —")
    extra = "" if (am is None) else f"  (billed; API today {_money(api.get('today'))} · 7d {_money(api.get('week'))})"
    lines = [f"real $ this month: {_money(real)}  =  " + "  +  ".join(parts) + extra]
    ev = t.get("est_value")
    if ev:
        asof = f" (as of {ev['asof']})" if ev.get("asof") else ""
        mult = f"  →  {t['plan_mult']:.0f}× the subscription" if t.get("plan_mult") else ""
        lines.append(f":: est sub value (plan usage, NOT billed){asof}: month {_money(ev.get('month'))}"
                     f" · today {_money(ev.get('today'))} · 7d {_money(ev.get('week'))}{mult}")
    gb = _gate_blocks_line()
    if gb:
        lines.append(gb)
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
                return proj                       # the repo's own configured project name wins
    except Exception:
        pass
    return config.git_root_project(cwd) or (os.path.basename(str(cwd).rstrip("/")) or "").lower() or None


def _k(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"${x / 1000:.1f}k" if x >= 1000 else f"${x:.0f}"


def render_line(t: Optional[dict] = None) -> str:
    """One compact status-bar line — REAL $ (API + subs + remote) then est-value, SEPARATE, never summed."""
    t = t or tally()
    api = t.get("api") or t.get("actual") or {}
    rem = t.get("remote") or {}
    sub = t.get("subscription") or 0
    real = (api.get("month") or 0) + (sub or 0) + ((rem.get("month")) or 0)
    scope = f"[{t['scope']}] " if t.get("scope") else ""
    bits = [f"API {_k(api.get('month'))}"]
    if sub:
        bits.append(f"subs {_k(sub)}")
    if rem.get("month") is not None:
        bits.append(f"remote {_k(rem.get('month'))}")
    s = f"◈ {scope}real {_k(real)}/mo = " + " + ".join(bits)
    ev = t.get("est_value")
    if ev:
        s += f"  ::  est value {_k(ev.get('month'))}/mo"
    return s


def _conv_repos(conv=None, cwd=None):
    """The repo(s) THIS conversation is relevant to (collapsed view): the cwd repo + any the conversation touched."""
    repos = set()
    try:
        from . import budget
        repos |= set(budget.projects_for_conv(conv or budget._conv()))
        cp = _project_for_cwd(cwd) if cwd else (budget._project() or None)
        if cp:
            repos.add(cp)
    except Exception:
        pass
    return sorted(r for r in repos if r)


def _all_repos():
    """Every repo with billed charges OR plan-value (expanded view)."""
    repos = set()
    try:
        from . import budget
        repos |= set(budget.all_projects())
    except Exception:
        pass
    try:
        data = json.loads(_cache_path().read_text())
        for srec in (data.get("est_value_by_source") or {}).values():
            repos |= set((srec.get("repos") or {}).keys())
    except Exception:
        pass
    return sorted(r for r in repos if r)


def _sum_repos(repos):
    am = em = 0.0
    for r in repos:
        t = tally(project=r)
        am += (t.get("actual") or {}).get("month") or 0
        em += (t.get("est_value") or {}).get("month") or 0
    return {"actual_month": am, "est_month": em}


def _month_total(t):
    return ((t.get("actual") or {}).get("month") or 0) + ((t.get("est_value") or {}).get("month") or 0)


def _est_breakdown(repo):
    """{project: {"month": m}} — est-value cells under `repo` (matched as a team OR project name), aggregated by
    project. The per-project detail `_breakdown_line` renders; empty (→ no breakdown line) when there's no finer
    split (e.g. `repo` already IS the leaf project, or an actual-$-only repo with no est-value cells)."""
    rl = (repo or "").strip().lower()
    bd = {}
    for c in _est_cells():
        if rl and c.get("team") != rl and c.get("project") != rl:
            continue
        p = c.get("project") or ""
        bd.setdefault(p, {"month": 0.0})["month"] += c.get("month", 0) or 0
    return bd


def _breakdown_line(repo, top=4):
    """One indented line of a repo's top classified PROJECTS by month plan-value — the agentic breakdown under the
    repo rollup. Empty when the repo has no project detail (e.g. an actual-$-only repo)."""
    bd = _est_breakdown(repo)
    items = sorted(((p, w.get("month") or 0) for p, w in bd.items() if p and p != repo), key=lambda x: -x[1])
    items = [(p, m) for p, m in items if m > 0][:top]
    if not items:
        return None
    return _INDENT + "  └ " + " · ".join(f"{p} {_k(m)}" for p, m in items)


def _render_scope(scope_all=False, conv=None, cwd=None, line=False, top=12, breakdown=True):
    """Contextual receipt, REPO > PROJECT: COLLAPSED (default) = this conversation's repo(s); EXPANDED (--all) =
    every repo RANKED by month spend (top `top` + tail summarized). Each repo is a rollup line; underneath, its
    classified-project breakdown (the agentic detail) — unless `line`/breakdown is off."""
    render = render_line if line else render_tally

    def block(repo):
        out = [render(tally(project=repo))]
        if breakdown:
            bl = _breakdown_line(repo)
            if bl:
                out.append(bl)
        return out

    if scope_all:
        ranked = sorted(((r, tally(project=r)) for r in _all_repos()), key=lambda rt: -_month_total(rt[1]))
        ranked = [rt for rt in ranked if _month_total(rt[1]) > 0]            # drop $0 buckets
        parts = []
        for r, _t in ranked[:top]:
            parts += block(r)
        rest = ranked[top:]
        if rest:
            am = sum((t.get("actual") or {}).get("month") or 0 for _, t in rest)
            em = sum((t.get("est_value") or {}).get("month") or 0 for _, t in rest)
            parts.append(f"{_INDENT}▸ + {len(rest)} smaller repos: billed {_k(am)}/mo · plan {_k(em)}/mo")
        parts.append(render({**tally(), "scope": "all repos"}))
        return "\n".join(parts)
    # collapsed: this conversation's repo(s) + the rest summarized
    shown = _conv_repos(conv, cwd)
    if not shown:
        return render(tally())
    parts = []
    for r in shown:
        parts += block(r)
    hidden = [r for r in _all_repos() if r not in set(shown)]
    if hidden:
        h = _sum_repos(hidden)
        parts.append(f"{_INDENT}▸ + {len(hidden)} more repos: billed {_k(h['actual_month'])}/mo · "
                     f"plan {_k(h['est_month'])}/mo  — `spendguard receipt --all`")
    return "\n".join(parts)


def _conv_org():
    """The org this conversation/connection belongs to (the repo's .spendguard.json org) — the default tree scope."""
    try:
        from . import saas
        return (saas.conn().get("org") or "").strip().lower() or None
    except Exception:
        return None


def _two_axis_table(t: dict) -> list:
    """The two axes as a TABLE — `Actual $` and `Est value $` in SEPARATE columns. Each row sits in ONE column (the
    other shows —); the columns total INDEPENDENTLY and are NEVER added into one number. This is the hard rule: an
    est-value row is $0 in the Actual column, so the two can never be silently mixed."""
    api = (t.get("api") or {}).get("month")
    rem = (t.get("remote") or {}).get("month")
    sub = t.get("subscription") or 0
    ev = t.get("est_value") or {}
    evm = ev.get("month")
    asof = f" (as of {ev['asof']})" if ev.get("asof") else ""
    cell = lambda x: (_money(x) if x is not None else "—")
    LW = 32
    rows = [("API (batch + realtime)", api, None),
            ("Remote compute (vast.ai)", rem, None),
            ("Subscription (Max + Pro)", (sub or None), None),
            ("Plan usage (Claude Code·Codex·ai)", None, evm)]
    out = [f"{'':<{LW}}{'Actual $':>12}{'Est value $':>14}    ← two axes, never added"]
    for label, a, e in rows:
        out.append(f"{label:<{LW}}{cell(a):>12}{cell(e):>14}")
    out.append("─" * (LW + 26))
    out.append(f"{('TOTAL' + asof):<{LW}}{_money((api or 0) + (rem or 0) + (sub or 0)):>12}{cell(evm):>14}")
    return out


def render_tree(scope_org=None) -> str:
    """The receipt: the two-axis Actual$ | Est-value$ TABLE, then the ORG → TEAM → PROJECT est-value tree (the
    attribution, matching the server). `scope_org` limits to one org (None = all orgs)."""
    t = tally()
    parts = [_PREFIX + "spend this month"]
    parts += [_INDENT + ln for ln in _two_axis_table(t)]
    tree = _est_tree(scope_org)
    if not tree:
        return "\n".join(parts)
    parts.append(_INDENT + "Est value $ by org → team → project (plan usage — NOT billed):")
    for org in sorted(tree, key=lambda o: -tree[o]["month"]):
        o = tree[org]
        parts.append(f"  ▸ {(org or '(unclassified)'):<24}{_k(o['month']):>9}/mo")
        for team in sorted(o["teams"], key=lambda x: -o["teams"][x]["month"]):
            tm = o["teams"][team]
            parts.append(f"      {(team or 'other'):<22}{_k(tm['month']):>9}")
            for proj, pm in sorted(tm["projects"].items(), key=lambda x: -x[1])[:6]:
                parts.append(f"         {(proj or '(untagged)'):<24}{_k(pm):>9}")
    return "\n".join(parts)


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
            print(json.dumps({"tally": tally(), "tree": _est_tree(None)}, indent=2))
            return 0
        if "--stop-hook" in args:
            # Claude Code Stop hook: `systemMessage` is the ONLY hook output the user sees — one global line.
            print(json.dumps({"systemMessage": render_line(tally())}))
            return 0
        if "--statusline" in args:
            # Claude Code statusLine: session JSON on stdin; prepend cwd · model · ctx% to the global one-line tally.
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
            print((prefix + "  ·  " if prefix else "") + render_line(tally()))
            return 0
        if "--line" in args:                          # compact one-line global tally (scripting)
            print(render_line(tally()))
            return 0
        # the ORG → TEAM → PROJECT tree: --all = every org · --org X = one org · default = the connection's org,
        # falling back to ALL when that org has no attributed value (its taxonomy org may differ from the conn org).
        scope = None if "--all" in args else (_arg("--org") if "--org" in args else _conv_org())
        if scope and not _est_tree(scope):
            scope = None
        print(render_tree(scope_org=scope))
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
        # SPENDGUARD_NO_AUTOINSTALL=1 → the read-only receipt skips patching the SDKs (0.6s → ~0.05s warm path)
        cfg["statusLine"] = {"type": "command",
                             "command": f"env SPENDGUARD_NO_AUTOINSTALL=1 {sg} receipt --statusline", "padding": 0}
        stop = cfg.setdefault("hooks", {}).setdefault("Stop", [])
        if not any(h.get("command", "").endswith("receipt --stop-hook")
                   for g in stop for h in (g.get("hooks") or [])):
            stop.append({"hooks": [{"type": "command",
                                    "command": f"env SPENDGUARD_NO_AUTOINSTALL=1 {sg} receipt --stop-hook",
                                    "timeout": 5}]})
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


if __name__ == "__main__":      # `python -m spendguard.receipt [--all|--org X|--line|--stop-hook|--statusline]`
    raise SystemExit(cli(sys.argv[1:]))
