"""Plan VALUE of subscription-lane calls that have NO session-log miner — priced from the calls ledger.

WHY THIS EXISTS. The claude-code and codex lanes get their est-value from session-log miners (`spendguard cc`,
`spendguard codex`): those CLIs write transcripts we mine, capturing ALL plan usage, not just spendguard's own
prompts. The gemini (Antigravity `agy`) and zai (GLM Coding) lanes have no such transcript source — so when the
advisor delegates to them, the call is recorded ($0 billed, kind='subscription', executor=<lane>) but its plan
VALUE was invisible: $0 on the est-value axis, which also blinded the load-balancer's utilization brain to them.

WHAT IT DOES. Every subscription call already carries its token counts and its lane in the `calls` ledger. This
module prices those tokens at API-equivalent rates (`pricing.realtime_cost`) and stamps the per-lane est-value
windows (`receipt.stamp_est_value`, billed=False) — the same cache the session miners feed. So `spendguard receipt`
and the in-chat footer show plan value for these lanes too.

WHICH LANES. Exactly the lanes with no session miner, DERIVED: every executor in `adapters._LANES` minus the ones a
miner already values (`receipt._SOURCE_REFRESH` is that registry). A new lane with no miner is valued automatically;
a lane that later gains a miner drops out — nothing to edit here (the no-hardcoding rule, held structurally).

HONEST SCOPE. This values the calls spendguard DELEGATED through the lane (the ones in our ledger) — not other,
non-spendguard usage of that CLI, for which we have no log source. It is the value of the work spendguard sent,
priced at what the metered API would have charged; $0 billed, because a flat-fee plan served it.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import json
import sqlite3
import time

from . import config

_WINDOW_DAYS = 40           # scan this far back so stamp_est_value has the per-day detail to re-bucket a month window
_REFRESH_MAX_AGE_S = 180    # the receipt path calls refresh_lane_value_if_stale(); only re-price when older than this

# The SERVER CHANNEL a lane executor's est-value pushes under. Identity for most; the ONE quirk is the zai-coding
# LANE, which the server/client ledger contract names 'zai' (server core.ts CHANNELS). Derived-with-one-override,
# never a hardcoded per-lane table (the no-hardcoding rule).
_LANE_CHANNEL = {"zai-coding": "zai"}


def _channel_for(executor: str) -> str:
    return _LANE_CHANNEL.get(executor, executor)


def ledger_valued_lanes() -> set:
    """Lane executors whose plan value is priced HERE from the ledger — every subscription lane EXCEPT those a
    session-log miner already values. Derived from the lane registry and the miner registry, never a hardcoded list,
    so adding/removing a lane or a miner needs no edit in this file."""
    from . import adapters, receipt
    lanes = {name for name, _mod in adapters._LANES.values()}
    session_mined = set(receipt._SOURCE_REFRESH)          # claude-code / codex / claude-ai — mined from transcripts
    return {ln for ln in lanes if ln not in session_mined}


def _subscription_rows(since: str, valued: set):
    """(executor, ts, provider, model, in_tok, out_tok, project) for kind='subscription' ledger rows on the valued
    lanes since `since`. Read-only, its own short-lived connection (closed on every path). [] on any error."""
    if not valued:
        return []
    try:
        qmarks = ",".join("?" for _ in valued)
        sql = ("SELECT executor, ts, provider, model, in_tok, out_tok, project FROM calls "
               f"WHERE kind='subscription' AND executor IN ({qmarks}) AND ts >= ?")
        params = (*sorted(valued), since)
        with contextlib.closing(sqlite3.connect(config.db_path())) as c:
            return c.execute(sql, params).fetchall()
    except Exception:
        return []


def stamp_from_ledger(window_days: int = _WINDOW_DAYS) -> dict:
    """Price the valued lanes' subscription calls at API rates and stamp each lane's est-value windows. Returns
    {executor: month_value_usd_stamped} for a caller/report. Best-effort; never raises."""
    out = {}
    try:
        from . import receipt
        valued = ledger_valued_lanes()
        if not valued:
            return out
        since = (receipt._utc_today() - _dt.timedelta(days=window_days)).strftime("%Y-%m-%d")
        by_lane = {}
        from . import lane_catalog
        for ex, ts, prov, model, in_tok, out_tok, project in _subscription_rows(since, valued):
            try:
                # price through the CATALOG (base + the lane's provider, reasoning-suffix aware) — not a raw
                # realtime_cost that leans on the global suffix-strip and can hit the gemini/vertex ambiguity.
                val = lane_catalog.use_name_cost(model, int(in_tok or 0), int(out_tok or 0), lane=ex) or 0.0
            except Exception:
                val = 0.0                                 # an unpriced model contributes 0, never a guess (chunk-safe)
            by_lane.setdefault(ex, []).append(
                {"day": (ts or "")[:10], "spend_micros": round(val * 1_000_000), "billed": False,
                 "org": "", "team": "", "project": project or ""})
        for ex, rows in by_lane.items():
            receipt.stamp_est_value(rows, source=ex)      # billed=False → the est-value axis, per-source (per-lane)
            out[ex] = sum(r["spend_micros"] for r in rows) / 1_000_000
        return out                                        # SUCCESS (out may be {} = no valued lanes / no rows)
    except Exception:
        return None                                       # ERROR — a distinct signal so refresh_lane_value_if_stale
        #                                                   does NOT reset the freshness timer on a failed stamp


def _mark_stamped() -> None:
    """Record WHEN the ledger lanes were last priced, so refresh_lane_value_if_stale can cheap-skip until stale."""
    try:
        from . import receipt
        p = receipt._cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        config.update_json(p, lambda d: d.__setitem__("lane_value_asof", time.time()) or d, reason="lane-value")
    except Exception:
        pass


def refresh_lane_value_if_stale(max_age_s: int = _REFRESH_MAX_AGE_S) -> None:
    """Re-price the ledger-valued lanes only when the last stamp is older than `max_age_s`. Called from the every-turn
    receipt path — the staleness gate keeps that path a single cache read on the common case. Best-effort; the mark is
    set even when there was nothing to stamp, so an idle machine does not re-scan every turn."""
    try:
        from . import receipt
        p = receipt._cache_path()
        last = 0
        if p.exists():
            try:
                last = json.loads(p.read_text()).get("lane_value_asof") or 0
            except Exception:
                last = 0
        if (time.time() - float(last or 0)) < max_age_s:
            return
    except Exception:
        return
    if stamp_from_ledger() is not None:               # mark fresh ONLY when the stamp SUCCEEDED (even if it found no
        _mark_stamped()                                # rows); an ERROR (None) must retry next tick, not sleep max_age_s


def day_totals(member_ref, window_days: int = _WINDOW_DAYS):
    """The SERVER-PUSH twin of stamp_from_ledger (which stamps the LOCAL receipt): the ledger-valued lanes' plan
    VALUE as /v1/ledger est-value rows (billed=False, channel=<lane>), aggregated per (project, day, provider, model,
    lane). Same pricing, shaped for the org dashboard so gemini/zai plan usage is visible THERE, not just locally —
    the client half of the server's EST_VALUE_CHANNELS. [] on no rows/error. day_totals is the est-value-source
    PROTOCOL (chat/claudecode implement it too), dispatched by each source's own sync."""
    from . import lane_catalog, receipt
    valued = ledger_valued_lanes()
    if not valued:
        return []
    since = (receipt._utc_today() - _dt.timedelta(days=window_days)).strftime("%Y-%m-%d")
    agg, skipped_no_day = {}, 0
    for ex, ts, prov, model, in_tok, out_tok, project in _subscription_rows(since, valued):
        day = (ts or "")[:10]
        if not day:
            skipped_no_day += 1                           # can't attribute to a day → COUNT it, never drop silently
            continue
        try:
            val = lane_catalog.use_name_cost(model, int(in_tok or 0), int(out_tok or 0), lane=ex) or 0.0
        except Exception:
            val = 0.0                                     # unpriced model → $0 VALUE, but the CALL is still real WORK
        # keep the row even at $0 (matches stamp_from_ledger): the call counts toward work-done; a $0-only project is
        # dropped by the server's `having sum(spend_micros) > 0`, so it adds no noise and loses no real spend.
        key = (project or "", day, prov or "?", model or "?", ex)
        e = agg.setdefault(key, {"value": 0.0, "calls": 0, "in_tok": 0, "out_tok": 0})
        e["value"] += val
        e["calls"] += 1
        e["in_tok"] += int(in_tok or 0)
        e["out_tok"] += int(out_tok or 0)
    if skipped_no_day:
        config.warn_once(f"[lane_value] {skipped_no_day} lane subscription row(s) had no timestamp — excluded from the "
                         f"push (their plan value is a lower bound by that much until the rows carry a day).")
    return [{"day": day, "provider": prov, "model": model, "kind": "workload", "channel": _channel_for(ex),
             "billed": False, "spend_micros": round(e["value"] * 1_000_000), "calls": e["calls"],
             "in_tokens": e["in_tok"], "out_tokens": e["out_tok"], "member_ref": member_ref, "project": project}
            for (project, day, prov, model, ex), e in agg.items()]


def sync(dry=False):
    """Push the ledger-valued lanes' (gemini/zai) plan VALUE (billed=False, channel=<lane>) to the server, so the org
    dashboard's work-done / unit-economics count them (the client twin of the server's EST_VALUE_CHANNELS). Filters to
    THIS connection's owned projects (no cross-attribution); declares a per-channel replace so re-syncs never stack.
    Runs inside `saas sync` (hourly via launchd). Best-effort; never raises out."""
    from . import saas
    c = saas.saas_connection()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private"}
    cok, cwhy = saas.contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    stamp_from_ledger()                                   # keep the LOCAL receipt stamp fresh too (best-effort)
    rows = day_totals(saas.contributor())
    flt = saas._project_filter(c)                         # only this connection's projects (same guard as build_rollup_rows)
    if flt is not None:
        rows = [r for r in rows if (r.get("project") or "").lower() in flt]
    for r in rows:
        r["uid"] = saas._row_uid(r)
    if dry:
        return {"day_totals": rows}
    if not rows:
        return {"skipped": "no ledger-valued lane value for this connection"}
    channels = sorted({r["channel"] for r in rows})      # replace per (channel, billed=False) so re-syncs don't stack
    try:
        return saas._request("POST", "/v1/ledger", {"visibility": c.get("visibility"), "day_totals": rows,
                             "replace": [{"channel": ch, "billed": False} for ch in channels]})
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/ledger endpoint yet"}
        raise


def main(argv=None) -> int:
    """`spendguard lanevalue` — force a re-price of the ledger-valued lanes and print each lane's plan value. This is
    the refresh command the stale-receipt caption points at for these lanes (mirrors `spendguard codex`).
    `--sync` pushes the plan value to the server (billed=false, channel=<lane>); `--sync --dry` shows what would go."""
    argv = list(argv or [])
    if "--sync" in argv:
        r = sync(dry="--dry" in argv)
        if r.get("skipped"):
            print(f"lanevalue --sync skipped: {r['skipped']}")
        elif "day_totals" in r:                           # --dry: the rows, not pushed
            rows = r["day_totals"]
            chans = sorted({x["channel"] for x in rows})
            print(f"lanevalue --sync --dry: {len(rows)} est-value row(s) → channels {chans or '—'} (billed=false, NOT pushed)")
        else:
            print(f"lanevalue --sync: pushed gemini/zai plan value (billed=false) → {r}")
        return 0
    stamped = stamp_from_ledger()
    if stamped is not None:                            # only reset freshness on success; None = error (don't mask it)
        _mark_stamped()
    stamped = stamped or {}
    valued = sorted(ledger_valued_lanes())
    print("Ledger-valued subscription lanes (no session-log miner) — plan VALUE priced from the calls ledger:")
    if not valued:
        print("  (no such lanes configured)")
        return 0
    for ln in valued:
        v = stamped.get(ln)
        if v is None:
            print(f"  {ln:<16} —        (no subscription calls in the last {_WINDOW_DAYS}d)")
        else:
            print(f"  {ln:<16} ${v:,.2f}  (est value stamped; window {_WINDOW_DAYS}d)")
    print("  ⚠ USAGE VALUE (tokens × API pricing), NOT $ billed — a flat-fee plan served these. Shown on the")
    print("    est-value axis of `spendguard receipt`, never added into real spend.")
    return 0


if __name__ == "__main__":      # python -m spendguard.lane_value
    raise SystemExit(main())
