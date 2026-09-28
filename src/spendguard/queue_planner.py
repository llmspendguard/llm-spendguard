"""C — the PERIODIC, PREDICTIVE queue planner: watch the live governor + queue, PREDICT which vendor is about to breach
its tokens-per-minute ceiling (a 429 BEFORE it happens), and RECOMMEND the action that avoids it — PACE (let the
admission bucket spread the load) or BATCH (move the eligible realtime backlog to the Batch API, which has a separate,
higher limit and ~half the cost). Ash 2026-09-27: "a 429 on a meter tells you to batch; you have the list of calls, you
can tell if a 429 is going to happen — this is basic logic."

`forecast()` is $0 and PURE ARITHMETIC on a PHYSICAL limit (tokens/minute). It is NOT a judgement about meaning — it is
capacity forecasting — so it uses no LLM (the agentic-decisions doctrine is about meaning; a rate limit is a number).
It reads the LIVE state spendguard already assembles (`dispatch.queue_state`: per-vendor tpm + in_flight + waiting) and
projects the current backlog's token load against each vendor's tpm, returning a per-vendor risk + recommendation.

The forecast is the INPUT to `tick()` (built next), which acts through machinery that ALREADY exists —
`route_economics.route_report` (is batch cheaper?), `whole_job` / `submit.submit_chat_tasks` (the Batch-API legs), and
the dispatch governor (pacing). This module forecasts and recommends; it does not reimplement execution.
"""
from . import dispatch

# A per-call token estimate for the forecast when the caller supplies none. The governor exposes the backlog as CALL
# COUNTS (in_flight + waiting); tpm is tokens/minute; so backlog_tokens = backlog_calls * avg_call_tokens. This mirrors
# the nominal shape the dispatch bucket itself debits (a rough input + a nominal output, see adapters._est_call_tokens).
# It is a FORECAST input, not a billing figure — the output labels it as an estimate. Overridable per call.
NOMINAL_CALL_TOKENS = 3000

# Risk is "minutes of backlog" = projected_backlog_tokens / tpm — how long draining the current backlog would take at
# the vendor's ceiling. >= 1.0 min => the vendor is saturated for over a minute, so new arrivals wait/timeout and the
# realtime path 429s => BATCH the eligible load off it. Between the pace floor and 1.0 => approaching => PACE (the
# bucket already spreads it; surface it so it's visible). These are boundaries of a REAL quantity, not magic thresholds.
BATCH_RISK_MINUTES = 1.0
PACE_RISK_MINUTES = 0.5

_OK, _PACE, _BATCH, _UNKNOWN = "ok", "pace", "batch", "unknown_tpm"


def forecast(avg_call_tokens=NOMINAL_CALL_TOKENS):
    """Per-vendor 429 forecast from the LIVE governor state — $0, pure arithmetic on tokens/minute.

    Returns {vendors: {key: row}, at_risk: [key,...], recommend_counts: {...}, note}. Each row:
      {tpm, in_flight, waiting, backlog_calls, avg_call_tokens, projected_backlog_tokens,
       minutes_of_backlog, headroom_frac, risk, recommend}.
    `recommend`: 'ok' | 'pace' (approaching the ceiling) | 'batch' (backlog would saturate >1 min → offload to Batch
    API) | 'unknown_tpm' (no learned/set tpm for this vendor — headroom UNMEASURABLE). 'unknown_tpm' is itself the
    signal that matters: an unknown limit is exactly where the FIRST burst 429s, so it is reported at_risk (never a
    confident 'ok' we cannot back) — the fix is for learn_rate_limit to populate the ceiling from a 429/success header.
    Never raises; a governor read failure returns an empty forecast with a note."""
    try:
        state = dispatch.queue_state()
    except Exception as e:
        return {"vendors": {}, "at_risk": [], "recommend_counts": {},
                "note": "queue_state unavailable (%s) — no forecast" % type(e).__name__}
    avg = max(1, int(avg_call_tokens or NOMINAL_CALL_TOKENS))
    vendors, at_risk, counts = {}, [], {}
    for key, b in (state or {}).items():
        tpm = int(b.get("tpm") or 0)
        backlog = int(b.get("in_flight") or 0) + int(b.get("waiting") or 0)
        projected = backlog * avg
        row = {"tpm": tpm, "in_flight": int(b.get("in_flight") or 0), "waiting": int(b.get("waiting") or 0),
               "backlog_calls": backlog, "avg_call_tokens": avg, "projected_backlog_tokens": projected}
        if tpm <= 0:
            row["minutes_of_backlog"] = None
            row["headroom_frac"] = None
            row["risk"] = None
            row["recommend"] = _UNKNOWN            # cannot forecast headroom → the first burst 429s here; learn the limit
        else:
            mins = projected / float(tpm)
            row["minutes_of_backlog"] = round(mins, 3)
            row["headroom_frac"] = round(max(0.0, 1.0 - mins), 3)
            row["risk"] = round(mins, 3)
            row["recommend"] = (_BATCH if mins >= BATCH_RISK_MINUTES
                                else _PACE if mins >= PACE_RISK_MINUTES else _OK)
        vendors[key] = row
        counts[row["recommend"]] = counts.get(row["recommend"], 0) + 1
        if row["recommend"] != _OK:
            at_risk.append(key)
    return {"vendors": vendors, "at_risk": at_risk, "recommend_counts": counts,
            "note": "live dispatch.queue_state; risk = minutes-of-backlog (backlog_calls*avg_call_tokens / tpm); $0"}


def forecast_summary(fc=None, avg_call_tokens=NOMINAL_CALL_TOKENS):
    """A human line-per-vendor view of forecast() for a receipt / CLI / doctor. Pure formatting; $0. (Uniquely named
    — not the generic `summary`, which collides across the repo per the name-uniqueness gate.)"""
    fc = fc if fc is not None else forecast(avg_call_tokens)
    lines = ["429 forecast (risk = minutes of backlog vs tpm):"]
    if not fc.get("vendors"):
        lines.append("  (nothing in flight — governor idle)")
        return "\n".join(lines)
    for key, r in sorted(fc["vendors"].items(), key=lambda kv: (kv[1]["risk"] is None, -(kv[1]["risk"] or 0))):
        risk = "unknown" if r["risk"] is None else ("%.2f min" % r["risk"])
        lines.append("  %-28s tpm=%-9s backlog=%-4d(%d in/%d wait)  risk=%-10s → %s"
                     % (key, (r["tpm"] or "?"), r["backlog_calls"], r["in_flight"], r["waiting"], risk,
                        r["recommend"].upper()))
    return "\n".join(lines)


# ── C2: thoughtful batch chunking ────────────────────────────────────────────────────────────────────────────
# PUBLISHED per-batch Batch-API caps, grounded 2026-09-27 from the official docs (config-overridable via
# batch.max_requests / batch.max_mb; the SSOT home for these is the model catalog). OpenAI: 50,000 requests / 200 MB
# input file. Anthropic: 100,000 requests OR 256 MB, whichever first (a 413 request_too_large past that). The per-model
# ENQUEUED-TOKEN limit is ACCOUNT/tier-specific (both providers say "see platform settings") — no fixed default; read
# from config batch.max_enqueued_tokens[provider] when the account has set one.
_BATCH_CAPS = {
    "openai":    {"max_requests": 50000, "max_mb": 200},
    "anthropic": {"max_requests": 100000, "max_mb": 256},
}
_BATCH_CAPS_FALLBACK = {"max_requests": 50000, "max_mb": 200}   # the smaller (conservative) caps for an unknown vendor
_BYTES_PER_TOKEN = 4          # tokens→bytes basis for the MB cap (~4 chars/token, chars≈bytes) — a bound, not billing
# The validated STAGE ceiling — Ash's never-large-batches doctrine: a chunk is never bigger than a PROVEN stage, so
# chunking BOUNDS blast radius, not just rate. Config batch.stage_cap overrides; the ladder is the staged-validation set.
_STAGE_LADDER = (100, 250, 500, 1000)
_STAGE_CAP_DEFAULT = 1000     # the ladder top — "not large" vs the 50k provider cap, and a bounded blast radius


def batch_caps(provider):
    """The resolved Batch-API caps for `provider` — {max_requests, max_mb, max_enqueued_tokens?}. Config
    (batch.max_requests / batch.max_mb / batch.max_enqueued_tokens[provider]) overrides the published defaults; an
    unknown vendor gets the conservative fallback. $0 — a config + constant read, never a hardcode at the call site."""
    from . import config
    base = dict(_BATCH_CAPS.get((provider or "").strip().lower(), _BATCH_CAPS_FALLBACK))
    mr = config._cfg_get("batch", "max_requests", None)
    mb = config._cfg_get("batch", "max_mb", None)
    if isinstance(mr, (int, float)) and mr > 0:
        base["max_requests"] = int(mr)
    if isinstance(mb, (int, float)) and mb > 0:
        base["max_mb"] = int(mb)
    met = config._cfg_get("batch", "max_enqueued_tokens", None)
    if isinstance(met, dict) and met.get(provider):
        base["max_enqueued_tokens"] = int(met[provider])
    return base


def plan_batch_chunks(n_tasks, provider, *, avg_call_tokens=NOMINAL_CALL_TOKENS, stage_cap=None):
    """Split n_tasks into thoughtfully-sized Batch-API chunks (C2). Every bound is a REAL number — the provider's max
    requests/batch, the requests that fit the MB cap at avg tokens, the account's enqueued-token limit (if set), and
    Ash's validated STAGE ceiling — so a chunk never trips a provider 413 AND never exceeds a proven size. Pure
    arithmetic (not a meaning judgement): $0, no LLM.

    Returns {chunks: [size,...], chunk_size, n_chunks, binding, caps} — `binding` NAMES which constraint set the size,
    so the plan is auditable. `chunk_size` = min(all caps); chunks tile n_tasks (last chunk = remainder)."""
    n = max(0, int(n_tasks))
    if n == 0:
        return {"chunks": [], "chunk_size": 0, "n_chunks": 0, "binding": "empty", "caps": {}}
    caps = batch_caps(provider)
    avg = max(1, int(avg_call_tokens or NOMINAL_CALL_TOKENS))
    stage = int(stage_cap) if (stage_cap and int(stage_cap) > 0) else _STAGE_CAP_DEFAULT
    # candidate ceilings on the chunk SIZE (in requests) — each a named real constraint
    cand = {"provider_max_requests": int(caps["max_requests"]),
            "mb_cap": max(1, int(caps["max_mb"] * 1_000_000 / (avg * _BYTES_PER_TOKEN))),
            "stage_cap": stage}
    if caps.get("max_enqueued_tokens"):
        cand["enqueued_token_cap"] = max(1, int(caps["max_enqueued_tokens"] // avg))
    binding = min(cand, key=cand.get)
    chunk_size = max(1, min(cand.values()))
    n_chunks = (n + chunk_size - 1) // chunk_size
    chunks = [chunk_size] * (n_chunks - 1) + [n - chunk_size * (n_chunks - 1)] if n_chunks else []
    return {"chunks": chunks, "chunk_size": chunk_size, "n_chunks": n_chunks, "binding": binding,
            "caps": {**cand, "resolved": caps}}


# ── C2 tick brain: the predictive OFFLOAD PLAN (decision only — $0, estimate-first, no execution) ─────────────
def _batch_eligible():
    """Is there a configured Batch-API model to offload to? The OpenAI Batch API serves an OpenAI id, so an offload
    runs on config advisor.batch_model (NOT the intent's realtime vendor) — fungible work only, never a pinned/
    consensus vote. Returns (eligible, batch_model). No batch_model set => not eligible => no auto-submit by omission."""
    from . import config
    bm = config._cfg_get("advisor", "batch_model", None)
    return (bool(bm), bm)


def offload_plan(pending_by_intent, *, avg_call_tokens=NOMINAL_CALL_TOKENS, fc=None):
    """The predictive OFFLOAD PLAN: given the live 429 forecast + the pending REALTIME depth per intent, decide which
    intents to move to the Batch API (to keep realtime under TPM) and HOW to chunk them. $0, estimate-first, NO
    execution — the executor (drain / whole_job / bulk_delegate on_miss='batch') acts on it. It COMBINES the pieces that
    already exist, never reimplementing them: forecast() (which vendors are saturated), route_economics.route_report
    (per intent: the converged VENDOR + whether batch is cheaper/eligible + the $ estimate), and plan_batch_chunks
    (right-sized chunks). The vendor↔intent map (the wrinkle) comes from route_report.resolved.lane — the intent's
    converged vendor — so a per-vendor forecast reaches an intent-keyed queue.

    Returns {offload: [...], skipped: [...], saturated_vendors, batch_eligible, note}. An intent is planned for offload
    when its converged vendor is saturated (forecast) OR route_report says batch is the economical path — AND a
    batch_model is configured (else eligible=False and nothing is planned, since there is nowhere to offload to). An
    intent route_report cannot price is a NAMED gap in `skipped` (it stays realtime this tick), never silently dropped;
    a DELIBERATE spend/deadline stop from pricing propagates."""
    fc = fc if fc is not None else forecast(avg_call_tokens)
    eligible, batch_model = _batch_eligible()
    saturated = set()
    for key, r in fc.get("vendors", {}).items():
        if r.get("recommend") in (_BATCH, _UNKNOWN):
            saturated.add(key.split(":", 1)[0])        # the vendor half of the governor key (vendor[:model])
    plan, skipped = [], []
    if eligible:
        from . import route_economics, adapters, gate as _gate
        bprov = adapters.provider_for(batch_model)     # the offload RUNS on batch_model (an OpenAI id) → chunk vs ITS caps
        for intent, n in (pending_by_intent or {}).items():
            n = int(n or 0)
            if n <= 0:
                continue                               # no pending work for this intent — nothing to offload (not a drop)
            try:
                rep = route_economics.route_report(intent, n) or {}
            except Exception as e:
                if _gate.is_deliberate_stop(e):
                    raise                              # a spend/deadline refusal PROPAGATES — never downgraded to a skip
                # NAMED gap (never a silent drop — each intent is a unit of work): it stays REALTIME this tick and the
                # miss is surfaced in `skipped`, so an intent is never invisibly removed from consideration.
                skipped.append({"intent": intent, "n": n, "reason": "route_report failed: %s" % (str(e)[:60])})
                continue
            rec, resolved = (rep.get("recommend") or {}), (rep.get("resolved") or {})
            vendor = ((resolved.get("lane") or "").split(":", 1)[0]) or None   # the intent's converged vendor
            batch_eco = rec.get("path") == "batch_only"
            sat = bool(vendor) and vendor in saturated
            if not (batch_eco or sat):
                continue                               # neither saturated nor cheaper as batch → correctly stays realtime
            reasons = []
            if sat:
                reasons.append("forecast: %s saturated (429 imminent)" % vendor)
            if batch_eco:
                reasons.append("route_report: batch is the cheaper path")
            ch = plan_batch_chunks(n, bprov, avg_call_tokens=avg_call_tokens)
            plan.append({"intent": intent, "n": n, "vendor": vendor, "provider": bprov,
                         "chunks": ch["chunks"], "chunk_binding": ch["binding"], "est_usd": rec.get("usd"),
                         "batch_model": batch_model, "reasons": reasons})
    return {"offload": plan, "skipped": skipped, "saturated_vendors": sorted(saturated), "batch_eligible": eligible,
            "note": ("predictive offload plan (forecast + route_report + chunker); $0, estimate-first, no execution"
                     if eligible else "no advisor.batch_model configured — nothing to offload to (set one to enable)")}


def tick(avg_call_tokens=NOMINAL_CALL_TOKENS, pending_by_intent=None):
    """One PLANNER TICK — the periodic, $0, NO-EXECUTION brain the drain loop (and the CLI/MCP) call: read the live
    429 forecast + the per-intent realtime backlog, and return the concrete PLAN — which vendors to PACE (approaching
    the ceiling), which intents to OFFLOAD to the Batch API and HOW to chunk them, and which are unpriceable this tick.
    It DECIDES; the executor submits (estimate-first, durably) — this never spends and never mutates the queue, so it
    is safe to call every cycle. `pending_by_intent` defaults to the LIVE lane_queue realtime backlog (pending_counts).

    Returns {forecast, offload, skipped, pace, saturated_vendors, batch_eligible, note}."""
    fc = forecast(avg_call_tokens)
    pending_note = ""
    if pending_by_intent is None:
        try:
            from . import lane_queue
            pending_by_intent = lane_queue.pending_counts()
        except Exception as _pe:
            # a FAILED backlog read is not an EMPTY backlog: {} would make offload_plan see nothing and silently
            # recommend NO offload. Surface it in the note so the tick is not read as "backlog empty, nothing to do".
            pending_by_intent = {}
            pending_note = " · WARNING: pending backlog UNREAD (%s) — offload plan understates" % type(_pe).__name__
    op = offload_plan(pending_by_intent, avg_call_tokens=avg_call_tokens, fc=fc)
    pace = [k for k, r in fc.get("vendors", {}).items() if r.get("recommend") == _PACE]
    return {"forecast": fc, "offload": op["offload"], "skipped": op["skipped"], "pace": pace,
            "saturated_vendors": op["saturated_vendors"], "batch_eligible": op["batch_eligible"],
            "note": "planner tick ($0, no execution) — forecast + offload plan + pace list; the executor acts on it"
                    + pending_note}


def should_offload(intent, n, *, fc=None):
    """Should the drain send THIS intent's `n` leased rows to the Batch API instead of realtime? Reuses offload_plan
    for the single intent and returns its offload entry ({intent, n, vendor, provider, chunks, batch_model, reasons})
    or None (stay realtime). $0, estimate-first, NO execution — the drain acts on it. A per-intent convenience over
    offload_plan so the drain does not rebuild the decision."""
    for o in offload_plan({intent: int(n or 0)}, fc=fc).get("offload", []):
        if o.get("intent") == intent:
            return o
    return None


# The continuous look-ahead cadence (Ash 2026-09-27: "a periodic say every second planning"). tick() is $0 pure
# arithmetic on the live governor state, so a ~1s cadence is cheap; the interval is config-overridable per deployment.
PLANNER_INTERVAL_S_DEFAULT = 1.0


def plan_loop(interval_s=None, iterations=None, on_tick=None, avg_call_tokens=NOMINAL_CALL_TOKENS, stop_event=None):
    """Run tick() on a PERIODIC cadence — the continuous ~1s look-ahead the drain's per-round consult cannot give (a
    drain round lasts as long as its batch, not ~1s, so 429 prediction lags the burst). Each cycle: tick() ($0, NO
    execution — it DECIDES, never spends or mutates the queue), hand the plan to `on_tick(t)` (the CLI prints it; a
    daemon could log/alert/feed a dashboard), then wait `interval_s`. Read-only, so it is SAFE to run continuously
    ALONGSIDE the drain — the drain stays the single executor; this is the eyes, not the hands.

    Bounded + stoppable + OBSERVABLE: `iterations` is the REQUESTED number of ticks (None = run continuously) and forms
    the loop's range; `stop_event` (a threading.Event) stops it PROMPTLY mid-interval. A deliberate stop from tick() OR
    from the on_tick observer PROPAGATES — never downgraded to 'keep polling' (refusal-containment). `interval_s`
    defaults to config queue.planner_interval_s. Returns {ticks, stopped_by} — how many ticks ran AND why it ended
    ('iterations' completed | 'stop_event' | 'interrupt' | 'error') — so the termination is never a silent stop."""
    import time as _t
    from . import gate as _g
    if interval_s is None:
        try:
            from . import config
            interval_s = config._cfg_get("queue", "planner_interval_s", PLANNER_INTERVAL_S_DEFAULT)
        except Exception:
            interval_s = PLANNER_INTERVAL_S_DEFAULT
    interval_s = max(0.1, float(interval_s or PLANNER_INTERVAL_S_DEFAULT))
    n, stopped_by, tick_errors, tick_failures = 0, "iterations", 0, 0
    try:
        while iterations is None or n < int(iterations):
            try:
                t = tick(avg_call_tokens=avg_call_tokens)
            except Exception as e:
                if _g.is_deliberate_stop(e):
                    raise                                  # a deliberate stop propagates — never swallowed in a poll loop
                # a FAILED tick is COUNTED + logged + surfaced in the return (tick_failures), so a caller can tell a run
                # where every tick raised from a clean run — it is NOT reported as a completed tick with no trace (F1).
                tick_failures += 1
                import sys as _sys
                print(f"[queue_planner] tick failed ({type(e).__name__}: {str(e)[:80]}) — {tick_failures} tick "
                      f"failure(s) so far; the loop continues.", file=_sys.stderr)
                t = {"forecast": {}, "offload": [], "skipped": [], "pace": [], "error": "%s" % type(e).__name__}
            if on_tick is not None:
                try:
                    on_tick(t)
                except Exception as e:
                    if _g.is_deliberate_stop(e):
                        raise                              # a deliberate stop from the OBSERVER halts — never downgraded
                    # a non-deliberate observer hiccup must not KILL the planner loop — but it is COUNTED, logged, and
                    # surfaced in the return (tick_observer_errors), never SILENTLY swallowed: the loop must not report
                    # a clean run when the sole observer (a dashboard write, a metric push) failed every tick.
                    tick_errors += 1
                    import sys as _sys
                    print(f"[queue_planner] on_tick observer failed ({type(e).__name__}: {str(e)[:80]}) — loop "
                          f"continues; {tick_errors} observer error(s) so far.", file=_sys.stderr)
            n += 1
            more = iterations is None or n < int(iterations)   # don't wait after the final requested tick
            if more and stop_event is not None:
                if stop_event.wait(interval_s):            # True the moment it is set → stop promptly
                    stopped_by = "stop_event"
                    break
            elif more:
                _t.sleep(interval_s)
    except KeyboardInterrupt:
        stopped_by = "interrupt"
    return {"ticks": n, "stopped_by": stopped_by, "tick_failures": tick_failures, "tick_observer_errors": tick_errors}
