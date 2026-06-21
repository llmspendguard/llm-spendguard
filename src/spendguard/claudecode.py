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


def _project_of(cwd):
    if not cwd:
        return "claude-code"
    return os.path.basename(str(cwd).rstrip("/")) or "claude-code"


def _row_cost(model, u):
    inp = int(u.get("input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    cr = int(u.get("cache_read_input_tokens") or 0)
    cc = int(u.get("cache_creation_input_tokens") or 0)
    try:
        return pricing.realtime_cost(model, inp + cc + cr, out, cr), inp + cc + cr, out
    except Exception:
        return 0.0, inp + cc + cr, out


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
                cost, intok, outtok = _row_cost(model, u)
                key = f"{proj}|{model}|{day}"
                e = ledger.setdefault(key, {"project": proj, "model": model, "day": day,
                                            "cost": 0.0, "in_tok": 0, "out_tok": 0, "turns": 0})
                e["cost"] += cost; e["in_tok"] += intok; e["out_tok"] += outtok; e["turns"] += 1
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


def show():
    st, passinfo = update()
    _save_state(st)
    spend = [v for v in st["ledger"].values() if not v.get("_work")]
    work = [v for v in st["ledger"].values() if v.get("_work")]
    byproj = {}
    for r in spend:
        p = byproj.setdefault(r["project"], {"cost": 0.0, "turns": 0, "models": set()})
        p["cost"] += r["cost"]; p["turns"] += r["turns"]; p["models"].add(r["model"])
    total = sum(p["cost"] for p in byproj.values())
    print(f"Claude Code spend (from {st and len(st['sessions'])} sessions; +{passinfo['new_lines']} new lines this pass):\n")
    print(f"  {'project':<22}{'$ spend':>10}{'turns':>9}  models")
    for proj, p in sorted(byproj.items(), key=lambda x: -x[1]["cost"]):
        wk = next((w for w in work if w["project"] == proj), None)
        print(f"  {proj[:21]:<22}{('$%.2f' % p['cost']):>10}{p['turns']:>9}  {', '.join(sorted(m for m in p['models'] if m))[:40]}")
        if wk:
            tools = ", ".join(f"{k}×{v}" for k, v in sorted(wk["tools"].items(), key=lambda x: -x[1])[:5])
            print(f"  {'':<22}└ work: {tools}  ·  {len(wk['files'])} files touched")
    print(f"\n  {'TOTAL':<22}{('$%.2f' % total):>10}")
    print("  (spend ≈ tokens × canonical pricing; `claude-code sync` pushes it to the dashboard as channel=claude-code)")
    return 0


def day_totals(member_ref, project_filter=None):
    """Full per-(project, model, day) CC rows → server day_totals (channel=claude-code). Built from the FULL local
    accumulator so the server upsert is correct as sessions grow. project_filter (set) limits to a repo's project(s)."""
    st, _ = update()
    _save_state(st)
    out = []
    for r in st["ledger"].values():
        if r.get("_work") or r["cost"] <= 0:
            continue
        proj = (r["project"] or "").lower()
        if project_filter is not None and proj not in project_filter:
            continue
        out.append({"day": r["day"], "provider": "anthropic", "model": r["model"], "kind": "workload",
                    "channel": "claude-code", "spend_micros": round(r["cost"] * 1_000_000),
                    "calls": r["turns"], "in_tok": r["in_tok"], "out_tok": r["out_tok"],
                    "member_ref": member_ref, "project": proj})
    return out


def sync(dry=False):
    """Push Claude Code spend (channel=claude-code) → the server, like resources.sync. Honors visibility +
    contributor; filtered to this connection's project(s)."""
    from . import saas
    c = saas.conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private"}
    cok, cwhy = saas.contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    flt = saas._project_filter(c)
    rows = day_totals(saas.contributor(), project_filter=flt)
    for r in rows:
        r["uid"] = saas._row_uid(r)
    if dry:
        return {"day_totals": rows}
    if not rows:
        return {"skipped": "no Claude Code spend for this connection's project(s)"}
    try:
        return saas._request("POST", "/v1/ledger", {"visibility": c.get("visibility"), "day_totals": rows})
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/ledger endpoint yet"}
        raise


def main(argv=None):
    argv = argv or []
    sub = argv[0] if argv else "show"
    if sub == "sync":
        print("claude-code sync:", sync(dry="--dry" in argv))
        return 0
    return show()
