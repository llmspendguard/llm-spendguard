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
    """Local-vs-provider batch reconciliation WITHOUT printing. Answers TWO questions that must NOT be conflated:
      • LEAK = provider batch truth that is NOT in the ledger AT ALL (neither gate-recorded NOR already reconciled/
        backfilled). This is the alarm — genuinely ungoverned spend — so it is measured vs local INCLUDING the
        backfill (the `(provider-batch)` reconciled rows ARE accounted; excluding them would re-flag a gap a prior
        reconcile already absorbed).
      • capture_rate = how much the gate recorded LIVE (excl backfill). A quality signal ("are we capturing at the
        source, or leaning on after-the-fact reconcile?"), NOT a leak.
    The batch window opens at the first batch row in the ledger (cutoff = ledger_start('batch')), NOT the global
    ledger_start: realtime started recording weeks before batch, so the global start would drag in pre-batch-recording
    history and mislabel it as a leak. Provider batch before the batch cutoff is pre-ledger (expected)."""
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    prov, pending = _provider_batch_by_day(since)
    accounted = budget.by_day(kind="batch", since=since, exclude_reconciled=False)    # gate-recorded + backfill = what the ledger accounts for
    gate_only = budget.by_day(kind="batch", since=since, exclude_reconciled=True)     # what the gate captured LIVE (capture-rate signal)
    cutoff = budget.ledger_start("batch") or budget.ledger_start() or since
    post_p = sum(v for d, v in prov.items() if d >= cutoff)
    post_a = sum(v for d, v in accounted.items() if d >= cutoff)
    post_g = sum(v for d, v in gate_only.items() if d >= cutoff)
    pre_ledger = sum(v for d, v in prov.items() if d < cutoff)                         # provider batch before batch-recording existed
    # leak = NET account residual since the cutoff (provider truth not yet reconciled), NOT the sum of per-day positive
    # gaps: reconcile_into_ledger caps accounted ≤ provider at the TOTAL, then SPREADS the backfill across provider-usage
    # days — so per-day positive gaps are offset by over-recording on gate-record days (a day-spread artifact), and
    # summing them overstates the leak (the $507 vs the true $27). The net is what's genuinely unaccounted.
    leak = max(0.0, round(post_p - post_a, 2))
    return dict(since=since, cutoff=cutoff, prov=prov, local_batch=accounted, gate_batch=gate_only, pre_ledger=pre_ledger,
                local_rt=budget.by_day(kind="realtime", since=since), meta=budget.by_day(kind="meta", since=since),
                pending=pending, post_p=post_p, post_l=post_a, post_g=post_g, leak=leak,
                coverage=(post_a / post_p * 100) if post_p else 100.0,
                capture_rate=(post_g / post_p * 100) if post_p else 100.0)


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


def true_down(since=None, billed_rows=None):
    """ESTIMATE→ACTUAL true-down: bring the gate's PRE-SUBMIT batch estimate rows down to the provider-BILLED
    actuals. The gate records a batch's cost when it is submitted (the batch id does not exist yet, so estimate
    rows carry no batch id — structural, see gate._decide_and_account); the provider later bills the actual token
    usage per batch. This nets the two per (provider, model): billed = Σ per-batch actuals (both providers, from
    the reconcile caches via backfill's per-batch rows — grounded in batch ids on the ACTUALS side), and the
    over-estimate Δ = max(0, estimates − billed) is written as NEGATIVE correction rows spread across the estimate
    cells (project × day) proportionally. Under-estimates are NOT touched here — provider>recorded is the gap
    machinery's job (two one-way valves that meet at billed truth).

    Correction rows carry the REAL model + the true-down conv_id sentinel; original estimate rows are NEVER
    mutated (forensic: the ledger keeps what we thought AND what it billed; by_dims nets them for the push).
    Idempotent: the window's corrections are cleared + rebuilt from current billed truth each run, so a re-run is
    a no-op and an IN-FLIGHT batch (estimate recorded, not yet billed) that trues down today self-heals tomorrow
    when its actuals land. A provider whose billed fetch FAILED is skipped entirely — unknown must never read as
    $0 billed, or real estimates would be zeroed. REALTIME is untouched on purpose: gate realtime rows already
    record ACTUAL tokens at call time (the inline true-up), and without an admin key there are no per-call
    provider actuals to true down to.

    `billed_rows` = {"openai": [(prov, model, cost, in, out, day, batch_id), ...] | None, "anthropic": ...};
    None/absent = fetch that provider here (None after a failed fetch = skip). Returns the correction summary."""
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    if billed_rows is None:
        billed_rows = {}
        from . import backfill
        for prov_name, fetch in (("openai", lambda: backfill._openai_rows()),
                                 ("anthropic", lambda: backfill._anthropic_rows())):
            try:
                billed_rows[prov_name] = fetch()
            except Exception:
                billed_rows[prov_name] = None
    budget.clear_true_down(since)                       # rebuild the window's corrections from current billed truth
    # JOIN on the NORMALIZED model: gate estimate rows record the id the caller submitted (often the dated snapshot,
    # e.g. claude-haiku-4-5-20251001) while the billed caches key the base name — an exact-string join misses those,
    # trues real billed spend to $0 and dumps it on the gap machinery (right total, degraded attribution). The
    # correction ROW still carries the cell's ORIGINAL model so by_dims nets it against the very rows it corrects.
    from . import pricing

    def _join_model(m):
        try:
            return pricing.normalize(m or "?")
        except Exception:
            return m or "?"
    billed = {}                                          # (provider, normalized model) -> billed $ in the window
    for prov_name, rows in billed_rows.items():
        if rows is None:
            continue                                     # billed truth UNKNOWN for this provider → never true down
        for _p, model, cost, _it, _ot, day, _bid in rows:
            if (day or "") < since:
                continue
            k = (prov_name, _join_model(model))
            billed[k] = billed.get(k, 0.0) + float(cost or 0)
    cells = budget.gate_batch_cells(since)               # (project, provider, model, day) -> gate estimate $
    est = {}                                             # (provider, normalized model) -> Σ estimates
    for (_proj, prov_name, model, _day), v in cells.items():
        k = (prov_name, _join_model(model))
        est[k] = est.get(k, 0.0) + v
    trued, by_model = 0.0, {}
    for (prov_name, jmodel), est_total in est.items():
        if billed_rows.get(prov_name) is None:
            continue                                     # skipped provider (fetch failed)
        delta = est_total - billed.get((prov_name, jmodel), 0.0)
        if delta <= 0.01 or est_total <= 0:
            continue                                     # billed ≥ estimate (or nothing recorded) → nothing to true down
        for (proj, p2, m2, day), v in cells.items():
            if (p2, _join_model(m2)) != (prov_name, jmodel) or v <= 0:
                continue
            share = delta * (v / est_total)
            if share > 0.005:
                budget.record_true_down(day, prov_name, m2, round(share, 6), project=proj)
        trued += delta
        by_model[f"{prov_name}:{jmodel}"] = round(delta, 2)
    return dict(since=since, trued_down=round(trued, 2), by_model=by_model,
                skipped=[p for p, r in billed_rows.items() if r is None])


def reconcile_into_ledger(since=None):
    """Make the LOCAL ledger reflect PROVIDER-billed batch truth, from BOTH sides: (1) true_down() nets the gate's
    batch ESTIMATE rows down to billed actuals (estimate bias / failed batches never billed), then (2) the
    per-(provider,day) GAP between provider billing and gate-recorded batch is written as attributed rows
    (provider > recorded = pre-ledger / ungated / ungoverned). Idempotent (rebuilds both). The gate-recorded
    spend stays attributed (project/user). Zero model spend (provider GETs are free). Returns a summary. This is
    what makes the ledger correct + the dashboard show the real total, and it rides the existing daily reconcile
    cadence — no separate scheduler."""
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
    # Per-batch billed actuals, fetched ONCE and shared by the true-down and the evidence-attribution loop below.
    # A failed side is None (skip-that-provider in true_down; the loop just sees no evidence rows) and lands in
    # `errors` — a partial fetch must be visible, never silently treated as $0 billed.
    from . import backfill, conv
    _billed_rows = {}
    for _prov_name, _fetch in (("openai", backfill._openai_rows), ("anthropic", backfill._anthropic_rows)):
        try:
            _billed_rows[_prov_name] = _fetch()
        except Exception as e:
            _billed_rows[_prov_name] = None
            errors.setdefault(_prov_name, str(e)[:140])
    td = true_down(since=since, billed_rows=_billed_rows)       # estimates → billed actuals FIRST, so every
    local = budget.by_provider_day(kind="batch", since=since)   # gate-side read below sees corrected numbers
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
    # Attribute the gap BY PROJECT, AGENTICALLY: each batch → the SUBCONVERSATION that ran it → that segment's
    # LLM-classified project (with the repo/cwd as a PRIOR the LLM confirms/overrides). NEVER a regex keyword guess.
    # An evidenced batch (we know which repo ran it) always gets a real project; only a batch with NO conversation
    # at all falls to `fallback`. Populate the agentic cache with `spendguard accounting --run` (estimate-first).
    bmap = conv.batch_project_map()
    prov_by_proj = {}   # (project, day) -> provider $ (evidence-attributed)
    for _pn, _model, cost, _it, _ot, day, bid in ((_billed_rows.get("openai") or []) + (_billed_rows.get("anthropic") or [])):
        if (day or "") < since:
            continue
        proj = (bmap.get(bid, {}).get("project") or "").lower()   # agentic per-subconversation attribution
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
    provider_total = round(sum(prov_proj_total.values()), 2)   # evidence-based total — same source as the gaps
    local_total = round(sum(gate_proj_total.values()), 2)
    # Cap total reconciled at the ACCOUNT net gap (provider − gate). The per-project positive gaps (provider_proj −
    # gate_proj, negatives dropped) can sum to MORE than the true account gap when a batch is gated under one project
    # key but evidence-attributed to ANOTHER (gate_proj=0 → the full provider $ is written as a reconciled row while
    # the original gate row still stands → DOUBLE-COUNT, total > provider). Scaling the positive gaps down to the
    # account net guarantees gate + reconciled ≤ provider_total. Account-anchored magnitude, evidence-weighted split.
    account_net = max(0.0, round(provider_total - local_total, 2))
    pos = {proj: (pv - gate_proj_total.get(proj, 0.0)) for proj, pv in prov_proj_total.items()}
    pos = {proj: g for proj, g in pos.items() if g > 0.01}
    overshoot = round(sum(pos.values()), 2)
    scale = (account_net / overshoot) if (overshoot > account_net and overshoot > 0) else 1.0
    gap_usd = 0.0
    gap_rows = 0
    by_project = {}
    for proj, gap in pos.items():
        gap = round(gap * scale, 6)
        if gap <= 0.01:
            continue
        days = prov_proj_days.get(proj, {})
        tot = sum(days.values()) or 1.0
        for day, v in sorted(days.items()):                  # spread the (capped) gap across actual usage days
            share = gap * (v / tot)
            if share > 0.005:
                budget.record_reconciled(day, "(reconciled)", round(share, 6), project=proj)
        gap_usd += gap
        gap_rows += 1
        by_project[proj] = round(gap, 2)
    audit = audit_completeness()                  # triple-check: every batch accounted, nothing silently dropped
    return dict(since=since, provider_total=provider_total, gate_attributed=local_total,
                true_down=td,
                ungoverned=round(gap_usd, 2), gap_rows=gap_rows, gap_by_project=by_project,
                coverage=round(local_total / provider_total * 100, 1) if provider_total else 100.0,
                errors=errors, providers_ok=[p for p in ("openai", "anthropic") if p not in errors],
                complete=audit.get("complete", False), unaccounted=audit.get("openai", {}).get("unaccounted", []),
                audit=audit)


_RT_MARKER = "(realtime-history)"   # marker model for realtime backfilled from the gate's realtime_log
_RT_ORACLE_MARKER = "(realtime-oracle)"   # marker for realtime reconciled to the provider ADMIN-usage truth (timing-matched)
_RT_RECON_MARKER = "(realtime-reconstructed)"   # realtime reconstructed AGENTICALLY from conversations (admin-free, production)


def record_realtime_reconstruction(since=None):
    """WIRE the agentic realtime reconstruction into the ledger (ADMIN-FREE, production realtime axis). The expensive
    FIND runs PERIODICALLY (scripts/reconstruct/realtime_find_batch.py: find→consolidate→clean) and writes a tightened
    cache (~/.spendguard/realtime_reconstruction.json: embeddings/batch/spendguard-meta excluded, printed-$ ground truth,
    soft estimates halved). THIS read-and-record is cheap and idempotent — marker rows (kind=realtime, _RT_RECON_MARKER)
    that are reversible + excluded from gate/cap, so re-running rebuilds them. Admin NEVER writes here (dev cross-check
    only). NOTE: org-level + recorded at the window start (per-project-within-org + per-month split are refinements —
    needs the reconstruction to carry per-run project + session date)."""
    from . import budget
    import os, json
    cache = os.path.expanduser("~/.spendguard/realtime_reconstruction.json")
    if not os.path.exists(cache):
        return dict(recorded=0.0, rows=0, note="no reconstruction cache (run the periodic find/clean first)")
    try:
        data = json.load(open(cache))
    except Exception:
        return dict(recorded=0.0, rows=0, note="cache unreadable")
    day = since or data.get("since") or datetime.date.today().replace(day=1).isoformat()
    budget.clear_reconciled(model=_RT_RECON_MARKER)              # idempotent rebuild
    org_project = {"healiom": "lmm", "ensight": "llm-spendguard", "personal": "personal-admin"}   # org → representative project
    agg = {}
    for r in data.get("rows", []):
        proj = org_project.get((r.get("org") or "").lower(), "unattributed")
        k = (proj, r.get("provider") or "?")
        agg[k] = agg.get(k, 0.0) + float(r.get("usd") or 0)
    rec, n = 0.0, 0
    for (proj, prov), usd in agg.items():
        if usd <= 0:
            continue
        budget.record_reconciled(day=day, provider=prov, cost=round(usd, 2), project=proj,
                                 kind="realtime", model=_RT_RECON_MARKER)
        rec += usd; n += 1
    return dict(recorded=round(rec, 2), rows=n, day=day)


def reconcile_realtime(since=None):
    """Reconcile REALTIME spend into the ledger — the source historically dropped (regular key = batch only; realtime
    is NOT chat-reconstructable). Two paths:

    ADMIN-ORACLE (dev; SPENDGUARD_ADMIN_ORACLE + admin keys): reconcile to the PROVIDER admin-usage TRUTH, timing-
    matched per project (`realtime_oracle.by_project_day`) and RECORDED into the ledger — mirroring the batch reconcile
    (truth − gate-live = gap, recorded per project). This is how the real ~$1.5k of historical realtime reaches the
    org. Recording it (not just printing) means the no-admin-key CLIENT then PUSHES it like any other spend, and the
    completeness verdict reads reconciled instead of UNDER. Record once, never re-derive.

    DEFAULT (no admin key): import the gate's own realtime_log as the $-floor (max(0, log − gate-recorded) per
    provider/day). The FORWARD fix for full coverage with no admin key is the gate's inline true-up (records actual
    tokens at call time). Idempotent (clears + rebuilds the marker rows each run)."""
    from . import budget
    from .config import RT_LOG
    import os, json
    since = since or datetime.date.today().replace(day=1).isoformat()

    # ADMIN KEYS ARE DEV-ASSIST ONLY — they can NEVER be part of the main path. The admin-oracle-into-ledger block that
    # used to live HERE (record provider admin-usage truth as realtime spend) is DELETED on purpose: it made admin a
    # production reconcile input, the exact thing we forbid. The realtime axis is reconstructed AGENTICALLY from the
    # conversations — `resources.reconstruct_remote_llm` reads each session's recorded token records, classifies every
    # run (if it isn't batch it's realtime), prices the in/out tokens via pricing.py, attributes via the same
    # session_classification as batch — and is recorded by the CORE reconcile as its own marker. Admin appears ONLY in a
    # separate DEV cross-check (`scripts/audit/…`) that INDICATES the reconstruction's gap; it never writes the ledger.

    # ── PRODUCTION realtime axis: fold in the agentic conversational reconstruction (admin-free, from the cache) ──
    record_realtime_reconstruction()

    # ── gate realtime_log floor (forward inline true-up; no admin, no network) ──
    # CRITICAL: do NOT touch _RT_ORACLE_MARKER here. Those rows are the historical realtime truth, RECORDED ONCE by a
    # dev oracle run; the daily scheduler runs this path (no admin key) and must PERSIST them, not wipe them. The
    # RT_LOG gap recorded below is ~0 once the oracle has run (log ≈ gate-live), so the two coexist without double.
    if not os.path.exists(RT_LOG):
        return dict(since=since, imported=0.0, rows=0)
    budget.clear_reconciled(since=since, model=_RT_MARKER)        # rebuild ONLY our own log-floor markers (idempotent)
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


def _leak_cache_path():
    from . import config
    return config.HOME / "leak_line.json"


def cached_leak_line():
    """(line_or_None, age_seconds) from the LAST computed leak line, or None if never computed.
    Data efficiency: the provider pull behind leak_line() takes minutes; it already runs in the daily
    report / reconcile / close, and each run persists its verdict here as a byproduct. Fast readers
    (`spendguard doctor`) reuse that knowledge with its age instead of re-pulling 30 days of billing."""
    import json
    import time
    try:
        d = json.loads(_leak_cache_path().read_text())
        return d.get("line"), max(0.0, time.time() - float(d["ts"]))
    except Exception:
        return None


def _render_leak_line(c):
    if c["post_p"] <= 0 and c.get("pre_ledger", 0) <= 0.5:
        return None
    post_p, leak, cap, pre = c["post_p"], c["leak"], c.get("capture_rate", 100.0), c.get("pre_ledger", 0.0)
    # A material NET shortfall means provider truth hasn't been reconciled into the ledger yet (stale/never-run
    # reconcile) — `spendguard saas reconcile` (free) closes it. It is NOT "ungoverned spend / install the gate":
    # reconcile always absorbs the $ gap; ungated SOURCES are `spendguard coverage`'s job, not this metric's.
    if leak > max(2.0, 0.03 * post_p):
        return (f"ledger batch: ${leak:.2f} behind provider since {c['cutoff']} ({c['coverage']:.0f}% accounted) "
                f"— run `spendguard saas reconcile` (free) to refresh.")
    cap_s = f"gate captured {cap:.0f}% live" + ("" if cap >= 80 else " — `spendguard coverage` lists any ungated sources")
    pre_s = f"; ${pre:.0f} pre-batch-recording (one reconcile absorbs it)" if pre > 0.5 else ""
    return f"ledger batch: ✓ {c['coverage']:.0f}% accounted, no material leak — {cap_s}{pre_s}."


def leak_line(since=None):
    """One-line batch status for the report (or None if nothing to compare). Distinguishes:
      • a real LEAK — provider truth NOT in the ledger at all (alarm, ungoverned spend);
      • a capture-rate gap — gate didn't record it live, but reconcile backfilled it (accounted, just not captured
        at the source — informational, NOT an alarm);
      • pre-batch-recording history — provider batch before the gate tracked batch (expected; one reconcile absorbs it).
    Reporting a capture-rate gap as a LEAK is the bug this fixes: it cried '~$1.9k ungoverned, install the gate' when
    every batch since recording began was in fact accounted.
    Every SUCCESSFUL computation (including a 'nothing to compare' None — that too is knowledge) persists to
    leak_line.json for cached_leak_line() readers; a failed compute is NOT cached (failure isn't knowledge)."""
    try:
        c = _compute(since)
    except Exception:
        return None
    line = _render_leak_line(c)
    try:
        import json
        import time
        _leak_cache_path().write_text(json.dumps({"line": line, "ts": time.time()}))
    except Exception:
        pass
    return line


def sync(since=None):
    from . import budget
    since = since or datetime.date.today().replace(day=1).isoformat()
    prov, pending = _provider_batch_by_day(since)
    accounted = budget.by_day(kind="batch", since=since, exclude_reconciled=False)    # gate-recorded + reconciled backfill = ACCOUNTED
    gate_only = budget.by_day(kind="batch", since=since, exclude_reconciled=True)     # what the gate captured LIVE (capture-rate)
    local_rt = budget.by_day(kind="realtime", since=since)
    meta = budget.by_day(kind="meta", since=since)
    cutoff = budget.ledger_start("batch") or budget.ledger_start() or since          # the batch axis started recording here

    print(f"reconcile-ledger — provider billing vs the local ledger (batch), since {since}")
    print("  accounted = gate-recorded LIVE + reconciled-from-provider backfill.  leak = NET provider truth unaccounted.")
    if cutoff and cutoff > since:
        print(f"  ⚠ batch recording began {cutoff}; provider batch before that is pre-ledger (expected, not a leak).")
    print(f"\n  {'day':<12}{'provider':>11}{'accounted':>11}{'gate live':>11}{'Δ vs prov':>11}  status")
    days = sorted(set(prov) | set(accounted))
    p_tot = a_tot = g_tot = post_p = post_a = post_g = 0.0
    for d in days:
        p, a, g = prov.get(d, 0.0), accounted.get(d, 0.0), gate_only.get(d, 0.0)
        p_tot += p; a_tot += a; g_tot += g
        gap = p - a                                          # signed: provider − accounted on THIS day (day-spread noise)
        if d < cutoff:
            status = "· pre-ledger (expected)"
        else:
            post_p += p; post_a += a; post_g += g
            # per-day +/- gaps are a day-spread artifact (the backfill is spread by provider $/day, not matched to the
            # gate-record day), so they are NOT per-day leaks — only the NET (below) is. Label them descriptively.
            if gap > max(0.5, 0.05 * p):
                status = "under-covered (day-spread)"
            elif gap < -max(0.5, 0.05 * max(p, a)):
                status = "over-covered (day-spread)"
            else:
                status = "ok"
        print(f"  {d:<12}{('$%.2f' % p):>11}{('$%.2f' % a):>11}{('$%.2f' % g):>11}{('$%+.2f' % gap):>11}  {status}")
    print(f"  {'TOTAL':<12}{('$%.2f' % p_tot):>11}{('$%.2f' % a_tot):>11}{('$%.2f' % g_tot):>11}")
    cov = (post_a / post_p * 100) if post_p else 100.0
    cap = (post_g / post_p * 100) if post_p else 100.0
    leak = max(0.0, round(post_p - post_a, 2))                # the ONE honest leak number: net provider − accounted
    print(f"\n  since batch recording began ({cutoff}): provider ${post_p:.2f} vs accounted ${post_a:.2f} → {cov:.0f}% accounted")
    print(f"  of that, the gate captured ${post_g:.2f} LIVE ({cap:.0f}%); the rest was reconciled from provider truth.")
    if leak > max(2.0, 0.03 * post_p):
        print(f"  ⚠ ${leak:.2f} net provider batch since {cutoff} isn't reconciled yet — run `spendguard saas reconcile` "
              f"(free) to refresh. (Not 'ungoverned': reconcile closes the $ gap; `spendguard coverage` finds ungated sources.)")
    elif post_p > 0:
        print(f"  ✓ no material leak — ${leak:.2f} net unreconciled (staleness only); every provider batch is accounted.")
    else:
        print("  (no provider batch billing since recording began yet — re-run after the next gated batch.)")
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
    return dict(provider=p_tot, local=a_tot, gate=g_tot, coverage=cov, capture_rate=cap, leak=leak)


def _provider_total(since):
    """Provider-billed LLM total (the TRUTH) since `since` — OpenAI + Anthropic, as billed. Returns None (UNKNOWN)
    if EITHER provider fetch fails — never a silent partial/zero that would masquerade as 'fully reconciled'."""
    total, err = 0.0, False
    try:
        from .report import openai_by_day
        oai, _ = openai_by_day()
        total += sum(v for d, v in oai.items() if d >= since)
    except Exception:
        err = True
    try:
        from . import reconcile_anthropic as anth
        an, _ = anth.cost_by_day(since=since)
        total += sum(v for d, v in an.items() if d >= since)
    except Exception:
        err = True
    return None if err else round(total, 2)


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


class RealtimeSource:
    """reconcile.Source adapter for REALTIME LLM. captured = gate-recorded realtime by project — the INLINE true-up:
    actual response tokens recorded AT CALL TIME (exact, no admin key, no reconstruction). truth = the providers'
    admin USAGE report (dev cross-check) when an admin key is present, else None — without it there is no provider
    realtime check and correctness rests on gate COVERAGE (`spendguard coverage`). attribute_gap=[]: realtime is NOT
    chat-reconstructable (proven — the tokens aren't in the transcripts), so a large residual is a COVERAGE gap to
    surface (ungated calls), never to fill with a guess. This makes the daily cross-check cover batch + realtime + GPU."""
    name = "realtime"

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
        # The admin usage oracle is DEV-ONLY + a network call, so it's OPT-IN (SPENDGUARD_ADMIN_ORACLE=1) — the
        # default reconcile stays offline + the shipped client (no admin key) never calls it. None = no provider
        # check; realtime correctness then rests on gate COVERAGE, which the completeness verdict surfaces.
        import os
        if not os.environ.get("SPENDGUARD_ADMIN_ORACLE"):
            return None
        from .report import admin_realtime_total
        return admin_realtime_total(since or self._since)     # None unless an admin key is also set

    def captured(self, since=None):
        from . import budget
        by = {}
        for (proj, _day), v in budget.gate_by_project_day(kind="realtime", since=since or self._since).items():
            by[proj] = by.get(proj, 0.0) + v
        return [{"cost": round(v, 2), "project": p} for p, v in by.items()]

    def attribute_gap(self, gap, since=None):
        return []                                             # not chat-reconstructable; residual = coverage gap, surfaced


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendguard reconcile-ledger")
    ap.add_argument("--since", help="YYYY-MM-DD (default: start of this month)")
    a = ap.parse_args(argv)
    sync(since=a.since)
    return 0
