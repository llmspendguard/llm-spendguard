"""`spendguard scan` — the FIRST RUN. Zero config, zero keys, zero network, zero spend.

The old front door was `pip install` → `install-hook --venv` → `doctor` → edit your script → run it under the gated
interpreter: four steps and a mutation of the user's venv before a single number appeared. And `report`, the
obvious first command, does a live provider-billing pull that takes MINUTES on a fresh install — a first
impression that hangs is worse than none.

So: scan reads what a new user already HAS on disk — their Claude Code and Codex transcripts — and prints what
that work would cost at API rates. It is a presenter over `claudecode.update()` / `codex.update()` (the same
readers the sync path uses), not a second implementation.

Rules it must not break:
  • the TWO AXES stay separate — this is est PLAN VALUE, never summed with billed $, and it says so;
  • nothing leaves the machine: no network call, no LLM call, no key needed. Attribution here is the transcript's
    own cwd/project, NOT the LLM classifier (that is opt-in and lives in `chat`/`attribution`);
  • it never writes outside SPENDGUARD_HOME.
Designed to be the `uvx --from llm-spendguard spendguard scan` command: read-only, and safe to run on a laptop
you don't own.
"""
import datetime


def collect(days=None):
    """{source: {sessions, projects{name: usd}, models{name: usd}, days, total_usd}} from LOCAL transcripts only.
    Never raises for a missing source — a machine with no Codex simply reports zero for it."""
    out = {}
    for name, mod in (("Claude Code", "claudecode"), ("Codex", "codex")):
        rec = {"sessions": 0, "projects": {}, "models": {}, "days": set(), "total_usd": 0.0, "error": None}
        try:
            m = __import__(f"spendguard.{mod}", fromlist=["update"])
            st, _pass = m.update()
            try:
                m._save_state(st)                      # keep the watermark so the next scan is incremental
            except Exception:
                pass
            cutoff = ((datetime.date.today() - datetime.timedelta(days=int(days))).isoformat() if days else None)
            for v in (st.get("ledger") or {}).values():
                if v.get("_work") or (cutoff and (v.get("day") or "") < cutoff):
                    continue
                usd = float(v.get("cost") or 0)
                if usd <= 0:
                    continue
                rec["total_usd"] += usd
                rec["projects"][v.get("project") or "(unknown)"] = rec["projects"].get(v.get("project") or "(unknown)", 0.0) + usd
                if v.get("model"):
                    rec["models"][v["model"]] = rec["models"].get(v["model"], 0.0) + usd
                if v.get("day"):
                    rec["days"].add(v["day"])
            rec["sessions"] = len(st.get("sessions") or {})
        except Exception as e:
            rec["error"] = str(e)[:100]
        rec["days"] = sorted(rec["days"])
        out[name] = rec
    return out


def _usd(x):
    return f"${x:,.2f}"


def render(data, days=None):
    lines = ["spendguard scan — local coding-agent usage. No keys, no network, nothing leaves this machine.", ""]
    grand, any_data = 0.0, False
    for src, r in data.items():
        if r["error"]:
            lines.append(f"  {src:<13} (unreadable: {r['error']})")
            continue
        if not r["days"]:
            lines.append(f"  {src:<13} no transcripts found")
            continue
        any_data = True
        grand += r["total_usd"]
        span = f"{r['days'][0]} → {r['days'][-1]}" if r["days"] else "—"
        lines.append(f"  {src:<13} {r['sessions']:>5} sessions · {len(r['days'])} active days · {span}")
    if not any_data:
        lines += ["", "  Nothing to scan yet — this reads Claude Code / Codex transcripts on this machine.",
                  "  If you have metered API spend instead, connect a key and run `spendguard reconcile all`."]
        return "\n".join(lines)

    projects, models = {}, {}
    for r in data.values():
        for k, v in r["projects"].items():
            projects[k] = projects.get(k, 0.0) + v
        for k, v in r["models"].items():
            models[k] = models.get(k, 0.0) + v

    lines += ["", f"  EST PLAN VALUE{' (last %s days)' % days if days else ''} — what this work would cost at API "
                  f"rates.", "  This is NOT money billed: it is plan-covered usage, and it is never added to your "
                  "actual $.", ""]
    for p, v in sorted(projects.items(), key=lambda x: -x[1])[:8]:
        lines.append(f"    {p[:34]:<34}{_usd(v):>12}")
    if len(projects) > 8:
        lines.append(f"    {'… %d more' % (len(projects) - 8):<34}{_usd(sum(sorted(projects.values())[:-8])):>12}")
    lines += [f"    {'':-<34}{'':->12}", f"    {'TOTAL est value':<34}{_usd(grand):>12}",
              f"    {'billed $ (plan-covered)':<34}{_usd(0):>12}"]
    if models:
        top = " · ".join(f"{m} {_usd(v)}" for m, v in sorted(models.items(), key=lambda x: -x[1])[:3])
        lines += ["", f"  top models: {top}"]
    lines += ["", "  Next:", "    spendguard run -- python your_job.py   # gate a METERED run: caps + a cost "
              "estimate before submit", "    spendguard init                        # set your caps (60 seconds)",
              "    spendguard reconcile all               # once you have API keys: your ledger vs the real bill"]
    return "\n".join(lines)


def main(argv=None):
    argv = list(argv or [])
    days = None
    for i, a in enumerate(argv):
        if a == "--days" and i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                print("--days takes a number, e.g. --days 30")
                return 2
    print(render(collect(days=days), days=days))
    return 0
