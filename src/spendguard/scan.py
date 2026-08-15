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


def collect(days=None):
    """{source display name: {sessions, projects, models, days, total_usd, error}} — via the transcript PORT
    (`sources.transcript_sources`), so a tool we've never heard of shows up here the moment its adapter is
    installed. Never raises for a missing/broken source."""
    from . import sources
    out = {}
    for _key, src in sources.transcript_sources():
        name = getattr(src, "NAME", _key)
        try:
            rec = src.read(days=days) or {}
            rec.setdefault("error", None)
        except Exception as e:
            rec = {"error": str(e)[:100]}
        # A source (especially a third-party plugin) can RETURN a malformed record without raising — the
        # try/except above only catches a raise. Normalize to the shape render() reads, and coerce the total to a
        # number, so a bad plugin return can't KeyError/TypeError the whole scan (the docstring's "never raises").
        for _k, _default in (("sessions", 0), ("projects", {}), ("models", {}), ("days", []), ("total_usd", 0.0)):
            rec.setdefault(_k, _default)
        try:
            rec["total_usd"] = float(rec["total_usd"] or 0)
        except (TypeError, ValueError):
            rec["total_usd"] = 0.0
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
        # EMPTY STATE: don't dead-end. Someone with no agent transcripts still has providers and interpreters —
        # show what this machine CAN spend through, from the same discovery `spendguard sources` uses.
        lines += ["", "  No agent transcripts found (Claude Code / Codex are built in; other tools plug in via",
                  "  the `spendguard.providers` entry point). Here's what this machine can spend through anyway:", ""]
        try:
            from . import sources
            d = sources.discover()
            paid = [p["provider"] for p in d["providers"] if p["resolved"]]
            lines.append(f"    providers with a key : {', '.join(paid) if paid else '(none — add one to keys.env)'}")
            lines.append(f"    venvs that can spend : {len(d['venvs'])} ({len(d['ungated'])} NOT gated)")
            lines += ["", "  Next:",
                      "    spendguard reconcile all               # your ledger vs the provider's actual bill (free)"
                      if paid else
                      "    add a provider key to ~/.spendguard/keys.env, then `spendguard reconcile all`"]
            if d["ungated"]:
                lines.append(f"    spendguard install-hook --venv {d['ungated'][0]['venv']}")
            lines.append("    spendguard sources                     # the full picture")
        except Exception:
            lines.append("    (run `spendguard sources` for what this machine can spend through)")
        return "\n".join(lines)

    projects, models = {}, {}
    for r in data.values():
        # A source's per-project / per-model buckets come from the same (possibly third-party) read() as the
        # total. Guard the aggregation: a non-dict bucket or a non-numeric value must not crash the whole scan.
        for _bucket, _acc in (("projects", projects), ("models", models)):
            _src = r.get(_bucket)
            if not isinstance(_src, dict):
                continue
            for k, v in _src.items():
                try:
                    _acc[k] = _acc.get(k, 0.0) + float(v or 0)
                except (TypeError, ValueError):
                    continue

    lines += ["", f"  EST PLAN VALUE{' (last %s days)' % days if days else ''} — what this work would cost at API "
                  f"rates.", "  This is NOT money billed: it is plan-covered usage, and it is never added to your "
                  "actual $.", ""]
    top_projects = sorted(projects.items(), key=lambda x: -x[1])
    for p, v in top_projects[:8]:
        lines.append(f"    {p[:34]:<34}{_usd(v):>12}")
    if len(projects) > 8:
        # The remainder is the EXACT complement of the displayed top 8 — top_projects[8:] — not a re-sorted
        # 'smallest' slice that could pick a different (tie-broken) set than what was shown.
        rest = sum(v for _p, v in top_projects[8:])
        lines.append(f"    {'… %d more' % (len(projects) - 8):<34}{_usd(rest):>12}")
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
