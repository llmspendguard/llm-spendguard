"""Trust check — cross-check the AUTHORITATIVE provider billing against what spendguard RECORDED locally and PUSHED
to the server, so a double-count or drift can't hide. The lesson from the 2x prod incident: the only ground truth
is the provider's own bill; everything else must reconcile to it, loudly, every day.

  • provider_truth(since)  — OpenAI + Anthropic billing + gate-logged realtime (the authoritative $). None if a
                             fetch fails (NEVER a fake 0 that would read as "all good").
  • check(since)           — compare provider truth vs the local ledger AND the server total → a verdict per side.
  • CLI `spendguard trust` — prints the verdict, exits non-zero on ALARM (so a daily scheduled run surfaces it).
  • used as a PRE-PUSH GATE in saas.sync: a ledger that is >ALARM_RATIO× provider truth is NOT pushed (fail-closed),
    so the double-count class can't reach prod again.

Free (provider GETs + one server GET). Run daily.
"""

from . import config

WARN_FRAC = 0.15      # |recorded − truth| / truth beyond this → WARN
ALARM_RATIO = 1.4     # recorded ≥ this × truth → ALARM (almost certainly double-counting / accumulation)


def verdict(truth, recorded):
    """PURE: classify a recorded total against provider truth. Returns (level, message). level ∈
    unknown | ok | warn | alarm. truth=None (fetch failed) → UNKNOWN (never silently 'ok')."""
    if truth is None:
        return ("unknown", "provider truth UNKNOWN — billing fetch failed; cannot verify (fix the key/network), do NOT trust the total")
    if truth <= 0:
        return ("ok" if (recorded or 0) <= 0 else "warn",
                f"provider shows $0 but recorded ${recorded:.2f}" if (recorded or 0) > 0 else "no provider spend this period")
    ratio = (recorded or 0) / truth
    pct = (ratio - 1) * 100
    if ratio >= ALARM_RATIO:
        return ("alarm", f"recorded ${recorded:.2f} is {ratio:.2f}× the provider-billed ${truth:.2f} — likely DOUBLE-COUNT / accumulation")
    if abs((recorded or 0) - truth) / truth > WARN_FRAC:
        return ("warn", f"recorded ${recorded:.2f} vs provider-billed ${truth:.2f} ({pct:+.0f}%) — investigate")
    return ("ok", f"recorded ${recorded:.2f} ≈ provider-billed ${truth:.2f} ({pct:+.0f}%)")


def provider_truth(since=None):
    """The authoritative LLM $ this period (OpenAI + Anthropic batch billing + gate-logged realtime). Returns a float,
    or None if EITHER provider fetch fails — a partial/zero must never masquerade as the truth."""
    # NORMALIZE to a string once: `since` arrives straight from CLI `--since` unparsed, and a source's day keys
    # may be strings OR date objects — a mixed-type `d >= since` raised TypeError INSIDE the broad except below,
    # silently flipping the whole provider_truth to UNKNOWN as if the fetch had failed. str-vs-str can't.
    since = str(since or config.month_start_utc())
    total, ok = 0.0, True
    try:
        from .report import openai_by_day
        oai, _ = openai_by_day()
        total += sum(v for d, v in oai.items() if str(d) >= since)
    except Exception:
        ok = False
    try:
        from . import reconcile_anthropic as anth
        an, _ = anth.cost_by_day(since=since)
        total += sum(v for d, v in an.items() if str(d) >= since)
    except Exception:
        ok = False
    try:
        from . import gate
        rt, _ = gate.realtime_by_day(since=since)
        total += sum(v for d, v in rt.items() if str(d) >= since)
    except Exception:
        pass   # realtime is best-effort; the batch billing is the anchor
    return round(total, 2) if ok else None


def _ledger_llm_total(since):
    """What the LOCAL ledger recorded as LLM workload — the captured side that must reconcile to provider truth.
    APPLES-TO-APPLES with provider_truth(), axis by axis:
      • batch: gate estimate rows NETTED with their true-down corrections (ledger_sync.true_down brings estimates
        to billed actuals at reconcile) ↔ truth's provider-billed batch. Excludes '(provider-batch)' backfill —
        those rows MIRROR the provider side and would double-count against it.
      • realtime: gate-live rows (actual tokens recorded at call time) ↔ truth's gate realtime_log (the same
        capture). Excludes the realtime reconcile markers (history/oracle/reconstructed) — they mirror sources
        that are NOT in provider_truth, so counting them here inflates only the recorded side (the old +$13
        phantom drift).
    Meta is excluded on both sides (it's spendguard's own spend, tracked separately)."""
    from . import budget
    by = budget.by_day(kind="batch", since=since, exclude_reconciled=True)
    rt = budget.by_day(kind="realtime", since=since, exclude_reconciled=True)
    return round(sum(by.values()) + sum(rt.values()), 2)


def trust_report(since=None, with_server=True):
    """Pull provider truth + the local ledger (+ the server total, if connected) and return the verdicts. The
    daily trust report. Free."""
    since = since or config.month_start_utc()
    truth = provider_truth(since)
    ledger = _ledger_llm_total(since)
    out = {"since": since, "provider_truth": truth, "ledger": ledger}
    lvl, msg = verdict(truth, ledger)
    out["ledger_verdict"] = {"level": lvl, "msg": msg}
    if with_server:
        try:
            from . import saas
            x = saas.crosscheck(since=since)
            if x.get("error"):
                # A crosscheck that FAILED (network/auth) is NOT the same as "no server": dropping it left the CLI
                # silent and the overall level untouched, so an UNVERIFIED server side read as fine. Surface it so
                # the run never claims a sync it did not confirm.
                out["server_error"] = x["error"]
            else:
                out["server"] = {"rows": x.get("server_rows"), "value_drift": x.get("value_drift"),
                                 "server_only_stale": x.get("server_only"), "local_only": x.get("local_only"),
                                 "in_sync": x.get("in_sync")}
        except Exception as e:
            out["server_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    out["level"] = "alarm" if lvl == "alarm" else ("warn" if lvl == "warn" else lvl)
    # SERVER DRIFT MUST ELEVATE THE OVERALL LEVEL. out['level'] used ONLY the ledger verdict, so a server that was
    # out of sync (in_sync False) left the overall status at the ledger's 'ok' — a drift nobody was alerted to. A
    # drift is at least a warn; it never downgrades an existing alarm and never masks an 'unknown' billing fetch
    # (so the elevation is gated on 'ok', not '!= alarm').
    srv = out.get("server")
    if srv and srv.get("in_sync") is False and out["level"] == "ok":
        out["level"] = "warn"
    # A server crosscheck we couldn't RUN is unverified too — at least a warn, never a silent ok.
    if out.get("server_error") and out["level"] == "ok":
        out["level"] = "warn"
    return out


def cmd(argv=None):
    since = None
    if argv:
        for i, a in enumerate(argv):
            if a == "--since" and i + 1 < len(argv):
                since = argv[i + 1]
    r = trust_report(since=since)
    lv = r["ledger_verdict"]
    icon = {"ok": "🟢", "warn": "🟡", "alarm": "🔴", "unknown": "⚪"}.get(lv["level"], "·")
    print(f"TRUST CHECK — provider billing vs recorded (since {r['since']})")
    print(f"  {icon} ledger: {lv['msg']}")
    if r.get("server"):
        s = r["server"]
        flag = "" if s.get("in_sync") else f"  ⚠ drift={s.get('value_drift')} stale-on-server={s.get('server_only_stale')} local-only={s.get('local_only')}"
        print(f"  server: {s.get('rows')} rows{flag}")
    elif r.get("server_error"):
        print(f"  server: ⚠ crosscheck FAILED — {r['server_error']} (server sync UNVERIFIED)")
    if lv["level"] == "alarm":
        print("  *** ALARM: the recorded total is far above provider billing — investigate double-count before trusting/pushing. ***")
    elif r["level"] == "unknown":
        print("  *** CANNOT VERIFY: provider billing fetch failed — do NOT trust the total (fix the key/network). ***")
    # Exit on the OVERALL level (r['level']) — which folds in server drift AND an unverifiable billing fetch — not
    # the ledger verdict alone. 'unknown' (couldn't fetch billing) exits a non-zero 3, so a daily/CI run never reads
    # a failed verification as success.
    return {"alarm": 2, "warn": 1, "unknown": 3}.get(r["level"], 0)
