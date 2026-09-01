"""Load-balance across subscription LANES by per-plan UTILISATION — the proactive brain of "use every flat-fee plan
well". This layer only SENSES: which plans are HOT (saturated, shed FROM) vs IDLE (spare capacity, absorb overflow).
The routing decision and the dispatch wiring build on top (separately), and the acceptable-substitute set is
model-proposed + confirmed once (Stage B).

HONESTY (stated, not papered over): utilisation here is spendguard's OWN est-VALUE ÷ the flat plan fee — what the
subscription-covered usage WOULD have cost at API rates against what you pay. It is NOT the provider's true
remaining quota (Anthropic Max weekly/5h limits are not API-exposed), so it is a capacity-UTILISATION signal, not a
quota gauge. The reactive lane error stays the hard exhaustion backstop; this is the pacing layer that fills idle
paid capacity. Ash's conversation-mining idea (limit-signals in Claude Code transcripts) will SHARPEN this later.

Numbers come from the receipt's OWN per-source est-value cache, re-windowed the same way — so they MATCH the receipt
rather than being a parallel computation that could disagree.
"""
from . import config, adapters


class BulkResilienceRefused(Exception):
    """A large bulk run was submitted as a single shot with NO crash-resilience (no checkpoint, or not actually
    chunked). Raised BEFORE any lane is touched — a fail-closed guard, distinct from a per-task error row, so a
    caller can catch it and add the missing checkpoint/chunking rather than lose the run to a transient stall."""


# Thresholds are CONFIG, never hardcoded: a plan whose est-value is below IDLE_RATIO of its fee has spare capacity;
# above HOT_RATIO of its fee it is saturated. Defaults are starting points, tunable per `advisor.lane_*_ratio`.
IDLE_RATIO_DEFAULT = 0.5
HOT_RATIO_DEFAULT = 1.5

# route_decision is PROMPT-FREE by design (registry + utilisation only), but ranking metered substitutes by cost
# needs a call size. These give a stable per-unit-price PROXY for that ranking (cheaper-per-token wins); the ACTUAL
# call is billed exactly by the provider, so the proxy only affects WHICH cheap substitute is picked, never the $
# recorded. Config: advisor.route_est_in / route_est_out.
ROUTE_EST_IN_DEFAULT = 2000
ROUTE_EST_OUT_DEFAULT = 500


def _util_ratio_cfg(name, default):
    try:
        return float(config._cfg_get("advisor", name, None) or default)
    except (TypeError, ValueError):
        return default


def _resilience_min_units(default=1000):
    """The unit count above which bulk_delegate refuses an un-resilient single shot (config bulk.resilience_min_units;
    env SPENDGUARD_BULK_RESILIENCE_MIN_UNITS). 0/None disables the gate. A named threshold, never a bare literal."""
    try:
        v = config._cfg_get("bulk", "resilience_min_units", None)
        import os as _os
        v = _os.environ.get("SPENDGUARD_BULK_RESILIENCE_MIN_UNITS", v)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _recent_calls_by_lane(hours=24):
    """Per-lane CALL COUNT in the last `hours` — the load signal the $-value utilisation MISSES. A cheap model
    (glm / gemini-flash) does thousands of calls for a few dollars of est-value, so est-value ÷ fee reads it IDLE
    while its plan's real quota (calls / tokens) is being spent. Call volume is the honest 'how hard is this plan
    being worked' proxy when the provider's true quota isn't API-exposed — it is what stops the router piling
    every overflow onto the cheapest-$ lane. Best-effort → {} on any error (the caller then falls back to the
    $-value order, never breaks)."""
    try:
        import sqlite3
        con = sqlite3.connect(config.db_path())
        rows = con.execute("SELECT executor, COUNT(*) FROM calls WHERE executor IS NOT NULL AND executor != '' "
                           "AND ts >= datetime('now', ?) GROUP BY executor", (f"-{int(hours)} hours",)).fetchall()
        con.close()
        return {ex: int(n) for ex, n in rows}
    except Exception:
        return {}


def _lane_fee(lane, n_lanes, total_fee):
    """(fee, exact) per-lane MONTHLY fee. An explicit `subscription.lane_plans` {lane: usd} map wins (exact); else the
    total plan fee split evenly across the lanes (approximate — flagged, never presented as exact). No dollar literal
    lives here — the number is always config-derived."""
    lp = config._cfg_get("subscription", "lane_plans", None) or {}
    if isinstance(lp, dict) and lp.get(lane) is not None:
        try:
            return float(lp[lane]), True
        except (TypeError, ValueError):
            pass
    return (float(total_fee) / max(1, int(n_lanes))), False


def lane_utilization():
    """Per-lane est-value THIS MONTH and its utilisation vs the plan fee, so the router — and the user — can see which
    subscription plans are HOT and which are IDLE.

    Returns {"lanes": [{lane, provider, est_value_month, plan_fee, utilization, fee_exact, state, fresh}], "total_fee",
    "fee_is_default", "asof"}, state ∈ {idle, warm, hot}. Reuses receipt._cache_path / _rewindow / _plan_usd so the
    figures equal the receipt's (and inherit its stale-cache guard: `fresh=False` means the number could be a frozen
    earlier-month value and must be refreshed, never shown as current)."""
    from . import receipt
    import json
    try:
        data = json.loads(receipt._cache_path().read_text()).get("est_value_by_source") or {}
    except Exception:
        data = {}                                         # no cache yet → every lane reads as idle (nothing recorded)
    total_fee, fee_default = receipt._plan_usd()
    idle_r, hot_r = _util_ratio_cfg("lane_idle_ratio", IDLE_RATIO_DEFAULT), _util_ratio_cfg("lane_hot_ratio", HOT_RATIO_DEFAULT)
    # LANE -> provider from the ONE source of truth (adapters._LANES); the est-value SOURCE string is the lane name.
    lane_prov = {lane: prov for prov, (lane, _mod) in adapters._LANES.items()}
    lanes = sorted(lane_prov)
    recent = _recent_calls_by_lane(24)                    # volume signal the $-gauge misses (cheap lanes read idle)
    out = []
    for lane in lanes:
        rec = data.get(lane) or {}
        wins, fresh = receipt._rewindow(rec) if rec else ({"month": 0.0}, True)
        ev = float(wins.get("month") or 0.0)
        fee, fee_exact = _lane_fee(lane, len(lanes), total_fee)
        util = (ev / fee) if fee else None
        state = ("hot" if util is not None and util >= hot_r else
                 "idle" if util is not None and util < idle_r else "warm")
        out.append({"lane": lane, "provider": lane_prov[lane], "est_value_month": round(ev, 2),
                    "plan_fee": round(fee, 2), "utilization": (round(util, 3) if util is not None else None),
                    "calls_recent": int(recent.get(lane, 0)),   # 24h call VOLUME — the load-balance ordering key
                    "fee_exact": fee_exact, "state": state, "fresh": fresh})
    return {"lanes": out, "total_fee": round(float(total_fee), 2), "fee_is_default": fee_default,
            "asof": receipt._windows()[0]}


def hot_lanes():
    """Lanes that are saturated (shed work FROM these)."""
    return [l["lane"] for l in lane_utilization()["lanes"] if l["state"] == "hot"]


def idle_lanes():
    """Lanes with spare capacity (route overflow TO these), BEST-first — the order the router prefers. Ordered by
    real quota UTILITY where the provider exposes headroom (route_utility.rank_lanes over the persisted
    lanes.lane_headroom snapshot — more remaining × sooner-reset urgency, so a plan whose window resets soon with
    room is drained first, use-it-or-lose-it), and by recent CALL VOLUME for lanes whose quota is UNKNOWN (a cheap
    lane doing thousands of calls reads idle on the $ gauge but is spending its plan; volume spreads those). Cooling
    lanes are excluded. NO CLI call in this hot path — the snapshot is read from disk (refreshed out-of-band); with
    no snapshot yet, every lane is 'unknown' and this degrades EXACTLY to the old volume-only order."""
    from . import lanes as _lanes, route_utility
    idle = [l for l in lane_utilization()["lanes"] if l["state"] == "idle"]
    # unknown-headroom lanes keep the volume proxy: pre-sort by it so rank_lanes preserves that order for the Nones
    idle = sorted(idle, key=lambda l: (l.get("calls_recent", 0), l["utilization"] if l["utilization"] is not None else 0.0))
    snap = {r["lane"]: r for r in _lanes.lane_headroom(do_fetch=False)}       # persisted; no CLI in the routing hot path
    rows = [snap.get(l["lane"], {"lane": l["lane"], "provider": l["provider"], "known": False,
                                 "remaining_pct": None, "reset_ts": None}) for l in idle]
    from . import lane_economics
    # A prompt-metered lane whose SELF-USE cap is reached is held back from discretionary overflow — its stops-dead
    # prompt budget is reserved for real coding (spendguard only spends what it can see it spending).
    return [d["lane"] for d in route_utility.rank_lanes(rows)
            if d["available"] and not lane_economics.prompt_lane_reserved(d["lane"])]


# ── the CONFIRMED-substitute registry (Part 2 authorization = "model proposes, you confirm once") ──────────────────
# A JSON store keyed by INTENT → {confirmed:[provider:model], pending:[…], primary_model, proposed_by}. Only CONFIRMED
# substitutes are ever used to route; PENDING are model-proposals awaiting Ash's one-time confirm. Kept out of the
# spend DB (small, human-facing config) and written through the one JSON writer so a concurrent write can't shear it.
import json as _json


def _registry_path():
    return config.HOME / "lane_substitutes.json"


def _registry():
    try:
        return _json.loads(_registry_path().read_text())
    except Exception:
        return {}                                          # absent/corrupt → empty → substitution simply OFF (safe)


def substitutes_for(intent):
    """CONFIRMED acceptable substitute 'provider:model' specs for this intent, in preference order (or [])."""
    return list((_registry().get(intent) or {}).get("confirmed") or [])


def pending_for(intent):
    """Model-PROPOSED substitutes awaiting confirmation (not yet usable by the router)."""
    return list((_registry().get(intent) or {}).get("pending") or [])


def record_proposal(intent, primary_model, proposals, proposed_by=""):
    """Record model-proposed substitutes as PENDING — NOT usable until confirmed. De-dupes; never promotes to
    confirmed on its own (that is the human 'confirm once' step)."""
    def _add_pending(d):
        e = d.setdefault(intent, {})
        e["primary_model"] = primary_model
        e["pending"] = list(dict.fromkeys([*(e.get("pending") or []), *proposals]))
        e["proposed_by"] = proposed_by or e.get("proposed_by", "")
    config.update_json(_registry_path(), _add_pending, reason="lane-substitute-proposal")
    return pending_for(intent)


def confirm_substitute(intent, substitute):
    """The 'confirm once' step: promote one proposed substitute to CONFIRMED so the router may use it. Idempotent."""
    def _promote_confirmed(d):
        e = d.setdefault(intent, {})
        e["confirmed"] = list(dict.fromkeys([*(e.get("confirmed") or []), substitute]))
        e["pending"] = [p for p in (e.get("pending") or []) if p != substitute]
    config.update_json(_registry_path(), _promote_confirmed, reason="lane-substitute-confirm")
    return substitutes_for(intent)


_DELEGATE_OUT = 1500             # OUTPUT budget for a delegated task — NAMED, not a bare literal (token_caps guard)


def delegate(task, system=None, lanes=None, reasoning="low", max_tokens=_DELEGATE_OUT, intent=None,
             enqueue=False, priority=None):
    """Offload one task to the cheapest VIABLE idle subscription lane and return the answer — so heavy work runs $0
    on an idle plan while the orchestrator (e.g. this Claude Code session) spends nothing but coordination.

    enqueue=True instead DURABLY QUEUES the task (at `priority`, default INTERACTIVE so it drains ahead of bulk
    backfill) for the drainer to run later on an idle lane, and returns {queued: id, ...} without running it — the
    right call under high utilization or for fire-and-forget work (the drainer, `spendguard lanes --drain`, empties
    it onto idle plans at $0).

    Picks from the viable delegation lanes (config `advisor.delegate_lanes`, default the ones MEASURED fast + $0:
    gemini, zai — codex is EXCLUDED, its CLI is an agent, >75s on a real prompt → metered fallback), LEAST-UTILISED
    first, running each lane's model from `advisor.lane_models` at LOW reasoning (gemini-HIGH returns EMPTY — hidden
    reasoning eats the budget, so lane_models should point gemini at a `-low` variant). EMPTY or errored output is a
    FAILURE → fall through to the next lane; billed fallback is NOT silent (a lane that answered via the metered API
    is flagged `billed=True`). Returns {text, lane, model, cost, billed, executor, tried} or {text:None, error, tried}.
    The model that answered is the one recorded — attribution stays honest."""
    from . import adapters, calls
    intent = intent or "spendguard:delegate"
    if enqueue:                                   # durable fire-and-forget: park it for the drainer, don't run now
        from . import lane_queue
        pri = lane_queue.PRIORITY_INTERACTIVE if priority is None else int(priority)
        qid = lane_queue.enqueue(intent, task, system=system, reasoning=reasoning, priority=pri)
        return {"queued": qid, "intent": intent, "priority": pri, "text": None, "lane": None,
                "error": None if qid else "enqueue failed"}
    # LEARNED routing: with the bandit enabled (advisor.lane_bandit) it picks the lane by what it has LEARNED wins
    # for THIS intent — equal-start → bake-off-judge → exploit — instead of the static cheapest-idle heuristic below,
    # and echoes which lane served. Falls through to the heuristic if it had no live arm / none answered.
    if str(config._cfg_get("advisor", "lane_bandit", False)).strip().lower() in ("1", "true", "yes", "on"):
        try:
            from . import lane_bandit
            r = lane_bandit.bandit_call(intent, task, system=system, reasoning=reasoning)
            if r and r.get("text"):
                import sys as _sb
                print(f"[spendguard] 🎰 bandit → {r['lane']} · {r.get('use_name')} ({r.get('why')}) — $0 on-plan",
                      file=_sb.stderr)
                return {"text": r["text"], "lane": r["lane"], "model": r.get("use_name"), "cost": 0.0,
                        "billed": False, "executor": r["lane"], "tried": [r["lane"]], "bandit": True}
        except Exception:
            pass
    viable = list(lanes or config._cfg_get("advisor", "delegate_lanes", None) or ["gemini", "zai-coding"])
    lm = config._cfg_get("advisor", "lane_models", None) or {}
    util = {l["lane"]: (l["utilization"] if l.get("utilization") is not None else 0.0)
            for l in lane_utilization()["lanes"]}
    prov_of = {ln: prov for prov, (ln, _m) in adapters._LANES.items()}
    order = sorted([l for l in viable if isinstance(lm, dict) and lm.get(l) and prov_of.get(l)],
                   key=lambda l: util.get(l, 0.0))
    tried = []
    for lane in order:
        model = f"{prov_of[lane]}:{lm[lane]}"
        tried.append(model)
        with calls.context(intent=intent):
            r = adapters.call(model, task, system=system, reasoning=reasoning, max_tokens=max_tokens, sig=intent)
            # sig=intent → autotune raises this class's OUTPUT budget from its measured p99 (over the 1500 floor),
            # so a long delegated answer stops truncating at 1500/3000/6000 as the class is learned.
        txt = (r.get("text") or "").strip()
        if txt and not r.get("error"):
            return {"text": txt, "lane": lane, "model": model, "cost": r.get("cost"),
                    "billed": bool(r.get("cost")), "executor": r.get("executor"), "tried": tried}
    return {"text": None, "lane": None, "model": None, "tried": tried,
            "error": f"no viable delegation lane answered (tried {tried or 'none — set advisor.lane_models {lane: model}'})"}


def _bulk_arms(intent, lanes=None):
    """One arm per VIABLE lane for a BULK job — each lane's BEST-winrate use-name for this intent (so bulk rides each
    lane's best reasoning variant). A lane is dropped ONLY if its best arm was TRIED and WON NOTHING (winrate 0 — a
    proven total loser for this intent); that is a natural boundary, not a tuned threshold. Untried lanes are kept
    optimistically (explore), cooling lanes skipped. Spreading bulk across every remaining good lane is what makes it
    fast — they run in PARALLEL — so this returns the whole good set, not a single winner."""
    from . import lane_bandit, lane_catalog
    st = lane_bandit.arm_stats(intent)
    best = {}
    for arm in lane_catalog.arms(lanes or config._cfg_get("advisor", "delegate_lanes", None)):
        if lane_bandit._arm_cooling(*arm):
            continue
        s = st.get(arm) or {}
        wr = s.get("winrate")
        wr = 1.0 if wr is None else wr                # untried → optimistic, so a new lane still earns bulk work
        if s.get("trials") and wr <= 0.0:            # TRIED and won NOTHING for this intent → a proven loser, drop it
            continue
        cur = best.get(arm[0])
        if cur is None or wr > cur[0]:
            best[arm[0]] = (wr, arm)
    return [a for _wr, a in best.values()]


def _row_succeeded(row):
    """The success CONTRACT for a bulk/checkpoint row: it carries TEXT and no ERROR. Structural, not a quality
    judgement — the lane path already coerces an empty/whitespace answer to an explicit ERROR upstream
    (adapters: 'lane returned no usable text'), so within this pipeline text is present iff the task actually
    produced an answer; there is no 'valid empty result' row to misjudge. Mirrors lane_queue.settle's ok-test
    so the checkpoint-resume and the durable queue agree on what 'done' means (one contract, defined once)."""
    return bool(row) and bool(row.get("text")) and not row.get("error")


def bulk_delegate(tasks, intent, system=None, reasoning=None, max_workers=None, deadline_s=120.0,
                  checkpoint=None, chunk_size=100, refuse_billed=False, stats=None, force=False, tier=None):
    """Fan a LIST of similar tasks across ALL viable idle lanes CONCURRENTLY — the right shape for a BULK job (e.g.
    symgrep's ~6k one-sentence symbol descriptions) that the per-call bandit would trickle one at a time. Each task
    runs on a lane (round-robin across the lanes the bandit rates GOOD for this intent), each admission BOUNDED by
    the dispatch GOVERNOR (per-lane in-flight cap, so it FILLS the plans without tripping their throttle). So it is
    FAST (parallel across lanes), $0 (plan-served; a lane failure falls back to that provider's API, flagged
    `billed` — unless `refuse_billed`, which makes a lane miss an error row and NEVER bills, $0 by construction), and
    spread across EVERY good lane, not one.

    DURABLE (the CHUNK-never-single-shot rule): tasks run in chunks of `chunk_size`; when `checkpoint` (a jsonl path)
    is given, EACH completed result is appended before the next chunk, so a crash RESUMES instead of losing the run.
    Resume is keyed by CONTENT (sha256 of system+task+intent), never position — a re-run whose task list changed
    (different order/composition) maps each saved result back by MEANING, so it can never land on the wrong task; an
    older POSITIONAL checkpoint is detected and ignored with a notice, never misread. One task that errors returns an
    error row and never wedges the chunk. Returns [{text, lane, use_name, model, billed, error}] in task order."""
    import os as _os
    import json as _json
    import threading as _th
    import concurrent.futures as _cf
    from . import adapters, calls, dispatch, lane_catalog

    tasks = list(tasks)
    if not tasks:
        return []
    # RESILIENCE GATE (the durable half of chunk-never-single-shot). A large run submitted as ONE shot with no
    # checkpoint (a crash or a transient no-progress pass loses everything) or not actually chunked (chunk_size >=
    # the unit count, so one bad unit / a momentary full-lane pass can wedge the whole batch) is REFUSED before any
    # lane is touched — the mistake that killed a 54k-unit single-shot on pass 1 is made un-submittable, not merely
    # discouraged. force=True (or bulk.resilience_min_units=0) overrides. The queue drainer is unaffected: it feeds
    # small leased batches, well under the threshold.
    _min = _resilience_min_units()
    if not force and _min and len(tasks) > _min:
        _gaps = []
        if not checkpoint:
            _gaps.append("no checkpoint — a crash or transient stall loses the whole run (pass checkpoint=<jsonl>)")
        if chunk_size >= len(tasks):
            _gaps.append(f"not chunked — chunk_size={chunk_size} >= {len(tasks)} units, so one bad unit or a "
                         f"momentary full-lane pass can wedge everything (lower chunk_size)")
        if _gaps:
            raise BulkResilienceRefused(
                f"REFUSED: {len(tasks)} units (> bulk.resilience_min_units={_min}) as a single shot without "
                f"resilience — " + "; ".join(_gaps) + ". This is the chunk-never-single-shot rule: a large job "
                "must checkpoint and chunk so a transient no-progress pass cannot kill it. Fix the above, or pass "
                "force=True to override and own the risk.")
    # TIER CONFINEMENT (a cost RAIL, declared once). When `tier` is given, each lane contributes its OWN model FOR
    # this capability GROUP — lane_catalog.lane_model_for_tier(lane, tier): its cheap model for `cheap`, its strong
    # model for `strong`. So a bulk fan spreads across every plan's RIGHT-SIZED model (e.g. cheap describe → codex's
    # gpt-5.6-luna + claude-code's haiku + zai's glm), never a premium model for cheap work. FAIL-CLOSED: no lane
    # serves the group (undeclared, its models on no lane), or all serving it are cooling → EVERY task errors
    # (undescribed, re-runnable); we never widen off-tier and never fall onto a strong/Opus lane.
    if tier:
        arms = [(ln, m) for ln in lane_catalog.lanes()
                if (m := lane_catalog.lane_model_for_tier(ln, tier)) and not adapters._lane_cooling(ln)]
        if not arms:
            return [{"text": None, "lane": None, "use_name": None, "billed": False,
                     "error": f"--tier {tier!r}: no lane serving this group is available (undeclared, its models are "
                              f"on no lane, or every such lane is cooling) — refusing rather than widening off-tier; "
                              f"declare a {tier}-tier model on a lane (advisor.tiers / advisor.lane_models) and re-run"}
                    for _ in tasks]
    else:
        arms = _bulk_arms(intent)
        if not arms:
            return [{"text": None, "lane": None, "use_name": None, "billed": False,
                     "error": "no viable lane (set advisor.lane_models; check `spendguard lanes`)"} for _ in tasks]

    import hashlib as _hl

    def _content_key(task):
        # CONTENT identity, never position: sha256(system + task + intent). A re-run whose task LIST changed — symgrep
        # re-describes only CHANGED functions, so order + composition shift between runs — resumes by MEANING, so a
        # saved result can never be mapped onto a different task (a wrong description written into an index is the
        # worst failure class). Two identical tasks share a key (identical result) — correct, not a collision.
        h = _hl.sha256()
        h.update((str(system) + "\x00" + str(task) + "\x00" + str(intent)).encode("utf-8", "replace"))
        return h.hexdigest()[:24]

    _keys = [_content_key(t) for t in tasks]
    results = [None] * len(tasks)
    if checkpoint and _os.path.exists(checkpoint):   # RESUME by CONTENT KEY (never by position)
        done, _stale = {}, 0
        try:
            with open(checkpoint) as f:
                for ln in f:
                    try:
                        rec = _json.loads(ln)
                    except Exception:
                        continue
                    if "k" in rec:
                        done[rec["k"]] = rec["r"]
                    elif "i" in rec:
                        _stale += 1                          # a pre-content-key (positional) line — must NOT be misread
        except Exception:
            pass
        if _stale:
            import sys as _sy
            _sy.stderr.write(f"[spendguard] bulk: ignoring {_stale} POSITIONAL checkpoint line(s) from an older format "
                             f"— resuming by content key only (a positional resume could mismap results onto wrong tasks).\n")
        for i, k in enumerate(_keys):
            d = done.get(k)
            # only a SUCCESS resumes; an ERROR row in the checkpoint must be RETRIED, not counted as finished. A
            # failed task silently read as done is the worst outcome for this corpus (an undescribed symbol written
            # into an index as if described) — so a task with an error checkpoint line is left in `todo`.
            if _row_succeeded(d):
                results[i] = d
    todo = [i for i in range(len(tasks)) if results[i] is None]
    if isinstance(stats, dict):                       # so the caller can print "resumed N · dispatched M", not a spread
        stats["tasks"] = len(tasks)
        stats["resumed"] = len(tasks) - len(todo)     # successes carried over from the checkpoint
        stats["dispatched"] = len(todo)               # run THIS invocation (includes retried error rows)
    if not todo:
        return results                                # fully resumed from the checkpoint — nothing left to run

    _cklock = _th.Lock()

    def _checkpoint(i, res):
        if not checkpoint:
            return
        try:
            with _cklock, open(checkpoint, "a") as f:        # one durable line per finished task, keyed by CONTENT
                f.write(_json.dumps({"k": _keys[i], "r": res}) + "\n")
        except Exception:
            pass                                             # a checkpoint-write failure must not lose the in-hand result

    n = int(max_workers or dispatch._limit("global_concurrency", 24))

    def _run_task_on_lane(i, task):
        lane, use_name = arms[i % len(arms)]          # round-robin — spread across the good lanes for parallelism
        # A lane can start COOLING mid-run (an earlier task hit its quota → persisted reset window). `arms` was
        # fixed at the start, so round-robin would keep landing work here; rotate to the next arm that is not
        # cooling so a demoted lane stops receiving work within this run too. If every arm is cooling, keep the pick.
        if adapters._lane_cooling(lane) and len(arms) > 1:
            for j in range(1, len(arms)):
                alt_lane, alt_use = arms[(i + j) % len(arms)]
                if not adapters._lane_cooling(alt_lane):
                    lane, use_name = alt_lane, alt_use
                    break
        prov = lane_catalog.lane_provider(lane)
        model = f"{prov}:{use_name}"
        try:
            dispatch.acquire(prov, use_name, deadline_s)     # governor: bounds per-lane in-flight (fills, never swarms)
        except Exception as e:
            return i, {"text": None, "lane": lane, "use_name": use_name, "model": model, "billed": False,
                       "error": f"dispatch: {str(e)[:60]}"}
        try:
            calls.set_context(intent=intent)          # tag this worker thread's calls with the intent (attribution)
            r = adapters.call(model, task, system=system, reasoning=reasoning,   # sig=intent → the OUTPUT budget is this
                              sig=intent, timeout_s=deadline_s,                  # call-class's measured p99; refuse_billed
                              no_metered_fallback=refuse_billed,                 # → a lane miss errors, never a paid retry
                              no_substitution=bool(tier))                        # TIER: pin the cheap model — the bandit
            #                                                                      can NEVER swap it for a strong/Opus one;
            #                                                                      the only fallback is THIS model's metered
            #                                                                      API, which is in-tier by construction
            # (receipt suppressed via set_context above, not the context manager)  each reply feeds that measurement
        except Exception as e:
            return i, {"text": None, "lane": lane, "use_name": use_name, "model": model, "billed": False, "error": str(e)[:80]}
        finally:
            dispatch.release(prov, use_name)
        r = r if isinstance(r, dict) else {}
        # lane / use_name / model must all describe the SAME (actual) dispatch record. The result r carries the
        # provider + model + executor that ACTUALLY served — substitution and API fallback route through call(),
        # so r's base is the SUBSTITUTE's. Taking `lane` from r.executor while keeping `model` from the INTENDED
        # arm is what crossed the rows (lane:"gemini" with model:"openai:gpt-5.5"). Derive all three from r; keep
        # the intended arm only as PROVENANCE when a substitution/fallback moved the work.
        served_model = r.get("model") or use_name            # base sets model=raw (bare id); provider prefixes below
        served_prov = r.get("provider") or prov
        served_lane = r.get("executor") or ("api-fallback" if r.get("cost") else lane)
        if served_lane == "api":                             # the metered API served it — in a bulk fan-out that IS a
            served_lane = "api-fallback"                     # fallback from the intended lane; keep the descriptive label
        row = {"text": (r.get("text") or None), "lane": served_lane, "use_name": served_model,
               "model": f"{served_prov}:{served_model}", "billed": bool(r.get("cost")), "error": r.get("error")}
        if r.get("substituted_from") and f"{served_prov}:{served_model}" != f"{prov}:{use_name}":
            row["intended"] = f"{prov}:{use_name}"           # what the round-robin picked, before the substitution
            row["substituted_from"] = r["substituted_from"]
        return i, row

    # CHUNKED: bound how many futures are in flight at once, and make each chunk's results durable before the next.
    for c0 in range(0, len(todo), max(1, int(chunk_size))):
        chunk = todo[c0:c0 + max(1, int(chunk_size))]
        with _cf.ThreadPoolExecutor(max_workers=max(1, n)) as ex:
            for fut in _cf.as_completed([ex.submit(_run_task_on_lane, i, tasks[i]) for i in chunk]):
                i, res = fut.result()
                results[i] = res
                _checkpoint(i, res)
    return results


def _metered_substitute(subs, primary_spec):
    """Cheapest AFFORDABLE confirmed metered substitute for an intent, or None. A 'provider:model' spec counts as
    METERED here when its provider has NO subscription lane (adapters._LANES) — so it can only be served by the paid
    API — and is a provider we can actually call (adapters.PROVIDERS). Ranked by route_utility.rank_metered
    (cheapest-per-token that still has prepay first; an exhausted sunk-pool balance surfaces available=False and is
    skipped, never picked). route_decision is prompt-free, so the ranking uses a config NOMINAL call size
    (ROUTE_EST_IN/OUT) — a stable unit-price PROXY for WHICH cheap substitute to pick; the real call is billed
    exactly by the provider. Free lanes are always preferred UPSTREAM of this; it is the reactive last hop before
    paying full price on the ORIGINAL model, and it never leaves the user-CONFIRMED substitute set."""
    metered = [s for s in subs
               if s != primary_spec
               and not adapters._LANES.get(s.split(":", 1)[0], (None,))[0]      # no subscription lane → paid API only
               and s.split(":", 1)[0] in adapters.PROVIDERS]                    # …and a provider we can actually call
    if not metered:
        return None
    from . import route_utility
    nin = int(config._cfg_get("advisor", "route_est_in", None) or ROUTE_EST_IN_DEFAULT)
    nout = int(config._cfg_get("advisor", "route_est_out", None) or ROUTE_EST_OUT_DEFAULT)
    ranked = route_utility.rank_metered(metered, nin, nout)                     # cheapest-affordable first; rest surfaced
    return next((r for r in ranked if r["available"]), None)


def _intent_listed(intent, entries):
    """Is `intent` covered by a bandit allow/deny list? Exact name, or a PREFIX entry ending in ':' or '*'.

    WHY A PREFIX IS REQUIRED, not a nicety. Both lists were exact-match (`intent in entries`), which silently
    cannot express a FAMILY of intents whose names are generated per work item. honestreview's cross-vendor
    consensus panel labels every call `review:<filename>`, so denying it would have meant enumerating every file in
    every repo forever — i.e. the sanctioned "DENY an intent that genuinely needs the primary model" channel did not
    exist for the one caller that most needs it.

    MEASURED 2026-08-29, warden S1 wave 1: with `bandit_mode=optout` and an empty denylist, the bandit substituted
    `openai:gpt-5.5` for gemini (9x), zai (7x), moonshot (7x) and anthropic (7x). The panel's report still printed
    `anth=ok,moon=ok,gemi=ok` per file, so a FIVE-VENDOR consensus was really one model agreeing with itself while
    every count in the output claimed otherwise. Substituting the model is exactly right for work that needs AN
    answer and exactly wrong for work where WHICH MODEL ANSWERED is the measurement.

    Matching a trailing ':'/'*' is parsing a known shape, not deciding meaning."""
    for e in (entries or []):
        e = str(e)
        if e.endswith("*") and str(intent).startswith(e[:-1]):
            return True
        if e.endswith(":") and str(intent).startswith(e):
            return True
        if str(intent) == e:
            return True
    return False


def route_decision(intent, model, reactive=False):
    """(substitute_spec or None, why) — the routing brain, PURE (registry + utilisation only, no LLM; the agentic
    proposer fills the registry separately). Default OFF: an intent with no CONFIRMED substitute yields (None, …), so
    every existing call is unchanged.

    EFFECTIVE UTILISATION, not merely failover: the goal is to keep ALL the paid plans usefully used, so PROACTIVELY
    route an intent's work to the LEAST-utilised acceptable substitute whenever that plan sits more than
    `advisor.lane_balance_margin` BELOW the primary's utilisation (fill idle capacity; the margin stops thrashing
    once plans are balanced). REACTIVE (reactive=True): the primary lane just FAILED — take the least-utilised
    available substitute regardless of the margin, before the metered API. NEVER routes onto a cooling lane."""
    if not intent:
        return None, "no intent set — nothing to key substitutes on"
    subs = substitutes_for(intent)
    if not subs:
        # BANDIT (advisor.lane_bandit, default OFF): with no CONFIRMED substitute, let the LEARNED router pick a
        # cross-provider arm to shed to — equal-start across the delegate lanes, then the learned winner. This is how
        # hot claude-code work moves onto the idle lanes on the MAIN path. GATED to an explicit ALLOWLIST
        # (advisor.bandit_intents) so it only ever redirects intents the user has marked SAFE to run on another
        # model — never work that needs a specific model (e.g. a gpt-5-mini batch). META intents stay caged; never
        # the primary's own lane; never a cooling arm. Quality is LEARNED from bake-offs (`delegate` / `--bakeoff`);
        # this side just EXPLOITS what's known + explores untried. Empty allowlist ⇒ main-path routing stays inert.
        try:
            from .advisor import META as _META
        except Exception:
            _META = "spendguard"
        _bandit_on = str(config._cfg_get("advisor", "lane_bandit", False)).strip().lower() in ("1", "true", "yes", "on")
        # ELIGIBILITY — two modes. `allowlist` (default, conservative): shed ONLY intents the user marked safe in
        # advisor.bandit_intents. `optout` (advisor.bandit_mode=optout): shed EVERY intent EXCEPT META and an explicit
        # advisor.bandit_denylist — the "use the idle plans by default" posture. Either way the arms EXCLUDE the primary
        # (claude) lane and choose_arm picks the LEARNED-best substitute (or explores an untried one), so quality stays
        # governed by the bake-off learning; DENY an intent that genuinely needs the primary model.
        _mode = str(config._cfg_get("advisor", "bandit_mode", "allowlist")).strip().lower()
        if _mode == "optout":
            _eligible = not _intent_listed(intent, config._cfg_get("advisor", "bandit_denylist", None))
        else:
            _eligible = _intent_listed(intent, config._cfg_get("advisor", "bandit_intents", None))
        if _bandit_on and not str(intent).startswith(_META) and _eligible:
            try:
                from . import lane_bandit, lane_catalog, lane_economics
                _prim_lane = adapters._LANES.get(adapters.provider_for(model), (None,))[0]
                # exclude the primary lane AND any prompt-metered lane whose self-use cap is reached (reserve its
                # stops-dead prompt budget for real coding rather than spend it on discretionary bandit work)
                _arms = [a for a in lane_catalog.arms(config._cfg_get("advisor", "delegate_lanes", None))
                         if a[0] != _prim_lane and not lane_economics.prompt_lane_reserved(a[0])]
                _arm = lane_bandit.choose_arm(intent, _arms)
                if _arm:
                    return f"{lane_catalog.lane_provider(_arm[0])}:{_arm[1]}", f"bandit → {_arm[0]} ({_arm[1]})"
            except Exception:
                pass
        return None, "no confirmed substitute for this intent (propose+confirm first)"
    prov = adapters.provider_for(model)
    util = {l["lane"]: l for l in lane_utilization()["lanes"]}
    primary_lane = adapters._LANES.get(prov, (None,))[0]
    _pu = (util.get(primary_lane) or {}).get("utilization")
    pu = float(_pu) if _pu is not None else 0.0
    # rank the acceptable, available substitutes by recent CALL VOLUME — LEAST-LOADED first (spread evenly across
    # the free plans; ranking by $-utilisation alone piled every overflow onto the cheapest-$ lane while its plan
    # quota was being spent). $-utilisation is the tiebreaker.
    ranked = []
    for spec in subs:
        slane = adapters._LANES.get(spec.split(":", 1)[0], (None,))[0]
        if not slane or slane == primary_lane or adapters._lane_cooling(slane):   # skip unknown/self/cooling lanes
            continue
        _s = util.get(slane) or {}
        _su = _s.get("utilization")
        ranked.append((_s.get("calls_recent", 0), float(_su) if _su is not None else 0.0, spec, slane))
    if not ranked:
        # No FREE substitute LANE available (all cooling / same plan / none configured). REACTIVE ONLY: before the
        # caller pays FULL price on the ORIGINAL model's metered API, take the cheapest AFFORDABLE confirmed METERED
        # substitute (route_utility.rank_metered) — still inside the user-confirmed set, still surfaced/recorded by
        # the caller. Proactive never pays to fill idle plans, so it stops here.
        if reactive:
            _m = _metered_substitute(subs, model)
            if _m:
                return _m["target"], (f"primary lane {primary_lane} FAILED, no idle plan → cheapest metered "
                                      f"substitute {_m['target']} ({_m['why']})")
        return None, "no available substitute lane right now (all cooling, or same plan as primary)"
    ranked.sort(key=lambda t: (t[0], t[1]))
    _cr, su, spec, slane = ranked[0]
    if reactive:
        return spec, f"primary lane {primary_lane} FAILED → {spec} on {slane} ({su:.1f}x used)"
    margin = _util_ratio_cfg("lane_balance_margin", 0.5)
    if pu - su >= margin:
        return spec, f"balance: {primary_lane} {pu:.1f}x vs idle {slane} {su:.1f}x → {spec} (fill idle plan)"
    return None, f"plans already balanced ({primary_lane} {pu:.1f}x vs best {slane} {su:.1f}x, margin {margin})"


def format_utilization():
    """One line per lane for `spendguard lanes --balance` and the router's rationale. Pure est-VALUE (plan usage) —
    split from billed $ per the cost-display rule, and explicitly NOT the provider's quota."""
    u = lane_utilization()
    approx = "" if all(l["fee_exact"] for l in u["lanes"]) else \
        "   (per-lane fee = plan total ÷ lanes; set subscription.lane_plans for exact)"
    star = "*" if u["fee_is_default"] else ""
    head = f"per-plan UTILISATION this month — est-value ÷ plan fee{star}; NOT billed, NOT provider quota:{approx}"
    lines = [head]
    label = {"hot": "🔥 HOT  — shed FROM", "idle": "💤 IDLE — absorb overflow", "warm": "·  ok"}
    for l in u["lanes"]:
        util = f"{l['utilization']:.2f}x" if l["utilization"] is not None else "n/a"
        stale = "" if l["fresh"] else "  (STALE cache — run `spendguard receipt` to refresh)"
        lines.append(f"  {l['lane']:12s} ({l['provider']:9s})  est-value ${l['est_value_month']:>9.2f} / "
                     f"${l['plan_fee']:>6.0f} = {util:>7}  {label[l['state']]}{stale}")
    return "\n".join(lines)


# ── the AGENTIC "model proposes" step (authorization = model proposes, you confirm once). A cheap judge decides which
#    idle-lane CANDIDATE models are acceptable substitutes for an INTENT; the result is recorded PENDING, never used
#    until Ash confirms. Acceptability is a MEANING judgement → an LLM decides it, never a keyword (CLAUDE.md). ──
_PROPOSE_SYS = ("You route LLM work across paid subscription plans to use idle capacity without hurting quality. "
                "Given an INTENT (what the task does), the PRIMARY model in use, and CANDIDATE substitute models on "
                "other (idle) plans, decide which candidates are ACCEPTABLE substitutes — a model whose output would "
                "be GOOD ENOUGH for THIS intent. Be conservative: exclude a candidate if the intent plausibly needs "
                "capability it may lack (deep reasoning, long context, a specific modality). Return only the "
                "acceptable candidate ids, exactly as given.")
_PROPOSE_SCHEMA = {"type": "object", "additionalProperties": False,
                   "properties": {"acceptable": {"type": "array", "items": {"type": "string"}},
                                  "rationale": {"type": "string"}},
                   "required": ["acceptable", "rationale"], "nonempty": ["rationale"]}
_PROPOSE_OUT = 800               # OUTPUT budget for the proposal (a short id list + rationale) — NAMED, not a bare literal


def candidate_models():
    """Substitute candidates = a representative model per IDLE lane, from config `advisor.lane_models` {lane: model}
    (e.g. {"codex":"gpt-5.5","gemini":"gemini-3.7-flash-high","zai-coding":"glm-4.6"}). No hardcoded model list — the
    user declares which model each plan offers; unset → no candidates (the proposer says so)."""
    lm = config._cfg_get("advisor", "lane_models", None) or {}
    idle = set(idle_lanes())
    if not isinstance(lm, dict):
        return []
    return [f"{prov}:{lm[lane]}" for prov, (lane, _m) in adapters._LANES.items()
            if lane in idle and lm.get(lane)]


def propose_substitutes(intent, primary_model, candidates=None):
    """AGENTIC 'model proposes' step: a cheap judge (advisor.judge_model) decides which idle-lane candidate models are
    acceptable substitutes for `intent`, RECORDED AS PENDING for Ash to confirm (never auto-used). Caged as the
    meta intent so its own tiny spend is attributed. Returns {acceptable, rationale, pending}."""
    cands = candidate_models() if candidates is None else list(candidates)
    if not cands:
        return {"acceptable": [], "rationale": "no idle-lane candidate models configured (set advisor.lane_models)",
                "pending": pending_for(intent)}
    from . import calls
    judge = config._cfg_get("advisor", "judge_model", None) or config.advisor_judge_model()
    prompt = (f"INTENT: {intent}\nPRIMARY model (currently used): {primary_model}\n"
              f"CANDIDATE substitute models on idle plans: {cands}\n\n"
              f"Which of the candidates are acceptable substitutes for this intent? Return {{acceptable, rationale}}.")
    with calls.context(intent="spendguard:substitute"):
        r = adapters.call(judge, prompt, system=_PROPOSE_SYS, schema=_PROPOSE_SCHEMA, max_tokens=_PROPOSE_OUT)
    from . import output_contract
    obj, _ = output_contract._as_obj((r or {}).get("text") or "") if (r or {}).get("text") else (None, False)
    acceptable = [c for c in cands if isinstance(obj, dict) and c in (obj.get("acceptable") or [])]  # only real candidate ids
    rationale = (obj.get("rationale") if isinstance(obj, dict) else "") or ""
    if acceptable:
        record_proposal(intent, primary_model, acceptable, proposed_by=judge)
    return {"acceptable": acceptable, "rationale": rationale[:500], "pending": pending_for(intent)}


# ── Stage 3: PROMPT ADAPTATION for a substitute model. The mechanical schema dialect is already handled downstream
#    (adapters.json_schema_request). This is the SEMANTIC layer: agentically rewrite the SYSTEM instruction for the
#    target model WITHOUT changing the task, recorded per (intent, target) so dispatch reuses it mechanically. It
#    composes with the eval gate — an adapted prompt on a new model is a new sig, so it still must pass its own
#    test+eval before it can scale, which is the honest guarantee that adaptation didn't quietly change the task. ──
_ADAPT_SYS = ("You adapt an existing SYSTEM prompt so it works well on a DIFFERENT model, WITHOUT changing the task. "
              "Keep every instruction, constraint, and output requirement identical in MEANING; only adjust phrasing "
              "or format conventions a different model family follows. Do NOT add, drop, or soften any requirement. "
              "If no change is warranted, return the original and changed=false.")
_ADAPT_SCHEMA = {"type": "object", "additionalProperties": False,
                 "properties": {"adapted_system": {"type": "string"}, "changed": {"type": "boolean"},
                                "note": {"type": "string"}},
                 "required": ["adapted_system", "changed", "note"], "nonempty": ["adapted_system"]}
_ADAPT_OUT = 2000                # OUTPUT budget — an adapted system can be as long as the original; NAMED, not a literal


def adapted_system_for(intent, target_model):
    """The RECORDED adapted system for (intent, target_model), or None. Mechanical — dispatch reads this, never an LLM."""
    a = ((_registry().get(intent) or {}).get("adapt") or {}).get(target_model)
    return a.get("system") if isinstance(a, dict) else None


def adapt_system(intent, target_model, system, model=None):
    """AGENTIC (Stage 3): rewrite `system` for `target_model` without changing the task, and RECORD it per
    (intent, target) so dispatch reuses it mechanically. Explicit step (run at confirm time or on demand), never in
    the hot path. Returns {adapted_system, changed, note}. A no-op that records the original when there is no system."""
    system = system or ""
    judge = model or config._cfg_get("advisor", "judge_model", None) or config.advisor_judge_model()
    if not system.strip():
        result = {"adapted_system": "", "changed": False, "note": "no system prompt to adapt"}
    else:
        from . import calls, output_contract
        prompt = (f"TARGET model: {target_model}\nINTENT: {intent}\n\nSYSTEM PROMPT TO ADAPT:\n{system[:8000]}\n\n"
                  f"Adapt it for the target model WITHOUT changing the task. Return {{adapted_system, changed, note}}.")
        with calls.context(intent="spendguard:adapt"):
            r = adapters.call(judge, prompt, system=_ADAPT_SYS, schema=_ADAPT_SCHEMA, max_tokens=_ADAPT_OUT)
        obj, _ = output_contract._as_obj((r or {}).get("text") or "") if (r or {}).get("text") else (None, False)
        result = (obj if isinstance(obj, dict) and obj.get("adapted_system")
                  else {"adapted_system": system, "changed": False, "note": "adaptation unparseable — kept original"})

    def _store_adaptation(d):
        e = d.setdefault(intent, {})
        e.setdefault("adapt", {})[target_model] = {"system": result["adapted_system"],
                                                    "changed": bool(result.get("changed")),
                                                    "note": str(result.get("note") or "")[:300], "by": judge}
    config.update_json(_registry_path(), _store_adaptation, reason="lane-substitute-adapt")
    return result
