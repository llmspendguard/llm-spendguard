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

# repo path → project tag (the WHAT this work belongs to). Override/extend via config workdone.repos.
DEFAULT_REPOS = {
    "~/Documents/claude/lmm": "lmm",
    "~/Documents/claude/llm-spendguard": "llmseg",
    "~/Documents/claude/llm-spendguard-server": "llmseg",
    "~/Documents/animepipe": "manga2anime",
}


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
        for day, intent, model, n in callio._db().execute(
            "SELECT substr(ts,1,10) d, COALESCE(NULLIF(intent,''),'(unlabeled)'), COALESCE(model,'?'), COUNT(*) "
            "FROM call_io WHERE ts >= ? GROUP BY d, intent, model", (since,)):
            proj = conv._project_of(intent) or conv._project_of(model) or ""
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
            by[(day, proj or "lmm")]["intents"][intent] += n
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


def cmd(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard workdone")
    ap.add_argument("--since", help="YYYY-MM-DD (default: start of this month)")
    ap.add_argument("--by", choices=["day", "week", "month"], default="day")
    ap.add_argument("--push", action="store_true", help="push the work-done roll-up to the server")
    a = ap.parse_args(argv or [])
    if a.push:
        from . import saas
        print("workdone push:", saas.push_workdone(since=a.since, by=a.by))
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
