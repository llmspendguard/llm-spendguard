"""Reconcile the LOCAL gate ledger against the PROVIDER's actual billing — find leaks.

The gate's local SQLite ledger records what spendguard SAW (gated). The providers bill what actually
happened. Comparing them per day surfaces the thing that matters: **spend billed by the provider that the
gate never recorded** = ungoverned/leaked spend (calls from a non-gated venv/process/repo, or before the
local ledger existed). Provider batch usage is the ground truth (fetched free, no Admin key needed);
real-time spend isn't provider-visible without an Admin key, so it's shown local-only.

  spendguard reconcile-ledger            # this month
  spendguard reconcile-ledger --since 2026-06-01

LEAK (provider > local) = the important signal. Local > provider = estimate-over-actual / double-count.
"""
import argparse
import datetime


def _provider_batch_by_day(since):
    from .report import openai_by_day
    from . import reconcile_anthropic as anth
    prov = {}
    try:
        oai, pending = openai_by_day()
    except Exception:
        oai, pending = {}, 0
    try:
        an, _models = anth.cost_by_day(since=since)
    except Exception:
        an = {}
    for d, v in list(oai.items()) + list(an.items()):
        if d >= since:
            prov[d] = prov.get(d, 0.0) + v
    return prov, pending


def _compute(since=None):
    """Compute the local-vs-provider reconciliation WITHOUT printing — for the report/monitor."""
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    prov, pending = _provider_batch_by_day(since)
    local_batch = budget.by_day(kind="batch", since=since, exclude_reconciled=True)   # reconciled rows ARE provider truth — don't count them as local
    cutoff = budget.ledger_start() or since
    post_p = sum(v for d, v in prov.items() if d >= cutoff)
    post_l = sum(v for d, v in local_batch.items() if d >= cutoff)
    leak = sum(max(0.0, prov.get(d, 0) - local_batch.get(d, 0))
               for d in prov if d >= cutoff and prov.get(d, 0) - local_batch.get(d, 0) > max(0.5, 0.05 * prov.get(d, 0)))
    return dict(since=since, cutoff=cutoff, prov=prov, local_batch=local_batch,
                local_rt=budget.by_day(kind="realtime", since=since), meta=budget.by_day(kind="meta", since=since),
                pending=pending, post_p=post_p, post_l=post_l, leak=leak,
                coverage=(post_l / post_p * 100) if post_p else 100.0)


def audit_completeness():
    """Triple-check the batch reconciliation is COMPLETE (regular keys only). Enumerate EVERY provider batch;
    the only ones legitimately without usage are genuine zero-cost (0 completed requests). Any batch with
    completed requests but no usage is flagged UNACCOUNTED — surfaced, never silently dropped. complete=True
    iff there are no unaccounted batches."""
    from . import pricing
    audit = {}
    try:
        from collections import Counter
        from .reconcile_openai import load_key, fetch_batches
        k = load_key()
        by_status = Counter(); total = counted = zero = 0; counted_usd = 0.0; unaccounted = []
        for b in fetch_batches(k):
            total += 1; by_status[b["status"]] += 1
            u = b.get("usage") or {}
            it, ot = u.get("input_tokens", 0), u.get("output_tokens", 0)
            if it or ot:
                counted += 1
                counted_usd += pricing.batch_cost(b["model"], it, ot, (u.get("input_tokens_details") or {}).get("cached_tokens", 0))
            elif (((b.get("request_counts") or {}).get("completed", 0)) or 0) > 0:
                unaccounted.append(b["id"])          # completed requests but no usage = a REAL gap
            else:
                zero += 1                             # cancelled / all-failed before completion → genuine $0
        audit["openai"] = dict(total=total, by_status=dict(by_status), counted=counted,
                               counted_usd=round(counted_usd, 2), zero_cost=zero, unaccounted=unaccounted)
    except Exception as e:
        audit["openai"] = {"error": str(e)[:140]}
    try:
        import json
        import os
        from . import reconcile_anthropic as ra
        cache = json.load(open(ra.CACHE_PATH)) if os.path.exists(ra.CACHE_PATH) else {}
        audit["anthropic"] = dict(batches=len(cache), counted_usd=round(sum(r.get("cost", 0) for r in cache.values()), 2))
    except Exception as e:
        audit["anthropic"] = {"error": str(e)[:140]}
    audit["complete"] = not audit.get("openai", {}).get("unaccounted")
    return audit


def reconcile_into_ledger(since=None):
    """Make the LOCAL ledger reflect PROVIDER-billed batch truth: write the per-(provider,day) GAP between
    provider billing and gate-recorded batch as 'unattributed' rows. Idempotent (rebuilds them). The gate-recorded
    spend stays attributed (project/user); the gap = pre-ledger / ungated / ungoverned. Zero model spend (provider
    GETs are free). Returns a summary. This is what makes the ledger correct + the dashboard show the real total."""
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    # Only the account-OWNER connection reconciles the SHARED provider-account gap. A connected non-owner
    # (owns_account=false — e.g. one of several repos sharing one OpenAI/Anthropic account) must NOT claim it, or it
    # attributes OTHER repos' provider spend to its own project (the bug where vision-pipeline absorbed nlp-pipeline's
    # batch). Standalone / unconnected use (no saas.json) still reconciles fully — the whole account is genuinely theirs.
    try:
        from . import saas as _saas
        _conn = _saas.conn()
    except Exception:
        _conn = {}
    from . import reconcile                                 # shared reconcile core (same account-anchor guard as GPU)
    _ok, _why = reconcile.owner_ok(_conn)
    if not _ok:
        return dict(since=since, skipped=_why,
                    provider_total=0.0, gate_attributed=0.0, ungoverned=0.0, gap_rows=0, gap_by_project={}, errors={})
    prov = {}   # (provider, day) -> $ billed (truth)
    errors = {}   # NEVER silently undercount — a failed/partial provider fetch must be visible, not hidden
    try:
        from .report import openai_by_day
        oai, _pending = openai_by_day()                        # NB: returns (by_day, pending)
        for d, v in oai.items():
            if d >= since:
                prov[("openai", d)] = prov.get(("openai", d), 0.0) + v
    except Exception as e:
        errors["openai"] = str(e)[:140]
    try:
        from . import reconcile_anthropic as anth
        an, _ = anth.cost_by_day(since=since)
        for d, v in an.items():
            if d >= since:
                prov[("anthropic", d)] = prov.get(("anthropic", d), 0.0) + v
    except Exception as e:
        errors["anthropic"] = str(e)[:140]
    local = budget.by_provider_day(kind="batch", since=since)   # gate-recorded (attributed) batch, by provider/day
    # Fallback project for batches with NO conversation evidence: a single-project repo's LLM provider account is
    # entirely THAT project (e.g. Acme's OpenAI/Anthropic spend is all 'nlp-pipeline'), so a no-evidence batch is the
    # repo's project, not truly 'unattributed' — that bucket is for genuinely MULTI-project repos only. This is the
    # "most-recent/primary task" rule the user asked for; it takes LLM attribution to ~100% for single-project orgs.
    try:
        from . import saas
        _c = saas.conn()
        _ps = _c.get("projects")
        fallback = "unattributed" if (isinstance(_ps, list) and len(_ps) > 1) else \
            (_c.get("project") or budget._project() or "unattributed").strip().lower()
    except Exception:
        fallback = "unattributed"
    # Attribute the gap BY PROJECT using conversation/intent evidence (batch id → conversation → project), so the
    # provider-truth gap lands on nlp-pipeline / vision-pipeline / … instead of one blanket 'unattributed' bucket.
    from . import backfill, conv, callio
    links = conv.batch_links()
    try:
        b2i = {b: i for b, i in callio._db().execute("SELECT batch, COALESCE(NULLIF(intent,''),'') FROM call_io GROUP BY batch")}
    except Exception:
        b2i = {}
    prov_by_proj = {}   # (project, day) -> provider $ (evidence-attributed)
    for _pn, _model, cost, _it, _ot, day, bid in (backfill._openai_rows() + backfill._anthropic_rows()):
        if (day or "") < since:
            continue
        proj = (conv._project_of(links[bid].get("snippet", "")) if bid in links else "") or \
               (conv._project_of(b2i[bid]) if b2i.get(bid) else "") or ""
        prov_by_proj[(proj or fallback, day)] = prov_by_proj.get((proj or fallback, day), 0.0) + cost
    # match by PROJECT TOTAL, not (project, day): provider-billing day ≠ gate-record day, so a per-day match would
    # fail to subtract the gate-attributed spend and double-count it. But the gap must be DATED correctly (else
    # day/week/month/quarter periods are wrong) → SPREAD each project's NET gap across its actual provider-usage
    # days, proportional to provider $/day. Spreading the NET (not re-matching) avoids the per-day double-count.
    prov_proj_total, prov_proj_days = {}, {}
    for (proj, day), pv in prov_by_proj.items():
        prov_proj_total[proj] = prov_proj_total.get(proj, 0.0) + pv
        prov_proj_days.setdefault(proj, {})
        prov_proj_days[proj][day] = prov_proj_days[proj].get(day, 0.0) + pv
    gate_proj_total = {}
    for (proj, _day), gv in budget.gate_by_project_day(kind="batch", since=since).items():
        gate_proj_total[proj] = gate_proj_total.get(proj, 0.0) + gv
    budget.clear_reconciled(since)
    gap_usd = 0.0
    gap_rows = 0
    by_project = {}
    for proj, pv in prov_proj_total.items():
        gap = pv - gate_proj_total.get(proj, 0.0)             # provider billed more than the gate saw for this project
        if gap > 0.01:
            days = prov_proj_days.get(proj, {})
            tot = sum(days.values()) or 1.0
            for day, v in sorted(days.items()):              # spread the net gap across actual usage days (dated correctly)
                share = gap * (v / tot)
                if share > 0.005:
                    budget.record_reconciled(day, "(reconciled)", round(share, 6), project=proj)
            gap_usd += gap
            gap_rows += 1
            by_project[proj] = round(gap, 2)
    provider_total = round(sum(prov_proj_total.values()), 2)   # evidence-based total — same source as the gaps
    local_total = round(sum(gate_proj_total.values()), 2)
    audit = audit_completeness()                  # triple-check: every batch accounted, nothing silently dropped
    return dict(since=since, provider_total=provider_total, gate_attributed=local_total,
                ungoverned=round(gap_usd, 2), gap_rows=gap_rows, gap_by_project=by_project,
                coverage=round(local_total / provider_total * 100, 1) if provider_total else 100.0,
                errors=errors, providers_ok=[p for p in ("openai", "anthropic") if p not in errors],
                complete=audit.get("complete", False), unaccounted=audit.get("openai", {}).get("unaccounted", []),
                audit=audit)


_RT_MARKER = "(realtime-history)"   # marker model for realtime backfilled from the gate's realtime_log


def reconcile_realtime(since=None):
    """Backfill the gate's realtime history into the LEDGER. Realtime is normally recorded live (gate → sqlite +
    realtime_log.jsonl), but spend logged BEFORE the sqlite backend — or otherwise only in the log — never reaches the
    ledger, so it doesn't push to the org. This imports realtime_log.jsonl as 'realtime' rows = max(0, log − gate-
    recorded) per (provider, day). Idempotent (clears + rebuilds the marker rows each run). NOT provider-truth — it's
    the gate's own complete log; catching UNGATED realtime would need the provider Usage/Admin API (separate). Project
    fallback = the connection's single project (a single-project repo's realtime is all that project), else
    'unattributed' — same rule as the batch reconcile. Zero spend (reads a local file)."""
    from . import budget
    from .config import RT_LOG
    import os, json
    since = since or datetime.date.today().replace(day=1).isoformat()
    if not os.path.exists(RT_LOG):
        return dict(since=since, imported=0.0, rows=0)
    budget.clear_reconciled(since=since, model=_RT_MARKER)        # rebuild from the log (idempotent)
    log_pd = {}                                                  # (provider, day) -> $ in the gate's realtime log
    try:
        for ln in open(RT_LOG):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            d = r.get("day", "")
            if not d or d < since:
                continue
            k = (r.get("provider") or "?", d)
            log_pd[k] = log_pd.get(k, 0.0) + float(r.get("cost") or 0)
    except Exception:
        return dict(since=since, imported=0.0, rows=0)
    gate_pd = budget.by_provider_day(kind="realtime", since=since)   # REAL gate realtime (markers just cleared)
    try:
        from . import saas
        _c = saas.conn()
        _ps = _c.get("projects")
        fallback = "unattributed" if (isinstance(_ps, list) and len(_ps) > 1) else \
            (_c.get("project") or budget._project() or "unattributed").strip().lower()
    except Exception:
        fallback = "unattributed"
    imported, rows = 0.0, 0
    for (prov, day), log_cost in log_pd.items():
        gap = log_cost - gate_pd.get((prov, day), 0.0)           # log has more than the ledger saw → the stranded gap
        if gap > 0.005:
            budget.record_reconciled(day, prov, gap, project=fallback, kind="realtime", model=_RT_MARKER)
            imported += gap
            rows += 1
    return dict(since=since, imported=round(imported, 4), rows=rows)


def leak_line(since=None):
    """One-line leak alert for the report (or None if clean / nothing to compare)."""
    try:
        c = _compute(since)
    except Exception:
        return None
    if c["leak"] > 0.5:
        return (f"⚠️ LEDGER LEAK: ~${c['leak']:.2f} provider-billed batch not in the local ledger since "
                f"{c['cutoff']} (coverage {c['coverage']:.0f}%) — run `spendguard reconcile-ledger`.")
    if c["post_p"] > 0:
        return f"ledger coverage {c['coverage']:.0f}% of provider batch since {c['cutoff']} (no material leak)."
    return None


def sync(since=None):
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    prov, pending = _provider_batch_by_day(since)
    local_batch = budget.by_day(kind="batch", since=since, exclude_reconciled=True)   # exclude provider-truth rows from the local side
    local_rt = budget.by_day(kind="realtime", since=since)
    meta = budget.by_day(kind="meta", since=since)
    lstart = budget.ledger_start()

    print(f"reconcile-ledger — local gate ledger vs provider billing, since {since}")
    if lstart and lstart > since:
        print(f"  ⚠ local ledger only has data since {lstart}; provider spend before that is pre-ledger "
              f"(expected gap, not a true leak).")
    print(f"\n  {'day':<12}{'provider batch':>15}{'local batch':>13}{'diff':>11}  status")
    days = sorted(set(prov) | set(local_batch))
    p_tot = l_tot = leak = post_p = post_l = 0.0
    cutoff = lstart or since
    for d in days:
        p, l = prov.get(d, 0.0), local_batch.get(d, 0.0)
        diff = p - l
        p_tot += p; l_tot += l
        pre = d < cutoff
        if pre:
            status = "· pre-ledger (expected)"
        else:
            post_p += p; post_l += l
            if diff > max(0.5, 0.05 * p):
                status, leak = "⚠️ LEAK (billed, not gated)", leak + diff
            elif diff < -max(0.5, 0.05 * max(p, l)):
                status = "over-recorded (est>actual?)"
            else:
                status = "ok"
        print(f"  {d:<12}{('$%.2f' % p):>15}{('$%.2f' % l):>13}{('$%+.2f' % diff):>11}  {status}")
    print(f"  {'TOTAL':<12}{('$%.2f' % p_tot):>15}{('$%.2f' % l_tot):>13}{('$%+.2f' % (p_tot - l_tot)):>11}")
    cov = (post_l / post_p * 100) if post_p else 100.0
    print(f"\n  since the ledger went live ({cutoff}): provider ${post_p:.2f} vs local ${post_l:.2f} "
          f"→ coverage {cov:.0f}%")
    if leak > 0.5:
        print(f"  ⚠ ~${leak:.2f} provider-billed batch since {cutoff} is NOT in the local ledger — "
              f"ungoverned spend (a non-gated venv/process/repo). Install the gate there.")
    elif post_p > 0:
        print("  ✓ no material leak since the ledger went live — provider batch billing is accounted for.")
    else:
        print("  (no provider batch billing since the ledger went live yet — re-run after the next gated batch.)")
    print(f"  real-time (local-only, no provider cross-check w/o Admin key): ${sum(local_rt.values()):.2f}")
    print(f"  spendguard meta (advisor): ${sum(meta.values()):.2f}")
    # work done this month — context for the spend above (git commits + LLM intents per project). Same data the
    # sync pushes to the org; shown here so a reconcile reports BOTH "what it cost" and "what got done".
    try:
        from . import workdone
        wd = sorted(workdone.rollup(since=since, by="month"), key=lambda x: -x.get("n_commits", 0))
        if wd:
            print("\n  work done (context for the spend):")
            for r in wd[:6]:
                top = sorted((r.get("intents") or {}).items(), key=lambda x: -x[1])[:2]
                ints = (" · LLM: " + ", ".join(f"{i}×{n}" for i, n in top)) if top else ""
                print(f"    [{r['project'] or '?'}] {r['n_commits']} commits, {r['active_days']} active day(s){ints}")
    except Exception:
        pass
    if pending:
        print(f"  ({pending:,} OpenAI requests in flight — not yet billed)")
    return dict(provider=p_tot, local=l_tot, coverage=cov, leak=leak)


def _provider_total(since):
    """Provider-billed LLM total (the TRUTH) since `since` — OpenAI + Anthropic, as billed."""
    total = 0.0
    try:
        from .report import openai_by_day
        oai, _ = openai_by_day()
        total += sum(v for d, v in oai.items() if d >= since)
    except Exception:
        pass
    try:
        from . import reconcile_anthropic as anth
        an, _ = anth.cost_by_day(since=since)
        total += sum(v for d, v in an.items() if d >= since)
    except Exception:
        pass
    return round(total, 2)


def _gate_captured_rows(since):
    """Gate-recorded (attributed) batch LLM spend by project — the CAPTURED side, as reconcile.Source rows."""
    from . import budget
    by = {}
    for (proj, _day), gv in budget.gate_by_project_day(kind="batch", since=since).items():
        by[proj] = by.get(proj, 0.0) + gv
    return [{"cost": round(v, 2), "project": p} for p, v in by.items()]


class LLMSource:
    """reconcile.Source adapter for LLM provider spend. truth = provider billing (OpenAI + Anthropic); captured =
    gate-recorded batch by project; the gap is attributed by conversation/intent evidence INSIDE
    reconcile_into_ledger (batch→conv→project), so attribute_gap returns [] here and the residual is surfaced."""
    name = "llm"

    def __init__(self, conn=None, since=None):
        from . import saas
        try:
            self._conn = conn if conn is not None else saas.conn()
        except Exception:
            self._conn = {}
        self._since = since or datetime.date.today().replace(day=1).isoformat()

    def conn(self):
        return self._conn

    def truth_total(self, since=None):
        return _provider_total(since or self._since)

    def captured(self, since=None):
        return _gate_captured_rows(since or self._since)

    def attribute_gap(self, gap, since=None):
        # the gap that reconcile_into_ledger already attributed by conversation/intent evidence (batch→conv→project),
        # stored as reconciled rows. If reconcile hasn't run, this is empty → the residual warns to run it.
        from . import budget
        return [{"cost": round(v, 2), "project": p}
                for p, v in budget.reconciled_by_project(since or self._since).items()]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard reconcile-ledger")
    ap.add_argument("--since", help="YYYY-MM-DD (default: start of this month)")
    a = ap.parse_args(argv)
    sync(since=a.since)
    return 0
