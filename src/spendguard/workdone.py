"""Work-done layer — the CONTEXT for spend. Aggregates, per day/week/month and per project, the WORK that was
accomplished, so spend reads as "spent $X, and here's what got done." Pairs with the ledger on the same day axis.

TIER 1 (this module, FREE + deterministic): git commit subjects across the known repos + the intents/counts of
LLM batches run (the local call corpus). No diffs, no prompts — just commit subjects + intent labels + counts.
TIER 2 (separate, caged + opt-in): an LLM pass synthesizes a day's raw activity into a readable narrative
("Built the per-row UID layer; reconciled GPU attribution") — estimate-first under caps.meta, never auto-runs.

  spendguard workdone               # this month, per day · project
  spendguard workdone --since 2026-06-01 --by week
"""
import argparse, datetime, os, subprocess
from collections import defaultdict

# repo path → project tag (the WHAT this work belongs to). Repos MUST be configured via config workdone.repos
# (mapping absolute repo paths → project tags); there are no machine-specific defaults.
DEFAULT_REPOS = {}


def _repos():
    from . import config
    try:
        m = (config.saas_config().get("workdone") or {}).get("repos")
    except Exception:
        m = None
    return {os.path.expanduser(k): v for k, v in (m or DEFAULT_REPOS).items()}


def _git_commits(repo, since):
    """[(day, subject)] for commits since `since` in `repo` (empty if not a repo / git missing)."""
    try:
        out = subprocess.run(["git", "-C", repo, "log", f"--since={since}", "--date=short", "--pretty=%cd|%s"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode != 0:
            return []
        rows = []
        for line in out.stdout.splitlines():
            if "|" in line:
                day, subj = line.split("|", 1)
                rows.append((day.strip(), subj.strip()))
        return rows
    except Exception:
        return []


def _batch_intents(since):
    """{(day, project): {intent: count}} from the local call corpus — what LLM work actually ran each day."""
    out = defaultdict(lambda: defaultdict(int))
    try:
        from . import callio, conv
        bmap = conv.batch_project_map()       # AGENTIC: batch → its subconversation's classified project (no regex)
        for day, intent, batch, n in callio._db().execute(
            "SELECT substr(ts,1,10) d, COALESCE(NULLIF(intent,''),'(unlabeled)'), COALESCE(batch,''), COUNT(*) "
            "FROM call_io WHERE ts >= ? GROUP BY d, intent, batch", (since,)):
            proj = (bmap.get(batch, {}).get("project") or "")
            out[(day, proj)][intent] += n
    except Exception:
        pass
    return out


def build(since=None):
    """Per (day, project) work-done record: git commit subjects + batch intents + counts. Deterministic, free."""
    since = since or datetime.date.today().replace(day=1).isoformat()
    by = defaultdict(lambda: {"commits": [], "intents": defaultdict(int)})
    for repo, proj in _repos().items():
        for day, subj in _git_commits(repo, since):
            if day >= since:
                by[(day, proj)]["commits"].append(subj[:160])
    for (day, proj), intents in _batch_intents(since).items():
        for intent, n in intents.items():
            by[(day, proj or "")]["intents"][intent] += n
    rows = []
    for (day, proj), a in sorted(by.items()):
        rows.append({"day": day, "project": proj, "commits": a["commits"], "n_commits": len(a["commits"]),
                     "intents": dict(a["intents"]), "n_batch_calls": sum(a["intents"].values())})
    return rows


def _period(day, by):
    if by == "week":
        d = datetime.date.fromisoformat(day)
        return (d - datetime.timedelta(days=d.weekday())).isoformat()   # Monday of that week
    if by == "month":
        return day[:7]
    return day


def rollup(since=None, by="day"):
    """Roll the per-day records up to day | week | month, per project."""
    agg = defaultdict(lambda: {"commits": [], "intents": defaultdict(int), "days": set()})
    for r in build(since):
        k = (_period(r["day"], by), r["project"])
        a = agg[k]
        a["commits"].extend(r["commits"])
        a["days"].add(r["day"])
        for i, n in r["intents"].items():
            a["intents"][i] += n
    out = []
    for (period, proj), a in sorted(agg.items()):
        out.append({"period": period, "project": proj, "active_days": len(a["days"]),
                    "n_commits": len(a["commits"]), "commits": a["commits"],
                    "n_batch_calls": sum(a["intents"].values()), "intents": dict(a["intents"])})
    return out


# ── TIER 2: the caged "what was accomplished" SUMMARY. A small LLM turns the local work signals (commit subjects +
# intent counts — content stays on-device) into 2-3 factual sentences per project. CAGED by caps.meta (intent
# spendguard:*), ESTIMATE-FIRST (default dry; --run to spend). Only the scrubbed SUMMARY is pushed, never the
# signals. The system prompt forbids secrets/paths/identities; the gate's denylist + the server re-validate.
_META = "spendguard"
_SUMMARY_SYS = ("You write a 2-3 sentence, factual, past-tense summary of what was ACCOMPLISHED on a software "
    "project this period, from work signals (commit subjects + LLM-task intents and counts). Describe the WORK and "
    "its outcome. ABSOLUTELY NO secrets, API keys, file paths, URLs, person names, emails, or other PII — describe "
    "what was built, not who or where. If the signals are thin, say so in one line. Output only the summary prose.")
_SUMMARY_OUT = 220


def _summary_prompt(project, commits, intents):
    """PURE: the per-project prompt from local work signals. Testable; no I/O."""
    top = sorted((intents or {}).items(), key=lambda x: -x[1])[:12]
    lines = [f"Project: {project}"]
    if commits:
        lines.append(f"Commit subjects ({len(commits)}):")
        lines += [f"- {str(c)[:160]}" for c in list(commits)[:30]]
    if top:
        lines.append("LLM task intents (intent × count): " + ", ".join(f"{k}×{v}" for k, v in top))
    lines.append("Summarize what was accomplished, 2-3 sentences.")
    return "\n".join(lines)


def _aggregate_by_project(since=None):
    """{project: {commits, intents}} for the period — the input to the summarizer (this month, all periods folded)."""
    by = {}
    for r in rollup(since=since, by="month"):
        p = r.get("project") or "(untagged)"
        e = by.setdefault(p, {"commits": [], "intents": {}})
        e["commits"].extend(r.get("commits") or [])
        for k, v in (r.get("intents") or {}).items():
            e["intents"][k] = e["intents"].get(k, 0) + v
    return {p: e for p, e in by.items() if e["commits"] or e["intents"]}


def _summaries_path():
    from . import config
    config.HOME.mkdir(parents=True, exist_ok=True)
    return config.HOME / "work_summaries.json"


def load_summaries():
    import json
    p = _summaries_path()
    try:
        return json.load(open(p)) if p.exists() else {}
    except Exception:
        return {}


def summarize(since=None, run=False, model=None):
    """Generate (or estimate) the scrubbed per-project work summary. Estimate-first: default returns a zero-spend
    {projects, est_usd, model}; with run=True it makes the CAGED calls (gate → caps.meta), caches {project: summary}
    to ~/.spendguard/work_summaries.json, and returns {summarized, model}. push_workdone attaches the cache."""
    import json
    from . import config, pricing
    model = model or config.advisor_model()
    byproj = _aggregate_by_project(since)
    prompts = {p: _summary_prompt(p, e["commits"], e["intents"]) for p, e in byproj.items()}
    est = round(sum(pricing.realtime_cost(model, len(pr) // 4 + 80, _SUMMARY_OUT) for pr in prompts.values()), 4)
    if not run:
        from . import ui
        ui.estimate_only(action=f"summarize {len(prompts)} projects' work (caged · caps.meta)", cost=est)
        return {"projects": len(prompts), "est_usd": est, "model": model}
    from . import calls, adapters
    out = {}
    with calls.context(intent=f"{_META}:worksummary"):
        for p, pr in prompts.items():
            r = adapters.call(model, pr, max_tokens=_SUMMARY_OUT, system=_SUMMARY_SYS)   # gate → meta cap
            txt = (r.get("text") or "").strip()
            if txt and not r.get("error"):
                out[p] = txt[:800]
    json.dump(out, open(_summaries_path(), "w"), indent=2)
    return {"projects": len(prompts), "summarized": len(out), "model": model, "est_usd": est}


def cmd(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard workdone")
    ap.add_argument("--since", help="YYYY-MM-DD (default: start of this month)")
    ap.add_argument("--by", choices=["day", "week", "month"], default="day")
    ap.add_argument("--push", action="store_true", help="push the work-done roll-up to the server")
    ap.add_argument("--summarize", action="store_true", help="generate the caged per-project 'what was accomplished' summary (ESTIMATE-ONLY unless --run)")
    ap.add_argument("--run", action="store_true", help="with --summarize: actually spend (caged by caps.meta)")
    a = ap.parse_args(argv or [])
    if a.summarize:
        print("workdone summarize:", summarize(since=a.since, run=a.run))
        return 0
    if a.push:
        from . import saas
        # push monthly periods regardless of the display --by, to match the dashboard's current-month view
        print("workdone push:", saas.push_workdone(since=a.since))
        return 0
    rows = rollup(since=a.since, by=a.by)
    print(f"WORK DONE — by {a.by}, per project (context for spend):")
    for r in rows:
        top = sorted(r["intents"].items(), key=lambda x: -x[1])[:3]
        intent_s = (" · LLM: " + ", ".join(f"{i}×{n}" for i, n in top)) if top else ""
        print(f"\n  {r['period']}  [{r['project'] or '?'}]  {r['n_commits']} commits, {r['active_days']} active day(s){intent_s}")
        for c in r["commits"][:8]:
            print(f"      • {c}")
        if r["n_commits"] > 8:
            print(f"      … +{r['n_commits'] - 8} more")
    return 0
