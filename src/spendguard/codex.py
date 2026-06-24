"""Codex adapter — mine ~/.codex/sessions/**/*.jsonl into est-value spend + work, INCREMENTALLY.

OpenAI Codex (Desktop / VSCode / CLI) meters each turn: `event_msg`→`token_count` events carry the CUMULATIVE
`total_token_usage` (input / cached_input / output / reasoning) and the model lives in `turn_context` (e.g.
`gpt-5.5`). On a ChatGPT/Codex plan (`rate_limits.plan_type`) those tokens are PLAN-COVERED, so this is usage
VALUE — what it WOULD cost at API rates, NOT $ billed — emitted **channel=codex, billed=false**, exactly like the
Claude Code (`claudecode.py`) and claude.ai (`chat.py`) adapters. It sums into the same est-value tally/receipt and
the same org→team×project attribution; it is NEVER added to actual-$ (the gate-ledger / provider-billed axis).

INCREMENTAL + idempotent: a per-session digest cached by file mtime (`state.sessions[path] = {mtime, digest}`);
unchanged sessions are skipped, a grown session is re-digested whole (sessions are small). The session total is the
FINAL `token_count` event's cumulative `total_token_usage` — so re-mining a growing session never double-counts.
Cost = pricing.realtime_cost(model, input_tokens, output_tokens, cached_input_tokens). Project = cwd basename.
"""
import os, json, glob, pathlib, datetime

from . import config, pricing

_DEFAULT_MODEL = "gpt-5.5"          # only a fallback if a session somehow has no turn_context.model


def _sessions_dir():
    return os.environ.get("SPENDGUARD_CODEX_DIR") or str(pathlib.Path.home() / ".codex" / "sessions")


def _state_path():
    return config.HOME / "codex_state.json"


def _load_state():
    try:
        return json.loads(_state_path().read_text())
    except Exception:
        return {"sessions": {}, "cls": {}}


def _save_state(st):
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps(st, indent=0))
    except Exception:
        pass


def load_cls():
    """Per-session classifications {sid: {org, team, project}} — public accessor (parity with claudecode.load_cls)."""
    return _load_state().get("cls", {})


def _project_of(cwd):
    """Bucket by the REPO (git-root basename), not the session's cwd — so subdirs collapse to the repo and match
    how actual-$ is tagged, instead of fragmenting est-value across many cwd names."""
    if not cwd:
        return "codex"
    return config.git_root_project(cwd) or os.path.basename(str(cwd).rstrip("/")).lower() or "codex"


def _digest(path):
    """Parse one Codex session jsonl → a digest dict, or None if it has no usage. Usage = the FINAL token_count
    event's cumulative total_token_usage; model = last turn_context.model; prompt = first user_message (seeds
    agentic classification — the cwd is only a PRIOR)."""
    sid = os.path.basename(path)
    cwd = None; day = None; model = None; usage = None; plan = None; prompt = None
    try:
        with open(path, "r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                t = o.get("type"); p = o.get("payload") or {}
                if t == "session_meta":
                    cwd = cwd or p.get("cwd")
                    day = day or (p.get("timestamp") or "")[:10]
                elif t == "turn_context":
                    if p.get("model"):
                        model = p["model"]              # last turn wins (model can change mid-session)
                    cwd = cwd or p.get("cwd")
                elif t == "event_msg":
                    st = p.get("type")
                    if st == "token_count":
                        tot = (p.get("info") or {}).get("total_token_usage")
                        if tot:
                            usage = tot                 # cumulative → the LAST one is the session total
                        pt = (p.get("rate_limits") or {}).get("plan_type")
                        if pt:
                            plan = pt
                    elif st == "user_message" and prompt is None:
                        m = p.get("message")
                        if isinstance(m, str) and m.strip():
                            prompt = m.strip()[:2000]
    except Exception:
        return None
    if not usage:
        return None
    day = day or datetime.date.today().isoformat()
    model = model or _DEFAULT_MODEL
    intok = int(usage.get("input_tokens") or 0)          # OpenAI semantics: input_tokens INCLUDES the cached subset
    cached = int(usage.get("cached_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)           # already includes reasoning_output_tokens
    try:
        cost = pricing.realtime_cost(model, intok, out, cached)
    except Exception:
        cost = 0.0
    return {"sid": sid, "day": day, "model": model, "cwd": cwd, "project": _project_of(cwd),
            "cost": round(cost, 6), "in_tok": intok, "out_tok": out, "cached_tok": cached,
            "plan": plan, "prompt": prompt}


def update(st=None):
    """Re-digest only the session files whose mtime changed (the incremental watermark). Returns (state, n_changed)."""
    st = st or _load_state()
    sess = st.setdefault("sessions", {})
    changed = 0
    for path in glob.glob(os.path.join(_sessions_dir(), "**", "*.jsonl"), recursive=True):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        prev = sess.get(path) or {}
        if prev.get("mtime") == mtime and "digest" in prev:
            continue
        sess[path] = {"mtime": mtime, "digest": _digest(path)}
        changed += 1
    if changed:
        _save_state(st)            # persist the watermark so a NEXT process run skips unchanged sessions
    return st, changed


def _session_digests(days=None, st=None):
    """Cached per-session digests (refreshes the mtime cache first). Filters to the last `days` and to ones with cost."""
    if st is None:
        st, _ = update()                                  # update() persists the watermark itself when it changes
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None
    out = []
    for rec in (st.get("sessions") or {}).values():
        d = rec.get("digest")
        if not d or d.get("cost", 0) <= 0:
            continue
        if cutoff and d.get("day") and d["day"] < cutoff:
            continue
        out.append(d)
    return out


def classify(run=False, days=None, recls=False):
    """Classify Codex sessions into org→team×project via the SHARED classifier + taxonomy (the cwd is a PRIOR the
    LLM confirms/overrides per session, incl. cross-org). Caged, estimate-first. Stored in state.cls; reused by
    day_totals/sync. Parity with claudecode.classify (same convergence: re-do unclassified or 0-confidence)."""
    from . import attribution
    st, _ = update()
    cls = st.setdefault("cls", {})
    todo = [d for d in _session_digests(days, st=st) if d.get("prompt")
            and (recls or not (cls.get(d["sid"]) or {}).get("confidence"))]
    if not todo:
        _save_state(st)
        print("codex: nothing to classify (run `codex show` to mine first; --reclassify to redo).")
        return 0
    taxo, _ = attribution.taxonomy()
    items = [{"id": d["sid"], "text": f"[{d['project']}] {d['prompt']}"} for d in todo]
    res = attribution.classify_items(items, taxo, run)
    if not run:
        return 0
    cls.update(res)
    _save_state(st)
    print(f"codex: classified {len(res)}/{len(todo)} sessions into org→team×project.")
    return 0


def day_totals(member_ref, org_label=None):
    """Per-(team, project, model, day) Codex rows → server (channel=codex, billed=false). Each session maps to its
    CLASSIFIED org→team×project (state.cls); org_label keeps only sessions whose classified org matches (or are
    unclassified). Mirrors claudecode.day_totals exactly so the server contract is identical."""
    st, _ = update()                                       # refresh the digest cache first (claudecode re-globs disk)
    cls = st.get("cls", {})
    agg = {}
    for d in _session_digests(st=st):
        a = cls.get(d["sid"])
        if a is None:
            if org_label:
                continue                                 # org-routed push: skip unclassified (no cross-org pollution)
            a = {}
        org = a.get("org", "")
        if org_label and org and org.lower() != org_label.lower():
            continue
        team = (a.get("team") or "").lower()
        proj = (a.get("project") or d["project"] or "codex").lower()
        model = d.get("model") or ""
        key = f"{team}|{proj}|{model}|{d['day']}"
        e = agg.setdefault(key, {"team": team, "project": proj, "model": model, "day": d["day"],
                                 "cost": 0.0, "in": 0, "out": 0, "cached": 0, "n": 0})
        e["cost"] += d["cost"]; e["in"] += d.get("in_tok", 0); e["out"] += d.get("out_tok", 0)
        e["cached"] += d.get("cached_tok", 0); e["n"] += 1
    return [{"day": e["day"], "provider": "openai", "model": e["model"], "kind": "workload",
             "channel": "codex", "billed": False, "spend_micros": round(e["cost"] * 1_000_000),
             "calls": e["n"], "in_tokens": e["in"], "out_tokens": e["out"], "cached_in_tokens": e["cached"],
             "member_ref": member_ref, "project": e["project"], "team": e["team"],
             "tags": ("team:" + e["team"]) if e["team"] else ""}
            for e in agg.values() if e["day"]]


def sync(dry=False):
    """Push Codex spend (channel=codex, billed=false) → the server, ORG-ROUTED by each session's classified org.
    Mirrors claudecode.sync."""
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
        return {"skipped": "no Codex spend for this connection's org"}
    try:
        return saas._request("POST", "/v1/ledger", {"visibility": c.get("visibility"), "day_totals": rows})
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/ledger endpoint yet"}
        raise


def show(days=None):
    """Local Codex USAGE-VALUE report by project, and stamp the est-value windows so `spendguard receipt` / the
    in-chat footer sum claude-code + claude-ai + codex."""
    digs = _session_digests(days)
    byproj = {}
    for d in digs:
        p = byproj.setdefault(d["project"], {"cost": 0.0, "sessions": 0, "models": set()})
        p["cost"] += d["cost"]; p["sessions"] += 1; p["models"].add(d["model"])
    total = sum(p["cost"] for p in byproj.values())
    # stamp from ALL digests (not the day-filtered view) so the month window is complete. billed=false → est-value.
    try:
        from . import receipt
        _cls = _load_state().get("cls", {})        # repo (git-root) = rollup; classified project = breakdown
        receipt.stamp_est_value(
            [{"day": d["day"], "spend_micros": round(d["cost"] * 1_000_000), "billed": False,
              "repo": d["project"], "project": (_cls.get(d["sid"]) or {}).get("project") or d["project"]}
             for d in _session_digests(None)],
            source="codex")
    except Exception:
        pass
    span = sorted(d["day"] for d in digs)
    rng = f"{span[0]} → {span[-1]} ({len(set(span))} days)" if span else "no data"
    n = len(digs)
    print(f"Codex USAGE VALUE — {n} sessions · {rng}{' · last %sd' % days if days else ' · ALL-TIME'}\n")
    print(f"  {'project':<22}{'value $':>10}{'sessions':>10}  models")
    for proj, p in sorted(byproj.items(), key=lambda x: -x[1]["cost"]):
        print(f"  {proj[:21]:<22}{('$%.2f' % p['cost']):>10}{p['sessions']:>10}  "
              f"{', '.join(sorted(m for m in p['models'] if m))[:34]}")
    print(f"\n  {'TOTAL VALUE':<22}{('$%.2f' % total):>10}")
    print("  ⚠ USAGE VALUE (tokens × API pricing), NOT $ billed — Codex on a ChatGPT/Codex plan is covered by the")
    print("    flat plan. `codex sync` pushes it as channel=codex, billed=false, so it stays OUT of actual spend.")
    return 0


def main(argv=None):
    argv = argv or []
    if "--rebuild" in argv:                        # re-bucket existing sessions after the git-root _project_of change
        st = _load_state(); st["sessions"] = {}; _save_state(st)
        print("codex: digest cache cleared — re-mining all sessions with repo-level (git-root) buckets")
        argv = [a for a in argv if a != "--rebuild"]
    sub = argv[0] if argv else "show"
    if sub == "sync":
        print("codex sync:", sync(dry="--dry" in argv))
        return 0
    days = None
    if "--days" in argv:
        try:
            days = int(argv[argv.index("--days") + 1])
        except (ValueError, IndexError):
            pass
    if sub == "classify":
        return classify(run="--run" in argv, days=days, recls="--reclassify" in argv)
    return show(days=days)
