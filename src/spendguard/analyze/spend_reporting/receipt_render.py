"""Pure receipt rendering and scope aggregation over tally data and live state."""
from __future__ import annotations

import json
import os
import pathlib
from typing import Optional

from .receipt_tally import (
    _asof_label as _asof_label,
    _basis_line as _basis_line,
    _est_by_source as _est_by_source,
    _est_cells as _est_cells,
    _est_tree as _est_tree,
    _lane_activity as _lane_activity,
    _money as _money,
    _pct as _pct,
    _plan_label as _plan_label,
    _quarantine_lines as _quarantine_lines,
    _receipt_est_records as _receipt_est_records,
    _routed_lane_value as _routed_lane_value,
    _tok as _tok,
    _truncated_lines as _truncated_lines,
    _unpriced_lines as _unpriced_lines,
    _windows as _windows,
    tally as tally,
)
from ... import config


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
        with open(path) as _f:                        # `with`, not a bare open(): the receipt runs every turn, so a
            for ln in _f:                             # leaked handle here accumulates against the file each session
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


def _lane_seg(t: dict) -> str:
    """Compact 'which plan served the work' segment: 'codex 12× · gemini 3×' — the subscription lanes that actually
    ran spendguard's own prompts this month ($0 billed). '' when none (or only pre-migration unknown-lane rows)."""
    la = t.get("lanes") or {}
    act = sorted(((ln, d) for ln, d in la.items() if ln and ln != "?"), key=lambda kv: -kv[1].get("calls", 0))
    return " · ".join(f"{ln} {d['calls']}×" for ln, d in act[:4])


def _queue_seg() -> str:
    """Durable lane-queue backlog for the receipt: 'N pending · M leased' when work is waiting to drain onto idle
    lanes, else '' (nothing queued). Guarded — a queue read never breaks the receipt."""
    try:
        from ... import lane_queue
        d = lane_queue.queue_depth() or {}
        bits = [f"{d[k]} {k}" for k in ("pending", "leased", "failed") if d.get(k)]
        return " · ".join(bits)
    except Exception:
        return ""


def _tally_lines(t: dict) -> list:
    """The running-tally line(s). HARD RULE: REAL $ (money out the door) is shown as NAMED components — API
    (per-token) + Subscription (flat plan fee) + Remote (GPU/compute) + External (MCP/tool + external APIs, only when
    present) — then est-value (plan usage, NOT billed) on a SEPARATE line after '::'. The two axes are never combined
    into one number (that mixed total is the confusion this exists to prevent)."""
    api = t.get("api") or t.get("actual") or {}
    rem = t.get("remote") or {}
    ext = t.get("external") or {}
    sub = t.get("subscription") or 0
    am, rm, xm = api.get("month"), rem.get("month"), ext.get("month")
    real = (am or 0) + (sub or 0) + (rm or 0) + (xm or 0)
    parts = [f"API {_money(am)}"]
    if sub:
        parts.append(f"subs {_money(sub)}{'*' if t.get('subscription_assumed') else ''}")
    parts.append(f"remote {_money(rm)}" if rm is not None else "remote —")
    if xm:                                             # external (MCP/tool/API) — shown only when there IS such spend
        parts.append(f"external {_money(xm)}")
    extra = "" if (am is None) else f"  (billed; API today {_money(api.get('today'))} · 7d {_money(api.get('week'))})"
    lines = [f"real $ this month: {_money(real)}  =  " + "  +  ".join(parts) + extra]
    ev = t.get("est_value")
    if ev:
        asof = _asof_label(ev)
        mult = f"  →  {t['plan_mult']:.0f}× the subscription" if t.get("plan_mult") else ""
        lines.append(f":: est sub value (plan usage, NOT billed){asof}: month {_money(ev.get('month'))}"
                     f" · today {_money(ev.get('today'))} · 7d {_money(ev.get('week'))}{mult}")
    cache_line = _cache_split_line(t)
    if cache_line:
        lines.append(cache_line)
    seg = _lane_seg(t)
    if seg:                                        # WHICH plan served spendguard's own work — the inline lane visibility
        lines.append(f":: lanes serving your work (mo): {seg}  ($0 billed — a plan served these, not the metered API)")
    try:
        import spendguard.manage.admission.admission_accessors as plan_admission
        risky = [r for r in plan_admission.resolve_risks() if r.get("at_risk")]
        for r in risky:
            why = "PAID OVERAGE" if r.get("paid_overage") else f"{r.get('remaining_pct')}% remaining"
            lines.append(f":: PLAN RISK 🔴 {r['lane']} {why} — discretionary calls reroute or refuse")
    except Exception:
        lines.append(":: PLAN RISK ⚪ UNKNOWN — admission state unavailable")
    gb = _gate_blocks_line()
    if gb:
        lines.append(gb)
    ha = _lane_health_alert()                          # a lane/API down (from the last check) surfaces in EVERY receipt
    if ha:
        lines.append(ha)
    return lines


def _cache_split_line(t: dict) -> str:
    """The monthly input-cache axes, kept named and separate; absence is stated instead of invented as 0%/0%."""
    cache = t.get("input_cache") or {}
    if not (cache.get("cache_read_tok") or cache.get("cache_write_tok")):
        return ":: input cache split (mo): no measured token data for this scope"
    return ":: input cache split (mo): read %s (%.1f%%) · write %s (%.1f%%) · total input %s" % (
        _tok(cache.get("cache_read_tok")), 100 * cache.get("cache_read_share", 0),
        _tok(cache.get("cache_write_tok")), 100 * cache.get("cache_write_share", 0),
        _tok(cache.get("input_tok")))


def _lane_health_alert():
    """The lane-health alert line (a lane/API is UNREACHABLE per the last reliability check), or None. $0 — reads
    the cached health state, so a red lane surfaces in every conversation's receipt without a new sweep. NEVER
    raises: a receipt must not break over a health read."""
    try:
        from ... import reliability
        return reliability.health_alert()
    except Exception:
        return None


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
    """One compact status-bar line — REAL $ (API + subs + remote + external) then est-value, SEPARATE, never summed."""
    t = t or tally()
    api = t.get("api") or t.get("actual") or {}
    rem = t.get("remote") or {}
    ext = t.get("external") or {}
    sub = t.get("subscription") or 0
    real = (api.get("month") or 0) + (sub or 0) + ((rem.get("month")) or 0) + ((ext.get("month")) or 0)
    scope = f"[{t['scope']}] " if t.get("scope") else ""
    bits = [f"API {_k(api.get('month'))}"]
    if sub:
        bits.append(f"subs {_k(sub)}")
    if rem.get("month") is not None:
        bits.append(f"remote {_k(rem.get('month'))}")
    if ext.get("month"):                            # external (MCP/tool/API) — only when there IS such spend
        bits.append(f"external {_k(ext.get('month'))}")
    s = f"◈ {scope}real {_k(real)}/mo = " + " + ".join(bits)
    ev = t.get("est_value")
    if ev:
        s += f"  ::  est value {_k(ev.get('month'))}/mo"
    cache = t.get("input_cache") or {}
    if cache.get("input_tok"):
        s += "  ::  cache read %.1f%% · write %.1f%%" % (100 * cache.get("cache_read_share", 0),
                                                           100 * cache.get("cache_write_share", 0))
    seg = _lane_seg(t)
    if seg:                                        # the every-turn Stop-hook line: name the plan(s) that served work
        s += f"  · lanes: {seg} ($0)"
    if t.get("gaps"):
        s += "  · " + " | ".join(t["gaps"])
    return s


def _conv_repos(conv=None, cwd=None):
    """The repo(s) THIS conversation is relevant to (collapsed view): the cwd repo + any the conversation touched."""
    repos = set()
    try:
        import spendguard.manage.admission.admission_accessors as budget
        repos |= set(budget.resolve_projects_for_conv(conv or budget.resolve__conv()))
        cp = _project_for_cwd(cwd) if cwd else (budget.resolve__project() or None)
        if cp:
            repos.add(cp)
    except Exception:
        pass
    return sorted(r for r in repos if r)


def _all_repos():
    """Every repo with billed charges OR plan-value (expanded view)."""
    repos = set()
    try:
        import spendguard.manage.admission.admission_accessors as budget
        repos |= set(budget.resolve_all_projects())
    except Exception:
        pass
    try:
        for srec in _receipt_est_records().values():
            # stamp_est_value writes "cells" (keyed org|team|project), NEVER a "repos" key — so the old
            # srec.get("repos") was always empty and every repo tracked ONLY via plan value silently vanished from
            # the --all rollups. A repo is a cell's TEAM or PROJECT (same axis convention as _est_tally(repo=)).
            for cell in (srec.get("cells") or {}).values():
                for _axis in ("team", "project"):
                    _v = (cell.get(_axis) or "").strip().lower()
                    if _v:
                        repos.add(_v)
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
        from ...egress import saas_accessors
        return (saas_accessors.resolve_saas_connection().get("org") or "").strip().lower() or None
    except Exception:
        return None


def _two_axis_table(t: dict) -> list:
    """The two axes as a TABLE — `Actual $` and `Est value $` in SEPARATE columns. Each row sits in ONE column (the
    other shows —); the columns total INDEPENDENTLY and are NEVER added into one number. This is the hard rule: an
    est-value row is $0 in the Actual column, so the two can never be silently mixed."""
    api = (t.get("api") or {}).get("month")
    rem = (t.get("remote") or {}).get("month")
    ext = (t.get("external") or {}).get("month")   # non-token external (MCP/tool/API) — a 4th REAL-$ component
    sub = t.get("subscription") or 0
    ev = t.get("est_value") or {}
    evm = ev.get("month")
    asof = _asof_label(ev)
    cell = lambda x: (_money(x) if x is not None else "—")
    LW = 34
    # An ASSUMED plan fee is not an "Actual $" — it's a default nobody confirmed. The tally already carries
    # `subscription_assumed`; this renderer used to DROP it and print $400 as fact under the Actual column (and
    # label it "Max + Pro" even for a single $20 plan). Mark it, footnote it, and taint the TOTAL the same way.
    assumed = bool(t.get("subscription_assumed")) and (sub or 0) > 0
    star = "*" if assumed else ""
    rows = [("API (batch + realtime)", api, None),
            ("Remote compute (vast.ai)", rem, None)]
    if ext:                                        # only when there IS external spend — keeps the common table clean
        rows.append(("External (MCP · tool · APIs)", ext, None))
    rows += [(_plan_label() + star, (sub or None), None),
             ("Plan usage (Claude Code·Codex·ai)", None, evm)]
    out = [f"{'':<{LW}}{'Actual $':>12}{'Est value $':>14}    ← two axes, never added"]
    for label, a, e in rows:
        out.append(f"{label:<{LW}}{cell(a):>12}{cell(e):>14}")
    out.append("─" * (LW + 26))
    total = f"{_money((api or 0) + (rem or 0) + (sub or 0) + (ext or 0))}{star}"
    out.append(f"{'TOTAL':<{LW}}{total:>12}{cell(evm):>14}")
    # The as-of / STALE caption belongs to the EST-VALUE column only. Actual $ is read live from the ledger on
    # every render; hanging a staleness marker off TOTAL would smear the est-value cache's age onto numbers that
    # are current, which is its own kind of lie.
    if asof:
        out.append(f"Plan usage{asof}")
    if assumed:
        out.append(f"* subscription is an ASSUMED default ({_money(sub)}), not a measured charge — set yours with "
                   f"`spendguard config set subscription.plan_usd <amount>`")
    # Full est-value per lane, followed by WHICH lanes served spendguard's own routed work this month.
    activity = _lane_activity()
    by_lane = _routed_lane_value()
    # Routed calls are only the work spendguard dispatched, not the host lane's full ledger value.
    # The est-value source family total owns this display, including the explicit other bucket.
    for source, windows in _est_by_source().items():
        if source not in by_lane or by_lane[source] is not None:
            by_lane[source] = windows["month"]
    lanes_ev = sorted(by_lane.items(), key=lambda x: (x[1] is None, -(x[1] or 0), x[0]))
    if lanes_ev:
        out.append("Plan value by lane (mo): " + " · ".join(
            f"{s} {_money(m)}" if m is not None else f"{s} unknown" for s, m in lanes_ev))
    seg = _lane_seg(t)
    if seg:
        out.append(f"lanes serving spendguard's work (mo): {seg}  ($0 billed — plan-served, not the metered API)")
    qseg = _queue_seg()
    if qseg:
        out.append(f"lane queue: {qseg}  (drain onto idle lanes: spendguard lanes --drain)")
    out += _basis_line()
    out += _unpriced_lines()
    out += _truncated_lines()
    out += _quarantine_lines()
    return out


def _saved_lines(t: dict) -> list:
    """The THIRD axis — GUARDED SAVINGS (avoided $), shown SEPARATELY and NEVER added into Actual $ or Est value $.
    Measured sources (cache·block·cascade·realized) and counterfactual ones (advisor·compaction) are kept apart so a
    counterfactual is never blurred into a measured number, and a sanity ratio vs the (real + est-value) baseline
    FLAGS a vanity figure rather than hiding it. Empty when nothing has been guarded yet."""
    try:
        from ... import guard
        month = _windows()[2]
        s = guard.saved_since(month)
    except Exception:
        return []
    if not s or (s.get("total") or 0) <= 0:
        return []
    ev = (t.get("est_value") or {}).get("month") or 0
    baseline = (t.get("real_month") or ((t.get("api") or {}).get("month") or 0)) + ev
    try:
        cx = guard.savings_crosscheck(baseline, since=month)
    except Exception:
        cx = {}
    out = ["guarded savings this month (avoided $ — a 3rd axis, NEVER added to Actual or Est value):"]
    if s.get("certain"):
        out.append(f"   measured (cache·prompt-cache·block·cascade·realized)   {_money(s['certain'])}")
        _pc = (s.get("by_source") or {}).get("prompt_cache")
        if _pc:                                    # name the prompt-cache slice when present (system-prompt cache reads)
            out.append(f"     └ prompt-cache (system read at the cache discount)   {_money(_pc)}")
    if s.get("counterfactual"):
        out.append(f"   counterfactual (advisor·compaction)       {_money(s['counterfactual'])}  (estimated)")
    # ratio vs the (real + est-value) baseline is shown as a FACT, not judged by a threshold — the reader decides
    # whether a big number is legitimate (a blocked batch can dwarf a low month). The number is auditable by source.
    tail = f"   [{cx['ratio']}× the {_money(cx['baseline'])} baseline]" if cx.get("ratio") is not None else ""
    out.append(f"   Σ saved ~{_money(s['total'])}{tail}")
    return out


def render_tree(scope_org=None, project=None) -> str:
    """The receipt: the two-axis Actual$ | Est-value$ TABLE, then the ORG → TEAM → PROJECT est-value tree (the
    attribution, matching the server). `scope_org` limits to one org (None = all orgs)."""
    t = tally(project=project)
    scope_label = f" [{project}]" if project else ""
    parts = [_PREFIX + "spend this month" + scope_label]
    parts += [_INDENT + ln for ln in _two_axis_table(t)]
    parts += [_INDENT + gap for gap in t.get("gaps") or []]
    cache_line = _cache_split_line(t)
    if cache_line:
        parts.append(_INDENT + cache_line)
    parts += [_INDENT + ln for ln in _saved_lines(t)]      # 3rd axis: guarded savings (avoided $) — never summed in
    tree = _est_tree(scope_org, repo=project)
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
    if n or flow.get("in_tok") is not None or flow.get("out_tok") is not None:
        head += f" · in {_tok(flow.get('in_tok'))} / out {_tok(flow.get('out_tok'))}"
    est, act = flow.get("est"), flow.get("actual")
    incomplete_count = flow.get("incomplete_count") or 0
    if incomplete_count:
        act = None
    if est is not None:
        head += f" · est {_money(est)} → actual {_money(act)}{_pct(est, act)}"
    else:
        head += f" · actual {_money(act)}"
    if incomplete_count:
        head += (f" · INCOMPLETE: {incomplete_count} calls; known input-cost floor "
                 f"{_money(flow.get('input_cost_floor'))}; total cost unknown")
    # `t if t is not None`, NOT `t or tally()`: emit_flow passes a PROJECT-SCOPED tally; when that scope is legitimately
    # empty ({}), `t or tally()` discarded it and showed the UNSCOPED all-project totals instead. None = "no tally passed".
    parts = [head] + [_INDENT + ln for ln in _tally_lines(t if t is not None else tally())]
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
        from ... import learn
        fn = getattr(learn, "quick_tip", None)
        if fn:
            return fn(intent)
    except Exception:
        pass
    return None
