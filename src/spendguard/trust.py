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
import datetime

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
    since = since or datetime.date.today().replace(day=1).isoformat()
    total, ok = 0.0, True
    try:
        from .report import openai_by_day
        oai, _ = openai_by_day()
        total += sum(v for d, v in oai.items() if d >= since)
    except Exception:
        ok = False
    try:
        from . import reconcile_anthropic as anth
        an, _ = anth.cost_by_day(since=since)
        total += sum(v for d, v in an.items() if d >= since)
    except Exception:
        ok = False
    try:
        from . import gate
        rt, _ = gate.realtime_by_day(since=since)
        total += sum(v for d, v in rt.items() if d >= since)
    except Exception:
        pass   # realtime is best-effort; the batch billing is the anchor
    return round(total, 2) if ok else None


def _ledger_llm_total(since):
    """What the LOCAL ledger recorded as LLM workload (batch + realtime, excluding meta + reconciled-truth rows) —
    the captured side that must reconcile to provider truth."""
    from . import budget
    by = budget.by_day(kind="batch", since=since, exclude_reconciled=True)
    rt = budget.by_day(kind="realtime", since=since)
    return round(sum(by.values()) + sum(rt.values()), 2)


def check(since=None, with_server=True):
    """Pull provider truth + the local ledger (+ the server total, if connected) and return the verdicts. The
    daily trust report. Free."""
    since = since or datetime.date.today().replace(day=1).isoformat()
    truth = provider_truth(since)
    ledger = _ledger_llm_total(since)
    out = {"since": since, "provider_truth": truth, "ledger": ledger}
    lvl, msg = verdict(truth, ledger)
    out["ledger_verdict"] = {"level": lvl, "msg": msg}
    if with_server:
        try:
            from . import saas
            x = saas.crosscheck(since=since)
            if not x.get("error"):
                out["server"] = {"rows": x.get("server_rows"), "value_drift": x.get("value_drift"),
                                 "server_only_stale": x.get("server_only"), "local_only": x.get("local_only"),
                                 "in_sync": x.get("in_sync")}
        except Exception:
            pass
    out["level"] = "alarm" if lvl == "alarm" else ("warn" if lvl == "warn" else lvl)
    return out


def cmd(argv=None):
    since = None
    if argv:
        for i, a in enumerate(argv):
            if a == "--since" and i + 1 < len(argv):
                since = argv[i + 1]
    r = check(since=since)
    lv = r["ledger_verdict"]
    icon = {"ok": "🟢", "warn": "🟡", "alarm": "🔴", "unknown": "⚪"}.get(lv["level"], "·")
    print(f"TRUST CHECK — provider billing vs recorded (since {r['since']})")
    print(f"  {icon} ledger: {lv['msg']}")
    if r.get("server"):
        s = r["server"]
        flag = "" if s.get("in_sync") else f"  ⚠ drift={s.get('value_drift')} stale-on-server={s.get('server_only_stale')} local-only={s.get('local_only')}"
        print(f"  server: {s.get('rows')} rows{flag}")
    if lv["level"] == "alarm":
        print("  *** ALARM: the recorded total is far above provider billing — investigate double-count before trusting/pushing. ***")
    return 2 if lv["level"] == "alarm" else 0
