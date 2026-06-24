"""Claude Code adapter — mine ~/.claude/projects/*.jsonl into spend + work-done, INCREMENTALLY.

Claude Code meters every turn (message.usage: input/output/cache tokens + model) and records the work (tool_use:
Edit/Write/Bash/…, the cwd→project, git branch). This reads those transcripts and turns them into the same ledger
rows the rest of spendguard uses — so Claude Code spend shows on the dashboard next to API + batch + GPU, and the
work shows in the work-done view, EVEN ON A SUBSCRIPTION (CC reports tokens regardless of how it's billed).

INCREMENTAL + idempotent (this is the "track what's analyzed, update only the new part" the user asked for):
  * Per-session WATERMARK (`state.sessions[path] = {lines, mtime}`) — only NEW lines since last run are read, so a
    growing conversation is re-mined cheaply and never double-counted.
  * A local per-(project, model, day) ACCUMULATOR (`state.ledger`) — new lines add to it; we push the FULL day
    totals, so the server upsert (keyed by row uid) stays correct as sessions grow.
Cost ≈ pricing.realtime_cost(model, input+cache_create+cache_read, output, cached=cache_read). Project = cwd name.
"""
import os, json, glob, pathlib, datetime

from . import config, pricing

_TOOL_FILE_KEYS = ("file_path", "path", "notebook_path")


def _projects_dir():
    return os.environ.get("SPENDGUARD_CC_DIR") or str(pathlib.Path.home() / ".claude" / "projects")


def _state_path():
    return config.HOME / "claudecode_state.json"


def _load_state():
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {"sessions": {}, "ledger": {}}


def _save_state(st):
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(st, indent=0))
    except Exception:
        pass


def load_cls():
    """Per-session classifications {sid: {org, team, project}} — public accessor for other modules (resources GPU
    alignment, the worklog) so they don't read claudecode's state file by hardcoded name."""
    return _load_state().get("cls", {})


def _project_of(cwd):
    """Bucket by the REPO (git-root basename), not the session's cwd — so subdirs (lmm/scripts/fanout) collapse to
    the repo (lmm) and match how actual-$ is tagged, instead of fragmenting est-value across dozens of cwd names."""
    if not cwd:
        return "claude-code"
    return config.git_root_project(cwd) or os.path.basename(str(cwd).rstrip("/")).lower() or "claude-code"


def _row_cost(model, u):
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    # COST uses the full breakdown (cache_read priced at the discounted cached rate). The RETURNED token split is
    # HONEST and un-lumped: in = new input + cache CREATION (both full-priced), cached = cache READ (discounted).
    # Claude Code re-reads the whole context every turn, so cr dominates — lumping it into `in` would report a
    # misleadingly huge "input" (20B+) when it's mostly cheap cache reads. Returns (cost, in, out, cached_read).
    try:
        return pricing.realtime_cost(model, inp + cc + cr, out, cr), inp + cc, out, cr
    except Exception:
        return 0.0, inp + cc, out, cr


def _scan_new_lines(path, from_line):
    """Yield parsed records from `from_line` onward. Returns (records, total_lines)."""
    recs, n = [], 0
    try:
        with open(path, "r", errors="ignore") as f:
            for n, line in enumerate(f, 1):
                if n <= from_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return recs, n


def update(st=None):
    """Read NEW transcript lines into the local accumulator (spend + work per project/model/day). Pure-ish: mutates
    + returns state; no network. Returns (state, summary-of-this-pass)."""
    st = st or _load_state()
    sessions = st.setdefault("sessions", {})
    ledger = st.setdefault("ledger", {})
    added_cost, added_lines, touched = 0.0, 0, 0
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        prev = sessions.get(path) or {"lines": 0, "mtime": 0}
        if mtime <= prev.get("mtime", 0) and prev.get("lines"):
            continue                                       # unchanged since last pass → skip (the watermark)
        recs, total = _scan_new_lines(path, prev.get("lines", 0))
        if not recs and total <= prev.get("lines", 0):
            sessions[path] = {"lines": total or prev.get("lines", 0), "mtime": mtime}
            continue
        touched += 1
        for r in recs:
            msg = r.get("message") or {}
            u = msg.get("usage") or {}
            model = msg.get("model")
            day = (r.get("timestamp") or "")[:10] or datetime.date.today().isoformat()
            proj = _project_of(r.get("cwd"))
            if u and model:
                cost, intok, outtok, crtok = _row_cost(model, u)
                key = f"{proj}|{model}|{day}"
                e = ledger.setdefault(key, {"project": proj, "model": model, "day": day,
                                            "cost": 0.0, "in_tok": 0, "out_tok": 0, "cached_tok": 0, "turns": 0})
                e["cost"] += cost; e["in_tok"] += intok; e["out_tok"] += outtok
                e["cached_tok"] = e.get("cached_tok", 0) + crtok; e["turns"] += 1   # .get: old state entries predate the field
                added_cost += cost
            content = msg.get("content")
            if isinstance(content, list):                   # work-done: tool usage + files touched
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        wkey = f"{proj}|work"
                        w = ledger.setdefault(wkey, {"project": proj, "_work": True, "tools": {}, "files": []})
                        w["tools"][b.get("name", "?")] = w["tools"].get(b.get("name", "?"), 0) + 1
                        inp = b.get("input") or {}
                        for fk in _TOOL_FILE_KEYS:
                            if inp.get(fk):
                                fn = os.path.basename(str(inp[fk]))
                                if fn not in w["files"]:
                                    w["files"].append(fn)
        added_lines += (total - prev.get("lines", 0))
        sessions[path] = {"lines": total, "mtime": mtime}
    return st, {"sessions_updated": touched, "new_lines": added_lines, "new_cost": round(added_cost, 4)}


def show(days=None):
    st, passinfo = update()
    _save_state(st)
    cutoff = None
    if days:
        cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    spend = [v for v in st["ledger"].values() if not v.get("_work") and (not cutoff or v["day"] >= cutoff)]
    work = [v for v in st["ledger"].values() if v.get("_work")]
    # Stamp the est-value windows (from the FULL ledger, not the day-filtered `spend`) so `spendguard receipt` and the
    # in-chat footer can show plan-usage cheaply. billed=false → it stays out of actual-$; channel keyed so claude.ai
    # doesn't clobber it. Best-effort.
    try:
        from . import receipt
        receipt.stamp_est_value(
            [{"day": v["day"], "spend_micros": round(v["cost"] * 1_000_000), "billed": False,
              "project": v.get("project")}
             for v in st["ledger"].values() if not v.get("_work") and v.get("day")],
            source="claude-code")
    except Exception:
        pass
    byproj = {}
    for r in spend:
        p = byproj.setdefault(r["project"], {"cost": 0.0, "turns": 0, "models": set()})
        p["cost"] += r["cost"]; p["turns"] += r["turns"]; p["models"].add(r["model"])
    total = sum(p["cost"] for p in byproj.values())
    span = sorted(r["day"] for r in spend)
    rng = f"{span[0]} → {span[-1]} ({len(set(span))} days)" if span else "no data"
    print(f"Claude Code USAGE VALUE — {len(st['sessions'])} sessions · {rng}{' · last %sd' % days if days else ' · ALL-TIME'}\n")
    print(f"  {'project':<22}{'value $':>10}{'turns':>9}  models")
    for proj, p in sorted(byproj.items(), key=lambda x: -x[1]["cost"]):
        wk = next((w for w in work if w["project"] == proj), None)
        print(f"  {proj[:21]:<22}{('$%.2f' % p['cost']):>10}{p['turns']:>9}  {', '.join(sorted(m for m in p['models'] if m))[:40]}")
        if wk:
            tools = ", ".join(f"{k}×{v}" for k, v in sorted(wk["tools"].items(), key=lambda x: -x[1])[:5])
            print(f"  {'':<22}└ work: {tools}  ·  {len(wk['files'])} files touched")
    print(f"\n  {'TOTAL VALUE':<22}{('$%.2f' % total):>10}")
    print("  ⚠ this is USAGE VALUE (tokens × API pricing) — what it WOULD cost at API rates, NOT $ billed. On a")
    print("    subscription it's covered by the flat plan: \"~$X of value for your $Y/mo plan\". `claude-code sync`")
    print("    pushes it as channel=claude-code, billed=false, so the dashboard keeps it OUT of actual spend.")
    return 0


def _session_digests(days=None):
    """Per-SESSION digests (the cwd is an umbrella, so each session is classified on its own content)."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    out = []
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        d = _digest(path)
        if d["cost"] <= 0 and not d["tools"]:
            continue
        if cutoff and d["day"] and d["day"] < cutoff:
            continue
        d["sid"] = os.path.basename(path)[:48]
        out.append(d)
    return out


def classify(run=False, days=None, recls=False):
    """Classify Claude Code sessions into org→team×project via the SHARED classifier + taxonomy (NOT the cwd repo
    name — that's an umbrella). Caged, estimate-first. Stored per session in state.cls; reused by day_totals/sync."""
    from . import attribution
    st = _load_state()
    cls = st.setdefault("cls", {})
    # Re-classify a session if it's unclassified OR its cached confidence is 0/missing. A 0 means it was never given a
    # real confidence (stale cache from before confidence-capture, or a genuinely low-confidence read) → the
    # convergence loop re-does it, so CC attributions never silently sit at confidence 0 (the chat path always has one).
    todo = [d for d in _session_digests(days) if d.get("prompt")
            and (recls or not (cls.get(d["sid"]) or {}).get("confidence"))]
    if not todo:
        print("claude-code: nothing to classify (run `claude-code show` to mine first; --reclassify to redo).")
        return 0
    taxo, _ = attribution.taxonomy()
    items = [{"id": d["sid"], "text": f"[{d['project']}] {d['prompt']}"} for d in todo]
    res = attribution.classify_items(items, taxo, run)
    if not run:
        return 0
    cls.update(res)
    _save_state(st)
    print(f"claude-code: classified {len(res)}/{len(todo)} sessions into org→team×project.")
    return 0


def day_totals(member_ref, org_label=None):
    """Per-(team, project, model, day) CC rows → server (channel=claude-code, billed=false). Each session maps to its
    CLASSIFIED org→team×project (state.cls); `team` rides along for org→team scope attribution. org_label keeps only
    sessions whose classified org matches (or are unclassified) — for org-routed push."""
    st = _load_state()
    cls = st.get("cls", {})
    agg = {}
    for d in _session_digests():
        if d["cost"] <= 0:
            continue
        a = cls.get(d["sid"])
        if a is None:
            if org_label:                                  # org-routed push: skip unclassified (avoid cross-org pollution)
                continue
            a = {}                                         # local view: include with cwd fallback (no team)
        org = a.get("org", "")
        if org_label and org and org.lower() != org_label.lower():
            continue
        team = (a.get("team") or "").lower()
        proj = (a.get("project") or d["project"] or "claude-code").lower()
        model = d.get("model") or ""
        key = f"{team}|{proj}|{model}|{d['day']}"
        e = agg.setdefault(key, {"team": team, "project": proj, "model": model, "day": d["day"],
                                 "cost": 0.0, "in": 0, "out": 0, "cached": 0, "n": 0})
        e["cost"] += d["cost"]; e["in"] += d.get("in_tok", 0); e["out"] += d.get("out_tok", 0)
        e["cached"] += d.get("cached_tok", 0); e["n"] += 1
    return [{"day": e["day"], "provider": "anthropic", "model": e["model"], "kind": "workload",
             "channel": "claude-code", "billed": False, "spend_micros": round(e["cost"] * 1_000_000),
             "calls": e["n"], "in_tokens": e["in"], "out_tokens": e["out"], "cached_in_tokens": e["cached"],
             "member_ref": member_ref, "project": e["project"], "team": e["team"],
             "tags": ("team:" + e["team"]) if e["team"] else ""}
            for e in agg.values() if e["day"]]


def sync(dry=False):
    """Push Claude Code spend (channel=claude-code) → the server. Honors visibility + contributor; ORG-ROUTED by the
    session's classified org (only rows whose org matches THIS connection — or are unclassified — push here)."""
    from . import saas
    c = saas.conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private"}
    cok, cwhy = saas.contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    rows = day_totals(saas.contributor(), org_label=c.get("org"))
    for r in rows:
        r["uid"] = saas._row_uid(r)
    if dry:
        return {"day_totals": rows}
    if not rows:
        return {"skipped": "no Claude Code spend for this connection's org"}
    try:
        return saas._request("POST", "/v1/ledger", {"visibility": c.get("visibility"), "day_totals": rows})
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/ledger endpoint yet"}
        raise


def _iso_period(day, by):
    from . import attribution
    return attribution.iso_period(day, by)   # shared (day/week/month/quarter/ytd) — was a local copy missing 'ytd'


def _digest(path):
    """Full per-session digest = a WORK ROW: project, primary day, models, value$, turns, tools, files, and the
    first user prompt (what was ASKED — the 'what the spend was for'). Re-reads the whole session (on-demand)."""
    proj = None; days = {}; models = set(); cost = 0.0; turns = 0; tools = {}; files = []; prompt = ""; branch = ""
    in_tok = out_tok = cached_tok = 0; modelcost = {}
    recs, _ = _scan_new_lines(path, 0)
    for r in recs:
        if proj is None and r.get("cwd"):
            proj = _project_of(r.get("cwd"))
        if not branch and r.get("gitBranch"):
            branch = r.get("gitBranch")
        day = (r.get("timestamp") or "")[:10]
        msg = r.get("message") or {}
        if not prompt and (r.get("type") == "user" or msg.get("role") == "user"):
            c = msg.get("content")
            t = c if isinstance(c, str) else (" ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text") if isinstance(c, list) else "")
            t = (t or "").strip().replace("\n", " ")
            if t and not t.startswith("<") and "tool_result" not in t[:40] and "[Request interrupted" not in t:
                prompt = t[:200]
        u = msg.get("usage") or {}; model = msg.get("model")
        if u and model:
            cu, ai, bo, cr = _row_cost(model, u); cost += cu; turns += 1; models.add(model)
            in_tok += ai; out_tok += bo; cached_tok += cr; modelcost[model] = modelcost.get(model, 0) + cu
            if day:
                days[day] = days.get(day, 0) + cu
        c = msg.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    tools[b.get("name", "?")] = tools.get(b.get("name", "?"), 0) + 1
                    inp = b.get("input") or {}
                    for fk in _TOOL_FILE_KEYS:
                        if inp.get(fk) and os.path.basename(str(inp[fk])) not in files:
                            files.append(os.path.basename(str(inp[fk])))
    primary = max(days, key=days.get) if days else ((recs[0].get("timestamp") or "")[:10] if recs else "")
    dominant = max(modelcost, key=modelcost.get) if modelcost else (sorted(models)[0] if models else "")
    return {"project": proj or "claude-code", "day": primary, "models": sorted(models), "cost": round(cost, 4),
            "turns": turns, "tools": tools, "files": files, "prompt": prompt, "branch": branch,
            "in_tok": in_tok, "out_tok": out_tok, "cached_tok": cached_tok, "model": dominant}


def work(by="week", days=None):
    """Conversation-derived WORK DONE — per-session rows (what was asked + cost) bucketed by day/week/month/quarter."""
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    digs = []
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        d = _digest(path)
        if d["cost"] <= 0 and not d["tools"]:
            continue
        if cutoff and d["day"] and d["day"] < cutoff:
            continue
        digs.append(d)
    buckets = {}
    for d in digs:
        p = _iso_period(d["day"], by)
        b = buckets.setdefault(p, {"value": 0.0, "sessions": 0, "rows": []})
        b["value"] += d["cost"]; b["sessions"] += 1; b["rows"].append(d)
    print(f"WORK DONE — by {by}{' · last %sd' % days if days else ''} · from Claude Code conversations (value = usage $)\n")
    for p in sorted(buckets, reverse=True):
        b = buckets[p]
        print(f"  ▸ {p}  —  ${b['value']:.2f} value · {b['sessions']} sessions")
        for d in sorted(b["rows"], key=lambda x: -x["cost"])[:8]:
            tl = " · ".join(f"{k}×{v}" for k, v in sorted(d["tools"].items(), key=lambda x: -x[1])[:3])
            print(f"     {('$%.2f' % d['cost']):>8}  {d['project'][:13]:<14} {(d['prompt'] or '(no prompt captured)')[:66]}")
            if tl:
                print(f"     {'':>8}  {'':<14} └ {tl} · {len(d['files'])} files")
        print()
    print("  ↑ per-session ROWS (what was asked + $). NEXT: a caged LLM 'story' synthesis per period + push to the dashboard.")
    return 0


def _toklen(s):
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(s))
    except Exception:
        return max(1, len(s) // 4)


_STORY_SYS = (
    "You turn a developer's AI-assisted work SESSIONS into a WORK LOG. Each session line is: [project] what was "
    "asked | tools used | files. Output STRICT JSON only (no prose outside it):\n"
    '{"story": "<3-5 sentence first-person-plural narrative of what got DONE this period — concrete, no fluff, '
    'no activity counts>",\n'
    ' "insights": [{"type": "finding|decision|gotcha|next", "project": "<proj>", "text": "<a WORK/domain insight: '
    'something LEARNED, a DECISION made, a GOTCHA discovered, or a NEXT step — about the work itself, NOT about '
    'how to use LLMs/cost better>"}]}\n'
    "Give 3-8 insights, substance over activity (what we now KNOW). These are the org's private knowledge.")


def story(by="week", days=7, run=False):
    """Caged synth over the period's work rows → a narrative STORY + private WORK INSIGHTS (findings/decisions/
    gotchas/next — distinct from cost/LLM-usage learnings). Estimate-first; the LLM call is caged under caps.meta."""
    from . import config, adapters, calls, pricing, ui
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    digs = []
    for path in sorted(glob.glob(os.path.join(_projects_dir(), "**", "*.jsonl"), recursive=True)):
        d = _digest(path)
        if (d["cost"] > 0 or d["tools"]) and (not cutoff or not d["day"] or d["day"] >= cutoff):
            digs.append(d)
    if not digs:
        print("no sessions in range — nothing to synthesize."); return 0
    lines = []
    for d in sorted(digs, key=lambda x: -x["cost"])[:40]:
        tl = ",".join(f"{k}×{v}" for k, v in sorted(d["tools"].items(), key=lambda x: -x[1])[:4])
        lines.append(f"- [{d['project']}] {(d['prompt'] or '(no prompt)')[:160]} | tools: {tl} | {len(d['files'])} files")
    prompt = f"Sessions ({len(digs)}, last {days}d):\n" + "\n".join(lines)
    model = config.advisor_model()
    OUT = 1500
    est = pricing.realtime_cost(model, _toklen(_STORY_SYS + prompt), OUT)
    print(f"work story + insights — {model} (caged under caps.meta ${config.meta_cap():.2f}/day)")
    print(f"  ESTIMATE (zero paid calls): {len(digs)} sessions · in~{_toklen(_STORY_SYS + prompt):,} out≤{OUT} -> ~${est:.4f}")
    if not run:
        ui.estimate_only(action="synthesize the work story + private insights", cost=est)
        return 0
    with calls.context(intent="spendguard:worklog"):     # caged → meta budget, excluded from the workload corpus
        r = adapters.call(model, prompt, max_tokens=OUT, system=_STORY_SYS)
    if r.get("error"):
        print("  error:", r["error"]); return 1
    from .chat import _parse_story                         # tolerant parse (recovers story + insights if truncated)
    data = _parse_story(r.get("text", ""))
    print("\n=== WORK STORY ===\n" + (data.get("story") or r.get("text", "")[:800]))
    print("\n=== WORK INSIGHTS (private — your IP, never pooled) ===")
    for ins in (data.get("insights") or []):
        print(f"  [{ins.get('type', '?'):<8}] ({ins.get('project', '?')}) {ins.get('text', '')}")
    print(f"\n  (caged cost ${r.get('cost', 0):.4f}; intent spendguard:worklog)")
    return 0


def main(argv=None):
    argv = argv or []
    if "--rebuild" in argv:                        # re-bucket: clear the accumulator + watermarks, re-mine at repo level
        st = _load_state(); st["ledger"] = {}; st["sessions"] = {}; _save_state(st)
        print("claude-code: state reset — re-mining all transcripts with repo-level (git-root) buckets")
        argv = [a for a in argv if a != "--rebuild"]
    sub = argv[0] if argv else "show"
    if sub == "sync":
        print("claude-code sync:", sync(dry="--dry" in argv))
        return 0
    days = None
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (ValueError, IndexError):
            pass
    by = "week"
    if "--by" in argv:
        try:
            by = argv[argv.index("--by") + 1]
        except IndexError:
            pass
    if sub == "classify":                               # classify sessions into org→team×project (caged, est-first)
        return classify(run="--run" in argv, days=days, recls="--reclassify" in argv)
    if sub == "work":                                   # conversation-derived work rows, bucketed by period
        return work(by=by, days=days)
    if sub == "story":                                  # caged narrative + private work-insights (estimate-first)
        return story(by=by, days=days or 7, run="--run" in argv)
    return show(days=days)
