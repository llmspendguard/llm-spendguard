"""Receipt tally and compute layer: aggregate est-value and billed spend over the ledger into the tally data structure, including windows, est cells, routed-lane value, unpriced, truncation, and quarantine rows, remote and external tallies, proportional subscription-plan dollars and labels, and the public tally, level, and stamp APIs. This bottom leaf performs pure computation over ledger reads and configuration; it formats no output, performs no live emit, and never imports the render, emit, or CLI layers."""
from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
from typing import Optional

from spendguard.generate.execution import execution_accessors as _execution_accessors

from ... import config
from ...money_scale import NANO_USD_PER_USD
from ...sources import CLAUDE_CODE_SOURCE, CLAUDE_CHAT_SOURCE, CODEX_SOURCE

LEVELS = ("off", "footer", "flow", "verbose")
_CACHE = "receipt_cache.json"          # under SPENDGUARD_HOME — est-value windows stamped by cc/chat/sync


# ── verbosity ────────────────────────────────────────────────────────────────
def level() -> str:
    """Resolved receipts verbosity: env SPENDGUARD_RECEIPTS → config `receipts` → 'flow'. Unknown → 'flow'."""
    v = (os.getenv("SPENDGUARD_RECEIPTS") or config._cfg_get("receipts", "level", "flow") or "flow")
    v = str(v).strip().lower()
    return v if v in LEVELS else "flow"


# ── formatting ─────────────────────────────────────────────────────────────--
def _money(x: Optional[float]) -> str:
    return "—" if x is None else f"${x:,.2f}"


def _tok(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    if not n:
        return "0"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _pct(est: Optional[float], actual: Optional[float]) -> str:
    """Signed variance of actual vs estimate, e.g. '−11%'. Empty when either side is missing/zero."""
    if not est or actual is None:
        return ""
    d = (actual - est) / est
    sign = "−" if d < 0 else "+"
    return f" ({sign}{abs(d) * 100:.0f}%)"


# ── date windows (UTC, matching budget's day strings) ─────────────────────────
def _utc_today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _windows():
    t = _utc_today()
    return (t.strftime("%Y-%m-%d"),
            (t - _dt.timedelta(days=6)).strftime("%Y-%m-%d"),   # rolling 7d (incl. today)
            t.replace(day=1).strftime("%Y-%m-%d"))


# ── est-value cache (stamped by the heavier flows; read cheaply here) ─────────
_BY_DAY_KEEP = 40      # days of per-day detail kept so a later read can re-bucket (a month window spans ≤31)


def _receipt_aggregate_cache_path():
    return config.HOME / _CACHE


def _rewindow(rec):
    """Re-bucket a stamped record against TODAY's windows using its per-day detail, and report whether it could.

    Returns (windows, fresh). `fresh` is False for a pre-0.8.3 record (no `by_day`) stamped on an earlier day: its
    today/week/month were frozen at stamp time, so they are NOT this month's — the reader must say so rather than
    print a stale number as current."""
    by_day = rec.get("by_day") or {}
    today, week, month = _windows()
    if by_day:
        w = {"today": 0.0, "week": 0.0, "month": 0.0}
        for day, usd in by_day.items():
            for k, lo in (("today", today), ("week", week), ("month", month)):
                if day >= lo:
                    w[k] += usd
        return w, True
    frozen = {k: rec.get(k, 0) or 0 for k in ("today", "week", "month")}
    return frozen, (rec.get("asof") or "") >= today


def stamp_est_value(rows, source: str = CLAUDE_CODE_SOURCE) -> None:
    """Persist this SOURCE's est-value (billed=False) windows so the footer can show them without a heavy recompute.
    `rows` are day_totals-shaped dicts (day + spend_micros + billed). Stored per-source so claude-code and claude.ai
    sum instead of clobbering each other. Best-effort; never raises into the caller. Pass the FULL (unfiltered)
    history so the month window is complete regardless of any report's day filter."""
    try:
        today, week, month = _windows()
        # ORG → TEAM → PROJECT attribution (the useful axis, same as the server). Stored as flat cells keyed
        # org|team|project; the tree is built on render. (Each row carries the agentic classification from cls.)
        acc = {"today": 0.0, "week": 0.0, "month": 0.0, "asof": today, "cells": {}, "by_day": {}}
        keep = (_utc_today() - _dt.timedelta(days=_BY_DAY_KEEP)).strftime("%Y-%m-%d")
        for r in rows or []:
            if r.get("billed"):                       # only the est-value axis belongs in this cache
                continue
            day = r.get("day") or ""
            usd = (r.get("spend_micros") or 0) / 1_000_000
            org = (r.get("org") or "").strip().lower()
            team = (r.get("team") or "").strip().lower()
            proj = (r.get("project") or "").strip().lower()
            ca = acc["cells"].setdefault(f"{org}|{team}|{proj}",
                                         {"org": org, "team": team, "project": proj, "today": 0.0, "week": 0.0,
                                          "month": 0.0, "by_day": {}})
            # Keep the raw per-day totals (bounded window) so a LATER read re-buckets against ITS OWN windows.
            # Without this the cache freezes June's "month" and a July receipt reports it as this month's — the
            # exact stale-number-presented-as-current bug this tool exists to prevent.
            if day >= keep:
                acc["by_day"][day] = acc["by_day"].get(day, 0.0) + usd
                ca["by_day"][day] = ca["by_day"].get(day, 0.0) + usd
            for k, lo in (("today", today), ("week", week), ("month", month)):
                if day >= lo:
                    acc[k] += usd
                    ca[k] += usd
        p = _receipt_aggregate_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # THROUGH THE ONE WRITER: an unreadable cache used to discard every other source's est-value.
        from ... import config as _c
        _c.update_json(p, lambda d: d.setdefault("est_value_by_source", {}).update({source: acc}),
                       reason="est-value")
    except Exception:
        pass


def _receipt_est_records():
    """Ledger values supersede cached miners for each source family; caches cover unmined lanes.

    Open the ledger read-only: receipt rendering must never create, migrate, or checkpoint it.
    Numeric TEXT is cast per row before aggregation, independently of every real-dollar column.
    """
    import sqlite3
    from spendguard.store.spend_search.ledger_read_accessors import resolve__ledger_read_connection as _ledger_read_connection, resolve_consumer_empty_spend_compatibility as consumer_empty_spend_compatibility
    from ...sources import receipt_source_family
    from ...ledger import SpendLedger

    records = {}
    try:
        records = json.loads(_receipt_aggregate_cache_path().read_text()).get("est_value_by_source") or {}
    except (OSError, ValueError):
        pass
    # A cache-only installation has no ledger yet. Preserve its source records
    # (including attribution and as-of dates) without creating a database merely
    # to display a receipt. Existing ledgers still use the bounded reader below.
    if not pathlib.Path(config.db_path()).exists():
        return records
    ledger_records = {}
    today, week, month = _windows()
    try:
        with consumer_empty_spend_compatibility(), _ledger_read_connection() as conn:
            rows = conn.execute(
                "SELECT source, day, org, team, project_primary, "
                f"SUM(COALESCE(est_chat_usd_nano, 0))/{NANO_USD_PER_USD}.0 AS usd FROM spend_events "
                "WHERE day >= ? AND est_chat_usd IS NOT NULL AND billed = 0 "
                f"AND {SpendLedger._LIVE_FILTER} "
                "GROUP BY source, day, org, team, project_primary", (min(week, month),)).fetchall()
        for source, day, org, team, project, usd in rows:
            family = receipt_source_family(source)
            rec = ledger_records.setdefault(family, {"asof": today, "by_day": {}, "cells": {}})
            rec["by_day"][day] = rec["by_day"].get(day, 0) + usd
            org, team, project = ((value or "").strip().lower() for value in (org, team, project))
            cell = rec["cells"].setdefault((org, team, project),
                                          {"org": org, "team": team, "project": project, "by_day": {}})
            cell["by_day"][day] = cell["by_day"].get(day, 0) + usd
    except sqlite3.Error:
        pass
    # Remove aliases together with their canonical source, otherwise an overflow cache would double count.
    return {**{key: value for key, value in records.items()
               if receipt_source_family(key) not in ledger_records}, **ledger_records}


def _est_cells():
    """All est-value cells across sources: [{org, team, project, today, week, month}]. The flat store the tree is
    built from."""
    out = []
    try:
        for s in _receipt_est_records().values():
            for c in (s.get("cells") or {}).values():
                w, _fresh = _rewindow(c)               # today's windows, not the ones frozen at stamp time
                out.append(dict(c, **w))
    except Exception:
        pass
    return out


def _est_tally(org=None, team=None, project=None, repo=None):
    """Est-value windows {today, week, month, asof} summed across sources, optionally scoped. `org`/`team`/`project`
    filter the org→team→project attribution (AND). `repo` scopes to a REPO the way the per-repo breakdown does — a
    cell counts if its TEAM or its PROJECT equals the repo (a repo surfaces as either axis in the classification).
    That is the scope a per-repo receipt needs: without it, `tally(project=repo)` scoped the API-$ but returned the
    GLOBAL plan value, so every repo line showed the whole plan total (and `_sum_repos` added it once PER repo — an
    N× over-count). No filter = global. None if never stamped."""
    try:
        srcs = _receipt_est_records()
        if srcs:
            rew = [(k, s, _rewindow(s)) for k, s in srcs.items()]
            stale_srcs = [(k, s) for k, s, (_w, fresh) in rew if not fresh]   # frozen records, stamped earlier
            stale = bool(stale_srcs)
            # When ANY source is stale the caption must describe the STALE one (its OLDEST date) and name it —
            # reporting the freshest source's as-of would hide exactly the record the reader needs to distrust.
            asof = (min((s.get("asof", "") for _k, s in stale_srcs), default="") if stale
                    else max((s.get("asof", "") for s in srcs.values()), default=""))
            names = [k for k, _s in stale_srcs]
            if org is None and team is None and project is None and repo is None:
                return {"today": sum(w["today"] for _k, _s, (w, _f) in rew),
                        "week": sum(w["week"] for _k, _s, (w, _f) in rew),
                        "month": sum(w["month"] for _k, _s, (w, _f) in rew),
                        "stale": stale, "stale_sources": names, "asof": asof}
            ol = None if org is None else org.strip().lower()
            tl = None if team is None else team.strip().lower()
            pl = None if project is None else project.strip().lower()
            rl = None if repo is None else repo.strip().lower()
            agg = {"today": 0.0, "week": 0.0, "month": 0.0}
            for c in _est_cells():
                if rl is not None:                     # REPO scope: the cell counts if its team OR its project is the
                    if c.get("team") != rl and c.get("project") != rl:   # repo — the SAME mapping _est_breakdown
                        continue                        # uses, so a repo's line and its project breakdown agree
                else:
                    if ol is not None and c.get("org") != ol:
                        continue
                    if tl is not None and c.get("team") != tl:
                        continue
                    if pl is not None and c.get("project") != pl:
                        continue
                for k in agg:
                    agg[k] += c.get(k, 0)
            agg["asof"] = asof
            agg["stale"] = stale
            agg["stale_sources"] = names
            return agg
        data = json.loads(_receipt_aggregate_cache_path().read_text())
        d = data.get("est_value")                     # back-compat: older single-blob stamp (global only)
        if org is None and team is None and project is None and repo is None and isinstance(d, dict) and "today" in d:
            return d
    except Exception:
        pass
    return None


def _est_by_source():
    """Per-SOURCE (per-lane) est-value windows {source: {today, week, month}}, re-bucketed to today's windows — the
    breakdown BEHIND the summed `_est_tally`. Lets the receipt show WHICH plan/lane delivered the value, instead of a
    single collapsed number. None of the sources are summed here; the caller decides."""
    out = {}
    try:
        for k, s in _receipt_est_records().items():
            w, _fresh = _rewindow(s)
            out[k] = w
    except Exception:
        pass
    return out


def _routed_lane_value():
    """Current-month routed value by active lane from the calls ledger.

    Values are floats when every in-window call was priced and None when any such call was unpriceable. The separate
    cache namespace is intentional: transcript miners continue to own `est_value_by_source` and the footer total.
    """
    out = {}
    try:
        data = json.loads(_receipt_aggregate_cache_path().read_text()).get("routed_lane_value") or {}
        month = _windows()[2]
        active = {name for name, _module in _execution_accessors.resolve__LANES().values()}
        for lane in active:
            rec = data.get(lane) or {}
            activity = sum(n for day, n in (rec.get("activity_by_day") or {}).items() if day >= month)
            if not activity:
                continue
            unpriced = sum(n for day, n in (rec.get("unpriced_by_day") or {}).items() if day >= month)
            out[lane] = None if unpriced else sum(
                usd for day, usd in (rec.get("by_day") or {}).items() if day >= month)
    except Exception:
        pass
    return out


def _lane_activity(since=None):
    """Which SUBSCRIPTION LANE actually served spendguard's own work, from the calls ledger since `since` (default
    month start): {executor: {calls, in_tok, out_tok}}. This is the thing the user asked to see inline — a plan
    served these prompts ($0 billed), and now the receipt can name it instead of it being invisible. Cheap indexed
    read; {} on any error or on an older db written before the executor column existed (guarded, never raises)."""
    since = since or _windows()[2]
    from spendguard.store.spend_search.ledger_read_accessors import resolve_read_receipt_lane_activity as read_receipt_lane_activity
    try:
        rows = read_receipt_lane_activity(since)
        return {r[0]: {"calls": r[1], "in_tok": r[2], "out_tok": r[3]} for r in rows if r[0]}
    except Exception:
        return {}


# Optional commands that broaden a lane's ledger-derived floor with all host CLI usage. This is not an exclusion
# registry: every active lane is refreshed from calls on the receipt path, whether or not it has a miner.
_SOURCE_REFRESH = {CLAUDE_CODE_SOURCE: "spendguard cc", CODEX_SOURCE: "spendguard codex",
                   CLAUDE_CHAT_SOURCE: "spendguard chat"}


def _basis_line():
    """What KIND of number the Actual-$ column is. A total that mixes a provider bill with a max_tokens
    ceiling is not wrong, but reading it as money already spent IS — and nothing on the receipt used to say
    which was which. Unlabelled legacy rows are shown as unlabelled, never quietly counted as billed."""
    try:
        import spendguard.manage.admission.admission_accessors as budget
        b = budget.resolve_by_basis(_windows()[2])
    except Exception:
        return []
    tot = sum(v["cost"] for v in b.values())
    if tot <= 0:
        return []
    order = [(budget.BASIS_BILLED, "billed"), (budget.BASIS_ESTIMATE, "estimate (ceiling until reconciled)"),
             (budget.BASIS_RECONSTRUCTED, "reconstructed"), (budget.BASIS_ASSUMED, "assumed"),
             ("", "unlabelled (recorded before basis existed)")]
    # Sub-cent buckets render as "<$0.01" rather than "$0.00": a bucket that exists should not be displayed as
    # if it held nothing, and rounding it away would be the small version of hiding money.
    parts = [f"{lbl} " + ("<$0.01" if b[k]["cost"] < 0.005 else _money(b[k]["cost"]))
             for k, lbl in order if b.get(k, {}).get("cost")]
    return ["basis of the Actual $: " + " · ".join(parts)] if parts else []


def _unpriced_lines():
    """Calls we could not price. The MIRROR of quarantine: that holds money that cannot be real, this holds
    real usage whose price is unknown. Both stay out of the total; both must be visible, because a silent $0
    is a claim that the work was free — and that is the one thing we know it wasn't."""
    try:
        from ... import model_catalog, pricing
        import spendguard.manage.admission.admission_accessors as budget
        rows = [row for row in budget.resolve_unpriced_since(_windows()[2])
                if not model_catalog.billing_exempt(row["model"])]
    except Exception:
        return []
    if not rows:
        return []
    unknown_rows, pending_rows = [], []
    for row in rows:
        try:
            pricing.price(row["model"])
        except pricing.PriceUnknownError:
            unknown_rows.append(row)
        else:
            pending_rows.append(row)
    n = sum(r["calls"] for r in unknown_rows)
    out = ([f"⚠ NOT IN THE TOTAL: {n} call(s) on {len(unknown_rows)} model(s) spendguard cannot price — real usage, "
            f"unknown $ (never counted as free):"] if unknown_rows else [])
    for r in unknown_rows:
        out.append(f"    {r['model']:<28} {r['calls']:>4} calls   "
                   f"spendguard price {r['model']} --in <$/1M> --out <$/1M> "
                   "--source '<url>' --verified <YYYY-MM-DD> (writes the model catalog SSOT)")
    if pending_rows:
        pending_calls = sum(row["calls"] for row in pending_rows)
        out.append(f"⚠ pending_reprice_coverage: {pending_calls} historical call(s) now have catalog prices; "
                   "ledger costs remain unrepaired and excluded from the total until repriced:")
        for row in pending_rows:
            out.append(f"    {row['model']:<28} {row['calls']:>4} calls   "
                       f"spendguard reprice --model {row['model']}")
    return out


def _truncated_lines():
    """Money spent on answers that were CUT OFF. In the total, because it was really billed — but named,
    because it bought nothing.

    The third member of the family: quarantine holds money that cannot be real, unpriced holds real usage
    whose price is unknown, and this holds real money that produced no usable result. A call that ends at
    max_tokens billed for the whole input and a partial body, and an incomplete JSON object reads downstream
    as 'no findings' rather than 'no answer' — so it is worse than a visible failure. It was invisible here
    because it sits inside a legitimate-looking total."""
    try:
        from spendguard.store.spend_search.ledger_read_accessors import resolve__ledger_read_connection as _ledger_read_connection
        with _ledger_read_connection() as c:
            return _truncation_rows(c)
    except Exception:
        return []


#: What a truncation can actually be DONE about, keyed by the cap it truncated at. Not cosmetic: each
#: class has a different remedy and only one of the three is the cap-raise the receipt used to promise.
AT_CEILING, NO_CAP, RAISABLE = "AT CEILING", "NO CAP SET", "RAISABLE"


def truncation_remedy(c, model):
    """How THIS model's truncations can actually be fixed, read from the caps it truncated AT.

    THE HINT THIS REPLACES WAS WRONG TWICE OVER, both times in the expensive direction. It printed
    `spendguard maxtokens --model <m>` — a flag that does not exist, since `maxtokens` takes a call-class
    SIG (an opaque hash of model+intent), so there is no path from a model name to the thing the command
    wants. And it promised `gate.autotune=apply raises the cap automatically`, which is false for nearly
    every row it was printed beside. Measured on this ledger, 8,574 truncations split:

        4,299  cap AT the model's published ceiling  autotune computes a target, sees target <= cap,
                                                     and returns. There is no higher cap to move to.
        3,179  NO cap set (max_tokens 0)             autotune never adds a cap where the caller set none.
          586  cap genuinely below the ceiling       autotune acts. ~7% of the total.

    So the headline remediation addressed about a fourteenth of what it was attached to, and the largest
    class is not a cap problem at all: gemini-3.8-flash truncated 4,299 times with its cap already at the
    published 65,536 and a VISIBLE answer of 0-9 tokens, which is reasoning consuming the whole output
    budget. Raising a cap that is already at the ceiling is not an available move; lowering reasoning
    effort for that intent is. A remediation that cannot run, attached to a diagnosis that does not hold,
    is worse than no hint — it reads as a closed loop and the leak keeps billing.

    Inputs: an open connection to the spend db (`calls` and `gate_calls` share one file) and a model name.
    Output: `(label, advice, command)` — `command` is built from a sig taken from this model's OWN rows, so
    what gets printed is runnable. Failure: returns a bare fallback rather than raising, because a receipt
    that crashes hides the whole tally to report one hint.
    """
    try:
        from ... import pricing
        ceiling = int(pricing.max_output_tokens(model) or 0)
    except Exception:
        ceiling = 0
    # THE TWO TABLES DO NOT SPELL A MODEL THE SAME WAY, and joining on equality silently lost the largest
    # class there is. `calls.model` holds a bare name (`gemini-3.8-flash`); `gate_calls.model` may hold it
    # PROVIDER-PREFIXED (`gemini:gemini-3.8-flash`), because the recorder is handed whatever the adapter
    # resolved. An `=` join therefore matched zero of gemini-3.8-flash's 4,191 truncated rows and reported
    # "no observed cap for this model" — a RAISABLE label on the one class that is already at its ceiling,
    # which is the opposite of the truth and the exact wrong-direction answer this whole function exists to
    # stop. Both sides are normalised to the bare name, and the prefixed spelling is matched explicitly.
    bare = str(model).rsplit(":", 1)[-1]
    try:
        caps = c.execute(
            "SELECT COALESCE(max_tokens, 0), COUNT(*), sig FROM gate_calls "
            "WHERE truncated = 1 AND (model = ? OR model = ? OR model LIKE '%:' || ?) "
            "GROUP BY 1, 3 ORDER BY 2 DESC", (model, bare, bare)).fetchall()
    except Exception:
        caps = []
    if not caps:
        # Truncations recorded in `calls` with no matching `gate_calls` row: the class was never observed
        # through the gate's response recorder, so nothing here knows its cap. Say so instead of guessing.
        return (RAISABLE, "no observed cap for this model — `spendguard maxtokens <sig>` once it has run "
                          "through the gate's response recorder", "")
    cap, _count, sig = caps[0]
    command = f"spendguard maxtokens {sig} {cap or ''} {model}".replace("  ", " ").strip()
    if ceiling and cap >= ceiling:
        return (AT_CEILING,
                f"cap {cap:,} IS the published max — not a cap problem. The budget went to reasoning, not "
                f"the answer; lower reasoning effort for this intent or pin a non-reasoning model",
                command)
    if not cap:
        return (NO_CAP,
                "no cap was set, so autotune will not add one (it only tunes a cap the caller already "
                "sent) — either the caller must set one, or the provider's own default is cutting it",
                command)
    return (RAISABLE,
            f"cap {cap:,} is below the {ceiling:,} published max — gate.autotune=apply raises this one"
            if ceiling else f"cap {cap:,} is below this model's ceiling — gate.autotune=apply raises this one",
            command)


def _truncation_rows(c):
    from spendguard.store.spend_search.ledger_read_contracts import LEDGER_CALLS_TABLE, LedgerReadRequest
    from spendguard.store.spend_search.ledger_read_accessors import resolve_read_ledger_count as read_ledger_count, resolve_read_receipt_truncation_groups as read_receipt_truncation_groups
    try:
        since = _windows()[2]
        rows = read_receipt_truncation_groups(since)
        allrows = (read_ledger_count(LedgerReadRequest(
            table=LEDGER_CALLS_TABLE, columns=("id",), where_sql="ts>=?", parameters=(since,))),)
    except Exception:
        return []
    rows = [r for r in rows if r[1]]
    if not rows:
        return []
    n = sum(r[1] for r in rows)
    usd = sum(r[2] for r in rows)
    # The share is over CALLS, not dollars, and deliberately so. calls.cost holds per-call figures that are
    # still estimates until reconcile trues them down, so dividing it into the reconciled total above would
    # mix two different quantities and print a confident percentage from the wrong denominator — measured:
    # the raw call-log sum was $10,548 against $65.91 of real API spend, which rendered the waste as "0.0%".
    # A count ratio needs no reconciliation to be exact.
    ncalls = (allrows[0] or 0) if allrows else 0
    share = f" — {n}/{ncalls} calls ({n / ncalls * 100:.1f}%)" if ncalls else ""
    out = [f"⚠ IN THE TOTAL, BUT WASTED: {n} call(s) ended at max_tokens/length{share}, "
           f"${usd:,.2f} on the call log (pre-reconcile). You were billed for the input and a body that "
           f"does not parse:"]
    for row in rows[:3]:
        m, k, c_ = row.values
        label, advice, command = truncation_remedy(c, m)
        out.append(f"    {str(m):<28} {k:>4} cut off   ${c_:>8.2f}   {label}")
        out.append(f"        {advice}")
        if command:
            out.append(f"        {command}")
    if len(rows) > 3:
        out.append(f"    … {len(rows) - 3} more model(s) — `spendguard maxtokens <sig>` for the measured "
                   f"bound; the sig comes from a TRUNCATED warning, never from a model name")
    return out


def _quarantine_lines():
    """Impossible estimates the gate caught, shown BELOW the total they are excluded from. Silence here would
    be its own dishonesty: the row exists, it was almost counted as money, and the reader should know both."""
    try:
        import spendguard.manage.admission.admission_accessors as budget
        rows = budget.resolve_quarantined_since(_windows()[2])
    except Exception:
        return []
    if not rows:
        return []
    tot = sum(r["cost"] for r in rows)
    n = sum(r["n"] for r in rows)
    out = [f"⚠ EXCLUDED from the total: {n} impossible estimate(s) worth {_money(tot)} "
           f"(input exceeded the model's context window — the estimator was wrong, not the batch)."]
    for r in rows[:3]:
        out.append(f"    {r['day']}  {r['model']:<20} {_money(r['cost']):>10}  {r['project'] or '(no project)'}")
    if len(rows) > 3:
        out.append(f"    … {len(rows) - 3} more")
    return out


def _asof_label(ev):
    """How to caption the est-value axis. A record with per-day detail is re-bucketed on read, so it is CURRENT
    and just says when it was last refreshed. A frozen pre-0.8.3 record is not this month's number at all — say
    STALE, say how old, and name the command that refreshes it. Never print a stale window as if it were today's."""
    a = (ev or {}).get("asof")
    if not a:
        return ""
    if not ev.get("stale"):
        return f" (as of {a})"
    try:
        age = (_utc_today() - _dt.date.fromisoformat(a)).days
        age_s = f", {age}d old" if age > 0 else ""
    except Exception:
        age_s = ""
    # A session-mined source names its own refresh command; a LEDGER-valued lane (gemini/zai — no miner) refreshes via
    # `spendguard lanevalue`. A source that is NEITHER (a typo, a retired source) gets NO invented command — a
    # plausible-but-wrong remedy the reader runs and sees unchanged is worse than none. dict.fromkeys de-dups in order.
    try:
        from ... import lane_value
        _valued = lane_value.ledger_valued_lanes()
    except Exception:
        _valued = set()

    def _refresh_cmd(s):
        if s in _SOURCE_REFRESH:
            return _SOURCE_REFRESH[s]
        return "spendguard lanevalue" if s in _valued else None
    cmds = list(dict.fromkeys(c for c in (_refresh_cmd(s) for s in (ev.get("stale_sources") or [])) if c))
    fix = f"; refresh: {' + '.join('`%s`' % c for c in cmds)}" if cmds else ""
    return f" (⚠ STALE — windows frozen {a}{age_s}{fix})"


def _est_tree(scope_org=None, repo=None):
    """Nested ORG → TEAM → PROJECT plan-value tree (month) from the cells, optionally one org.
    {org: {month, teams: {team: {month, projects: {project: month}}}}}."""
    tree = {}
    sl = None if scope_org is None else str(scope_org).strip().lower()
    for c in _est_cells():
        if repo is not None and c.get("team") != repo.strip().lower() and c.get("project") != repo.strip().lower():
            continue
        if sl is not None and c.get("org") != sl:
            continue
        m = c.get("month", 0)
        if m <= 0:
            continue
        o = tree.setdefault(c.get("org") or "", {"month": 0.0, "teams": {}})
        t = o["teams"].setdefault(c.get("team") or "", {"month": 0.0, "projects": {}})
        o["month"] += m
        t["month"] += m
        t["projects"][c.get("project") or ""] = t["projects"].get(c.get("project") or "", 0.0) + m
    return tree


# ── remote-compute (GPU) cache — the billed REMOTE component, stamped by resources.sync ───────────
def stamp_remote(rows) -> None:
    """Persist BILLED remote-compute (GPU/vast.ai) windows so the receipt can show the Remote component without
    re-hitting the provider. `rows` are day_totals-shaped (day + spend_micros + billed); only billed remote rows
    count. Best-effort; never raises. Mirrors stamp_est_value (which holds the NON-billed est-value axis)."""
    try:
        today, week, month = _windows()
        acc = {"today": 0.0, "week": 0.0, "month": 0.0, "asof": today}
        for r in rows or []:
            if r.get("billed") is False:               # remote compute is real billed $ (default billed=True)
                continue
            day = r.get("day") or ""
            usd = (r.get("spend_micros") or 0) / 1_000_000
            for k, lo in (("today", today), ("week", week), ("month", month)):
                if day >= lo:
                    acc[k] += usd
        p = _receipt_aggregate_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(p.read_text())
        except Exception:
            data = {}
        data["remote"] = acc
        config.update_json(p, lambda _d: data)
    except Exception:
        pass


def _remote_tally():
    """Billed remote-compute windows {today, week, month, asof}, or None if resources.sync never stamped it."""
    try:
        d = json.loads(_receipt_aggregate_cache_path().read_text()).get("remote")
        if isinstance(d, dict) and "month" in d:
            return d
    except Exception:
        pass
    return None


def _external_tally():
    """Billed EXTERNAL cost (MCP/tool + external paid APIs) windows {today, week, month} from the ledger's `external`
    axis — a GLOBAL real-$ component like subscription and remote (both already global in real_month). Unlike remote
    (stamped async by resources.sync into the cache), external cost is recorded straight to the ledger, so this reads
    it live. None on any error (never break the receipt over a bookkeeping read)."""
    try:
        import spendguard.manage.admission.admission_accessors as budget
        today, week, month = _windows()
        w = {}
        for k, since in (("today", today), ("week", week), ("month", month)):
            w[k] = round(sum(budget.resolve_by_day(kind="external", since=since).values()), 6)
        return w if any(w.values()) else None          # None when there's no external spend → receipt stays clean
    except Exception:
        return None


class ReceiptPlanAllocationRefused(ValueError):
    """A contextual real-dollar total has no trustworthy plan-usage allocation basis."""


# ── the running tally — REAL $ (API + Subscription + Remote) and est-value, NEVER summed ─────────
def tally(project=None, conv=None) -> dict:
    """The two axes, returned SEPARATELY and never added together:
      • REAL $ (money out the door) = `api` (per-token API billing, from the gate ledger) + `subscription` (the flat
        plan fee — Anthropic Max, OpenAI Pro, …) + `remote` (billed GPU/vast.ai, cached by resources.sync)
        + `external` (billed MCP/tool + external-API cost, from the ledger's external axis; None when there's none).
      • `est_value` = the value of subscription-covered usage (Claude Code/claude.ai/Codex), NOT billed.
    `real_month` = api.month + subscription + remote.month + external.month. `actual` is an alias of `api`.
    With `project`, global real-dollar components use its proportional plan-usage share (per window;
    subscription uses month). Missing/stale usage refuses allocation rather than inventing a share."""
    today, week, month = _windows()
    api = {"today": None, "week": None, "month": None}
    gaps = []
    try:
        import spendguard.manage.admission.admission_accessors as budget
        # THE SCOPE IS PASSED THROUGH. tally() took `project` and `conv` and used NEITHER, while
        # budget.resolve_spent_since has supported both all along — so `tally(project=repo)`, which is how the
        # per-repo receipt is built, returned the GLOBAL total under a per-repo heading. The number the
        # receipt exists to make honest was the one number in it that was not.
        api = {"today": budget.resolve_spent_since(today, project=project, conv=conv, read_only=True),
               "week": budget.resolve_spent_since(week, project=project, conv=conv, read_only=True),
               "month": budget.resolve_spent_since(month, project=project, conv=conv, read_only=True)}
    except Exception as error:
        gaps.append(f"CAPTURE GAP: ledger read unavailable: {error!r}")
    try:
        from spendguard.store.spend_search.ledger_read_accessors import resolve_read_incomplete_call_settlements as read_incomplete_call_settlements
        pending_settlements = read_incomplete_call_settlements(month, project=project)
        if pending_settlements["n"]:
            gaps.append(f"INCOMPLETE SETTLEMENT: {pending_settlements['n']} calls have unreported output usage; "
                        f"known input-cost floor {_money(pending_settlements['input_cost_floor'])}; "
                        "total cost remains unknown (excluded from settled totals)")
    except Exception as error:
        gaps.append(f"CAPTURE GAP: settlement completeness unavailable: {error!r}")
    remote = _remote_tally()                       # billed GPU/remote compute (None until resources.sync stamps it)
    external = _external_tally()                    # billed MCP/tool + external-API cost (None when there's none)
    sub, sub_assumed = _plan_usd()                 # flat monthly subscription fee — a REAL cost (out the door)
    est_value = _est_tally(repo=project) if project else _est_tally()
    if project:
        # These costs are installation-wide, unlike the directly attributed API ledger. Allocate each
        # window by the same window's measured plan usage; the flat monthly fee uses month usage.
        global_value = _est_tally()
        shares = {}
        for window in api:
            global_usage = (global_value or {}).get(window) or 0
            scoped_usage = (est_value or {}).get(window) or 0
            # An assumed fallback fee is not a known installation charge. Without usage it
            # contributes no scoped share, while configured fees and actual charges still refuse.
            allocatable = ((sub or 0) if window == "month" and not sub_assumed else 0) + sum(
                (component or {}).get(window) or 0 for component in (remote, external))
            if allocatable and (global_usage <= 0 or not est_value
                                or global_value.get("stale") or est_value.get("stale")):
                raise ReceiptPlanAllocationRefused(f"Cannot allocate global real-dollar costs to {project!r}: "
                                 f"missing or stale plan-usage basis for {window}; "
                                 f"global={global_value!r}, scoped={est_value!r}")
            shares[window] = scoped_usage / global_usage if global_usage else 0.0
        sub *= shares["month"]
        remote, external = (
            {**component, **{window: component.get(window) * shares[window]
                            if component.get(window) is not None else None for window in shares}}
            if component is not None else None for component in (remote, external))
    # est-value scoped to the SAME repo as the API-$ when this is a per-repo tally — else a repo line shows its own
    # billed spend beside the GLOBAL plan value (and _sum_repos over-counts it once per repo). Global tally = global.
    out = {"api": api, "actual": api, "remote": remote, "external": external, "subscription": sub,
           "subscription_assumed": sub_assumed,
           "est_value": est_value}
    try:
        import spendguard.manage.admission.admission_accessors as _budget
        pending = [str(path) for path in _budget.resolve_spend_event_retry_paths()
                   if path.exists() and path.stat().st_size]
        if pending:
            gaps.append("CAPTURE GAP: unposted spend queued for retry: " + ", ".join(pending))
    except Exception as error:
        gaps.append(f"CAPTURE GAP: retry-marker inspection unavailable: {error!r}")
    out["gaps"] = gaps
    try:
        out["input_cache"] = budget.resolve_input_cache_split(month, project=project, conv=conv)
    except Exception:
        out["input_cache"] = None
    out["real_month"] = ((api.get("month") or 0) + (sub or 0) + ((remote or {}).get("month") or 0)
                         + ((external or {}).get("month") or 0))
    ev = out["est_value"]
    if ev and (ev.get("month") or 0) > 0 and sub:
        out["plan_usd"] = sub
        out["plan_assumed"] = sub_assumed
        out["plan_mult"] = (ev.get("month") or 0) / sub      # est-value as a multiple of the subscription FEE (ROI)
    if project is None:                            # GLOBAL "which plan served spendguard's own work" — not per-repo,
        try:                                        # so it rides only the unscoped tally (the stop-hook line / summary)
            la = _lane_activity()
            if la:
                out["lanes"] = la
        except Exception:
            pass
    return out


# Default subscription mix — MIRRORS the server's ASSUMED (app/page.tsx): Anthropic Max (20×) + an OpenAI/ChatGPT
# Pro seat. These flat fees are REAL costs (out the door). Declare your real mix in config `subscription.plans`
# (a list of {name, usd}) or set `subscription.plan_usd` / SPENDGUARD_PLAN_USD to override.
_DEFAULT_PLANS = (("Anthropic Max", 200.0), ("OpenAI Pro", 200.0))


def _plan_usd():
    """Monthly subscription $ for the proportional plan slice → (usd, assumed). Order: explicit
    `subscription.plan_usd`/SPENDGUARD_PLAN_USD → sum of configured `subscription.plans` → DEFAULT (Anthropic Max +
    OpenAI Pro, $400; assumed=True so the UI can say so, like the server does — never silently wrong)."""
    v = os.getenv("SPENDGUARD_PLAN_USD") or config._cfg_get("subscription", "plan_usd", None)
    if v:
        try:
            return float(v), False
        except (TypeError, ValueError):
            pass
    plans = config._cfg_get("subscription", "plans", None)
    if isinstance(plans, list) and plans:
        try:
            return float(sum(float(p.get("usd", 0)) for p in plans)), False
        except Exception:
            pass
    return float(sum(u for _, u in _DEFAULT_PLANS)), True


def _plan_label():
    """Label for the subscription row — the RESOLVED plan names, never a hardcoded pair. `Subscription (Max + Pro)`
    was printed verbatim even for someone on a single $20 Claude Pro plan; the label must describe THEIR plans (the
    hard cost-display rule: every named component is real, and an assumed number says so)."""
    plans = config._cfg_get("subscription", "plans", None)
    if isinstance(plans, list) and plans:
        names = [str(p.get("name") or "plan").strip() for p in plans if isinstance(p, dict)]
        if names:
            return "Subscription (" + " + ".join(names[:3]) + (", …" if len(names) > 3 else "") + ")"
    if os.getenv("SPENDGUARD_PLAN_USD") or config._cfg_get("subscription", "plan_usd", None):
        return "Subscription (configured)"
    return "Subscription (" + " + ".join(n for n, _ in _DEFAULT_PLANS) + ")"


from . import spend_reporting_accessors as _spend_reporting_accessors

_spend_reporting_accessors.set__money_resolver(lambda *args, **kwargs: _money(*args, **kwargs))
_spend_reporting_accessors.set__plan_usd_resolver(lambda *args, **kwargs: _plan_usd(*args, **kwargs))
_spend_reporting_accessors.set__receipt_aggregate_cache_path_resolver(
    lambda *args, **kwargs: _receipt_aggregate_cache_path(*args, **kwargs))
_spend_reporting_accessors.set__rewindow_resolver(lambda *args, **kwargs: _rewindow(*args, **kwargs))
_spend_reporting_accessors.set__utc_today_resolver(lambda *args, **kwargs: _utc_today(*args, **kwargs))
_spend_reporting_accessors.set__windows_resolver(lambda *args, **kwargs: _windows(*args, **kwargs))
_spend_reporting_accessors.set_stamp_est_value_resolver(lambda *args, **kwargs: stamp_est_value(*args, **kwargs))
_spend_reporting_accessors.set_stamp_remote_resolver(lambda *args, **kwargs: stamp_remote(*args, **kwargs))
