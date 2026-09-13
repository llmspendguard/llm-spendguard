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
             enqueue=False, priority=None, schema=None):
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
    The model that answered is the one recorded — attribution stays honest.

    `schema` (a JSON Schema) forces STRUCTURED output — threaded to adapters.call, which folds the shape into the
    lane's prompt, validates the reply locally (output_contract), and falls back to that model's metered API (strict
    on OpenAI/Anthropic) if a lane's output is off-shape. The JSON answer stays in `text`. (A schema request skips
    the text-only lane bandit and uses this cheapest-idle path, which still gets adapters-level lane arbitrage.)"""
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
    if schema is None and str(config._cfg_get("advisor", "lane_bandit", False)).strip().lower() in ("1", "true", "yes", "on"):
        # bandit_call is TEXT-only — a schema request skips it and takes the adapters.call path below, which folds
        # the shape into the lane prompt, validates locally, and falls back to the (strict) API if off-shape.
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
            r = adapters.call(model, task, system=system, reasoning=reasoning, max_tokens=max_tokens, sig=intent,
                              schema=schema)   # structured output: shape rides the lane prompt + local-validate + API fallback
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


def _arity_checked(row, task, expect_ids):
    """ITEM COMPLETENESS (arity) — the one check the per-item shape validator CANNOT do, applied identically to the
    LANE and the vision(API) runners so ONE contract governs both (no drift). A PACKED envelope that is shape-perfect
    but silently DROPS ids (measured ~7% on packed batches) would otherwise be accepted as a success and its missing
    items lost forever. When the caller declares the ids a task expects, verify the id SET: an INCOMPLETE envelope
    becomes a MISS (error row, text cleared) so checkpoint/resume RE-RUNS it and it is NEVER counted done — exactly
    like an off-shape reply. check_envelope counts ids (FORMAT), never judges meaning. Fail CLOSED: a check that
    itself errors marks the row an error (retried, visible), never a swallowed success."""
    if not (expect_ids and _row_succeeded(row)):
        return row
    try:
        from . import output_contract
        _exp = expect_ids(task) if callable(expect_ids) else expect_ids
        if _exp:
            _ok, _det = output_contract.check_envelope(row["text"], _exp)
            if not _ok:
                # reason='arity_miss' — shape-perfect but items DROPPED; a distinct cause from an off-shape/empty miss
                # so a caller can see "the envelope came back short" without parsing the human `error` string.
                return {**row, "text": None, "parsed": None, "arity_miss": _det, "reason": "arity_miss",
                        "error": f"envelope INCOMPLETE: {_det['n_got']}/{_det['n_expected']} ids "
                                 f"({_det['reason']}) — retried, not silently accepted"}
    except Exception as _ae:
        # reason='arity_check_error' — the completeness check itself broke (fail CLOSED: retried, never a swallowed pass).
        return {**row, "text": None, "parsed": None, "reason": "arity_check_error",
                "error": f"completeness check errored ({type(_ae).__name__}: {str(_ae)[:60]}) — not counted done"}
    return row


def _bulk_notify(msg):
    """A fan-wide refusal (an UNDECLARED tier group) is a CONFIG gap that refuses every task of THIS fan — not a
    transient miss. Emit it ONCE PER FAN CALL on stderr: bulk_delegate returns all of a fan's task rows in one shot,
    so this fires once per fan, never per-task/per-row (the spam it was built to avoid). PURE — no module state to go
    stale or grow — so a fan that refuses ALWAYS says why, and a long-lived process that fixes then re-breaks the
    config is never left silently un-warned. The robust machine signal is the structured `reason` on every returned
    row; this stderr line is the human convenience on top."""
    import sys as _s
    print(f"[spendguard] bulk_delegate: {msg}", file=_s.stderr)


def _least_loaded_arm(arms, start, free_of, cooling_of):
    """Pick the (lane, use_name) arm with the MOST free dispatch capacity right now — DYNAMIC least-loaded dispatch,
    the fix for static round-robin's head-of-line blocking (one slow lane queued its whole share while fast lanes
    idled). Scans arms in round-robin order FROM `start` so equal-load ties (the start of a run, every lane empty)
    fall to the round-robin arm — which SEEDS the cross-vendor spread before the fast lanes pull the overflow.
    Skips cooling arms (subsumes the old rotation); every arm cooling → arms[start] (the round-robin pick). Pure:
    free_of(lane)->free_slots and cooling_of(lane)->bool are injected, so the policy is unit-testable offline.
    MEASURED (A/B, bandit pinned off): faster AND tighter wall-clock than static, spectrum preserved (weighted to
    the fast lanes, never collapsed to one) — scripts/probe/dispatch_ab.py."""
    n = len(arms)
    pick, best_free = arms[start % n], -1
    for off in range(n):
        a = arms[(start + off) % n]
        if cooling_of(a[0]):
            continue
        f = free_of(a[0])
        if f > best_free:                             # strictly-greater keeps the FIRST (round-robin-ordered) tie → spread
            best_free, pick = f, a
    return pick


def bulk_delegate(tasks, intent, system=None, reasoning=None, max_workers=None, deadline_s=120.0,
                  checkpoint=None, chunk_size=100, refuse_billed=False, stats=None, force=False, tier=None,
                  schema=None, expect_ids=None, gate_sig=None, lanes=None, images_for=None, vision_model=None,
                  model_for=None, prompt_for=None, task_key=None, return_keyed=False,
                  on_miss=None, batch_submit=None):
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
    error row and never wedges the chunk.

    `schema` (a JSON Schema) forces STRUCTURED output on every task — so a JSON-envelope job (e.g. warden's
    {results:[{id,…}]} contract with cross-model strict-mode parity) can ride the parallel lane fan instead of being
    forced through per-call adapters.call. It is threaded straight to adapters.call: the shape rides each lane's
    prompt and is validated locally (output_contract), and a lane whose reply is off-shape falls back to that model's
    metered API (strict on OpenAI/Anthropic) — so the envelope holds on every lane. The result stays in `text` (the
    JSON string), so the success contract, checkpoint/resume, and tier confinement are unchanged.

    `expect_ids` closes the ARITY hole for PACKED envelopes: a lane can return a shape-PERFECT {results:[{id,…}]}
    that silently OMITS ids (measured ~7% on packed batches), and the per-item shape check is blind to an item that
    never came back — so it would be accepted as a $0 lane success and lost. Pass `expect_ids` = a callable
    (task → the ids that task packed) or a single id-list (all tasks); each result runs through
    output_contract.check_envelope, and an INCOMPLETE envelope becomes a MISS (retried via the checkpoint, surfaced
    with `arity_miss`), NEVER silently counted done.

    `lanes` CONFINES the fan to an explicit lane subset (e.g. only the fungible lanes, excluding a protected
    interactive one) — the same `lanes=` `_bulk_arms` and the tier path already accept, forwarded so a caller can
    say "fan across codex+gemini+zai, never claude-code" without mutating advisor.delegate_lanes. None = every
    delegate lane. A lane named here that is reserved/cooling still drops out (fail-closed), never widened past.

    `images_for` + `vision_model` fan a BULK VISION job across the metered API instead of lanes — the lane executors
    are text-only CLIs with no image channel (the exact trap behind a labeler that cold-400'd). Pass `images_for` =
    a callable (task → the image path(s)/data-URL(s) that task labels) or a single image list (all tasks), and
    `vision_model` = a vision-capable API model (e.g. 'openai:gpt-5-nano', 'gemini:gemini-3-flash'). Every task then
    rides adapters.call(images=…) on that model — governed, checkpointed/resumed, refuse-billed and arity-checked by
    the SAME durable core as the lane fan, just a different per-task runner. lanes/tier/arms do not apply (vision
    never rides a lane); vision_model is REQUIRED when images_for is set (else every task errors, re-runnable).

    `gate_sig` carries the bulk gate onto the LANE path (a lane runs a CLI subprocess, so the SDK-patch gate never
    fires — lane bulk was otherwise ungoverned). A genuinely-bulk fan (>= bulkgate.preview_max) is subject to the
    SAME SPENDGUARD_ENFORCE discipline the SDK path gets: run bulkgate.gated_batch(...) (estimate→test→eval) and pass
    the sig you gated as `gate_sig`; an ungated fan is refused under `block`, logged 'would-block' under `warn`
    (the default), allowed under `off`. force=True overrides. ($0 lanes have no spend to cap — this gate is the
    test/eval discipline, which is about whether the job was proven before N thousand items ran, not about dollars.)

    `task_key` + `return_keyed` pair results back to tasks by MEANING, never position — the same "never by
    position" rule the checkpoint/resume already enforces (`_content_key`), extended to the RETURN so a caller
    can't mis-pair either. The default return is a task-ordered LIST (unchanged), but zipping it with anything but
    the exact `tasks` list — a caller that deduped, filtered, or reordered before fanning — silently crosses rows
    onto the wrong item (the mis-paired-card bug). Pass `task_key` = a callable (task → a hashable key) or a list
    of keys (one per task) and `return_keyed=True` to get back a DICT {key: row}: pairing is explicit and can
    never drift. Keys MUST be unique per task — a duplicate raises (a colliding key would silently DROP a result,
    the very failure this closes), fail-loud not fail-quiet. `task_key=None` keys by index (always unique).

    `on_miss` names what happens to a task the LANES do not serve — the degradation DIRECTION, made explicit
    instead of discovered on an invoice (the default per-task realtime fallback is the MOST expensive shape):
      · "api"   — the current default: a lane miss falls to that model's per-task REALTIME metered API.
      · "error" — a lane miss is a free error row, never billed ($0 by construction). == refuse_billed=True.
      · "batch" — run lanes with NO realtime fallback, then take the MISS SET (no text) as one group. Callers with
                  a batch path want this: lanes first, batch the remainder (~50% cheaper). If `batch_submit` (a
                  callable: the miss tasks → a batch handle/id, e.g. via bulkgate.gated_batch) is given, it is
                  invoked ONCE on the misses and those rows become reason="queued_batch" carrying `batch`=<handle>
                  (async: bulk_delegate never blocks on the 24h batch window); without it, the misses become
                  reason="batch_eligible" (a STRUCTURED signal to route them to your own batch path). Pairs with
                  return_keyed so the caller re-associates queued rows by key, never by position.
    `on_miss` takes precedence over `refuse_billed` when both are set; None keeps the `refuse_billed` behaviour.

    Returns a task-ordered LIST [{text, lane, use_name, model, billed, error}], or {key: row} when return_keyed."""
    import os as _os
    import json as _json
    import threading as _th
    import concurrent.futures as _cf
    from . import adapters, calls, dispatch, lane_catalog

    tasks = list(tasks)
    # MISS POLICY: on_miss (if given) wins over refuse_billed. "error"/"batch" run WITHOUT a realtime fallback (a
    # lane miss stays a textless row); "batch" then groups the misses AFTER the fan (below). Validated at the door.
    if on_miss is not None:
        if on_miss not in ("api", "error", "batch"):
            raise ValueError("bulk_delegate(on_miss=…): expected 'api' | 'error' | 'batch', got %r" % (on_miss,))
        refuse_billed = on_miss in ("error", "batch")
    # CALLER KEYS (return_keyed): resolve BEFORE any fan work so a mis-shaped task_key fails at the door, not after
    # spending. A non-callable task_key is a per-task sequence — it MUST be exactly one key per task (a short list
    # would IndexError mid-fan or, worse, silently truncate the mapping). A callable is resolved per task at finalize.
    if task_key is not None and not callable(task_key):
        task_key = list(task_key)
        if len(task_key) != len(tasks):
            raise ValueError(f"bulk_delegate(task_key=…): {len(task_key)} keys for {len(tasks)} tasks — pass exactly "
                             f"one key per task, a callable(task)->key, or None to key by index.")

    def _caller_key(i):
        if callable(task_key):
            return task_key(tasks[i])
        return task_key[i] if task_key is not None else i

    def _finalize(rows):
        """Every return path builds a task-ORDERED list (one row per task, by index). This is the single seam that
        turns it into {caller_key: row} when return_keyed — so no return site has to know about keying, and the
        list default is byte-for-byte unchanged. A duplicate caller key is FATAL (a silent overwrite would drop a
        real result — the mis-pairing class this feature exists to kill), never a quiet last-writer-wins."""
        if not return_keyed:
            return rows
        out, first_idx = {}, {}
        for i, row in enumerate(rows):
            k = _caller_key(i)
            if k in out:
                raise ValueError(f"bulk_delegate(return_keyed=True): duplicate task_key {k!r} (tasks {first_idx[k]} and {i}) "
                                 f"— keys must be UNIQUE per task; a collision would drop a result. Fix the task_key.")
            out[k] = row
            first_idx[k] = i
        return out
    if not tasks:
        return _finalize([])
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
    # BULK-GATE REACH (the axis-4 gap warden's Q8 found). The estimate→test→eval discipline does NOT follow work
    # onto a lane: a lane runs a CLI subprocess, touching no patched SDK, so gate._bulkgate_check never fires and a
    # lane fan is otherwise UNGOVERNED. The SPEND gate is moot ($0 on a plan) — but "was this proven to work before N
    # thousand ran?" is not about dollars: a free run that emits garbage still burns wall-clock and corrupts
    # downstream. So a genuinely-bulk fan (>= preview_max) gets the SAME enforce-mode gate the SDK path gets. The
    # caller runs bulkgate.gated_batch(...) (estimate→test→eval) and passes the sig it gated as `gate_sig`; here we
    # verify it is fresh+passing. block → REFUSE (GateBlocked, a deliberate stop fail-open handlers re-raise by
    # construction); warn → log 'would-block' and allow (the roll-out default); off → allow. force=True overrides.
    # bulkgate is imported DIRECTLY (not guarded into 'off'): if the safety gate itself cannot load, that is a real
    # failure and must FAIL CLOSED — silently substituting 'off' would let an un-proven fan of thousands run.
    if not force:
        from . import bulkgate as _bg
        _bmode = _bg.mode()
        if _bmode != "off" and len(tasks) >= _bg.preview_max():
            _st = (_bg.gate_status(gate_sig) if gate_sig else
                   {"fresh": False, "reason": "no gate_sig — this fan was never estimated/tested/evaluated"})
            if not _st.get("fresh"):
                _bg._log_block(gate_sig or "lane-bulk", "lanes", len(tasks), 0.0,
                               "blocked" if _bmode == "block" else "would-block")   # (fail-safe internally)
                if _bmode == "block":
                    raise _bg.GateBlocked(
                        f"REFUSED: {len(tasks)} tasks fanned onto lanes for {intent!r} without a fresh gate "
                        f"({_st.get('reason')}). A lane run is $0 but NOT proven — run bulkgate.gated_batch(...) "
                        f"(estimate→test→eval) and pass gate_sig=<the sig you gated>, or force=True to own the risk.")
    # TIER CONFINEMENT (a cost RAIL, declared once). When `tier` is given, each lane contributes its OWN model FOR
    # this capability GROUP — lane_catalog.lane_model_for_tier(lane, tier): its cheap model for `cheap`, its strong
    # model for `strong`. So a bulk fan spreads across every plan's RIGHT-SIZED model (e.g. cheap describe → codex's
    # gpt-5.6-luna + claude-code's haiku + zai's glm), never a premium model for cheap work. FAIL-CLOSED: no lane
    # serves the group (undeclared, its models on no lane), or all serving it are cooling → EVERY task errors
    # (undescribed, re-runnable); we never widen off-tier and never fall onto a strong/Opus lane.
    # VISION fans across the metered API, not lanes (the lane executors are text-only CLIs). When images_for is set,
    # each task's image(s) ride adapters.call(images=…) on vision_model via a different per-task runner (below);
    # arms stay None (no lane picked) and the lane-only tier/reserved machinery is skipped.
    _vision = images_for is not None
    if _vision and not (vision_model or model_for):
        return _finalize([{"text": None, "lane": None, "use_name": None, "billed": False, "reason": "no_vision_model",
                 "error": "bulk_delegate(images_for=…) needs vision_model=… or model_for=… (a vision-capable API "
                          "model); the subscription lanes are text-only, so bulk vision fans across the metered API"}
                for _ in tasks])
    if _vision:
        arms = None
    elif tier:
        # Split the model-DECLARED lanes from the AVAILABLE ones so a PERMANENT config gap (no lane serves this group
        # at all → refuses every run) is DISTINGUISHABLE from a TRANSIENT one (lanes serve it but are all cooling right
        # now → retry later). They demand opposite responses and used to be one message, indistinguishable as N misses.
        _pool = lanes or lane_catalog.lanes()
        _declared = [(ln, m) for ln in _pool if (m := lane_catalog.lane_model_for_tier(ln, tier))]
        arms = [(ln, m) for ln, m in _declared if not adapters._lane_cooling(ln)]
        if not arms:
            if not _declared:                            # PERMANENT: nothing serves this group → refuses every run
                _reason, _msg = "tier_undeclared", (
                    f"--tier {tier!r}: NO lane declares a model for this group — advisor.tiers[{tier!r}] is unset, or "
                    f"none of its models is any lane's advisor.lane_models entry. A CONFIG gap: it refuses every task, "
                    f"every run (not a busy-capacity blip). Declare it — `spendguard tiers set {tier} <model…>` and "
                    f"`spendguard lanes set-model <lane> <model>` — then re-run.")
                _bulk_notify(_msg)                       # loud at the door (once per fan), never per-row
            else:                                        # TRANSIENT: lanes DO serve it, all cooling now → retry later
                _reason, _msg = "all_lanes_cooling", (
                    f"--tier {tier!r}: every lane serving this group is cooling right now "
                    f"({', '.join(ln for ln, _m in _declared)}) — refusing rather than widening off-tier. TRANSIENT "
                    f"(capacity), NOT a config gap; retry when a lane frees, or add another lane to the group.")
            return _finalize([{"text": None, "lane": None, "use_name": None, "billed": False, "reason": _reason, "error": _msg}
                    for _ in tasks])
    else:
        arms = _bulk_arms(intent, lanes=lanes)
        if not arms:
            return _finalize([{"text": None, "lane": None, "use_name": None, "billed": False, "reason": "no_viable_lane",
                     "error": "no viable lane (set advisor.lane_models; check `spendguard lanes`)"} for _ in tasks])

    if not _vision:
        # RESERVED-LANE GUARD — the SAME reservation idle_lanes() and route_decision() already honor, applied here
        # too (bulk_delegate was the one router that skipped it). A prompt-metered lane whose SELF-USE cap is reached
        # keeps its stops-dead budget for real coding, so it must not take DISCRETIONARY bulk overflow — and the
        # tier= path draws from lane_catalog.lanes(), which would otherwise ride a protected lane's cheap model.
        # Filter in ONE place so BOTH tier and _bulk_arms honor it; fail closed (error rows, never widen onto one).
        from . import lane_economics
        _reserved = sorted({a[0] for a in arms if lane_economics.prompt_lane_reserved(a[0])})
        if _reserved:
            arms = [a for a in arms if a[0] not in _reserved]
            if not arms:
                return _finalize([{"text": None, "lane": None, "use_name": None, "billed": False, "reason": "all_lanes_reserved",
                         "error": f"every viable lane is reserved ({', '.join(_reserved)}: self-use cap reached — "
                                  f"its prompt budget is held for real coding, not discretionary bulk). Re-run when "
                                  f"a lane frees up, or widen advisor.delegate_lanes."} for _ in tasks])

    import hashlib as _hl

    def _content_key(task):
        # CONTENT identity, never position: sha256(system + task + intent). A re-run whose task LIST changed — symgrep
        # re-describes only CHANGED functions, so order + composition shift between runs — resumes by MEANING, so a
        # saved result can never be mapped onto a different task (a wrong description written into an index is the
        # worst failure class). Two identical tasks share a key (identical result) — correct, not a collision.
        h = _hl.sha256()
        base = str(system) + "\x00" + str(task) + "\x00" + str(intent)
        _m = model_for(task) if callable(model_for) else None
        if _m:                                           # a resolved PER-TASK model (cross-vendor panel) is part of the
            base += "\x00" + str(_m)                     # identity → two vendors on one task resume as SEPARATE results;
            #                                              no per-task model (None, or model_for absent) → key unchanged
        h.update(base.encode("utf-8", "replace"))
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
        return _finalize(results)                     # fully resumed from the checkpoint — nothing left to run

    _cklock = _th.Lock()

    _ck_failures = [0]                                       # checkpoint-write failures this run (LOUD, surfaced in stats)

    def _checkpoint(i, res):
        # DURABLE by construction, not single-copy: `res` is ALSO returned to the caller in-memory (results[i]) and the
        # task is DETERMINISTICALLY RE-RUNNABLE from `tasks` at $0 on a lane — so a lost checkpoint line is a re-run,
        # never lost work. The checkpoint is the caller's chosen RESUME log (an append journal replayed by CONTENT key),
        # whose path the caller guarantees. This function's ONLY job is to keep that resume guarantee, and to make its
        # FAILURE loud rather than silent.
        if not checkpoint:
            return
        try:
            with _cklock, open(checkpoint, "a") as f:        # one durable line per finished task, keyed by CONTENT
                f.write(_json.dumps({"k": _keys[i], "r": res}) + "\n")
        except Exception as e:
            # The in-hand result is STILL returned (results[i]) — a checkpoint-write failure loses nothing in THIS run.
            # But it must be LOUD, never swallowed: a silently-unwritable checkpoint (disk full / path gone) means the
            # run FINISHES yet a later RESUME re-runs from the last good line — re-doing work (or re-paying on a billed
            # lane). Count every failure and announce the FIRST once (never per-row spam); the count rides `stats` so
            # the caller sees the resume guarantee degraded, not a clean run.
            with _cklock:
                _ck_failures[0] += 1
                _first = _ck_failures[0] == 1
            if _first:
                _bulk_notify(f"checkpoint write FAILED ({type(e).__name__}: {str(e)[:80]}) to {checkpoint} — results "
                             f"still returned, but a RESUME will re-run from the last good line. Fix the path/disk.")

    n = int(max_workers or dispatch._limit("global_concurrency", 24))
    # TAIL-HEDGING (opt-in, default OFF): if a task's PRIMARY lane hasn't returned a SERVED row within this many ms,
    # fire a DUPLICATE on the most-free OTHER lane and take whichever yields text first. Dynamic dispatch (above)
    # fixes the per-LANE tail (a slow lane no longer head-of-line-blocks); hedging fixes the per-CALL tail (one
    # request that stalls while its lane is otherwise fine). 0 = off (no extra lane load); set it to the intent's
    # measured p90–p95 latency, never lower (a too-low value hedges EVERY task = 2x lane load for no tail win).
    # Read via dispatch._limit so it shares the dispatch config surface: `dispatch.lane_hedge_ms` or env
    # SPENDGUARD_DISPATCH_LANE_HEDGE_MS. The hedge itself NEVER bills (below), so worst case is one free lane miss.
    _hedge_ms = int(dispatch._limit("lane_hedge_ms", 0))

    _MAX_IMAGE_BYTES = 32 * 1024 * 1024               # bound raw image bytes so a giant file is a LOUD row, not an OOM
    from . import gate as _gate
    _STOP_TYPES = tuple(_gate.deliberate_stop_types())  # includes DispatchTimeout — fail CLOSED (no degrading fallback)

    def _image_too_big(imgs):
        # size BEFORE load: a data: URL by its string length, a path by its file size. Returns (reason_code, message)
        # so callers branch on the CODE (structured), not free-form text — an image that would OOM fails loud and
        # re-runnable instead. None when every image is within cap.
        for im in imgs:
            try:
                sz = len(im) if (isinstance(im, str) and im.startswith("data:")) else _os.path.getsize(im)
            except Exception as e:
                return ("image_unreadable", f"image unreadable ({type(e).__name__}: {str(e)[:50]})")
            if sz > _MAX_IMAGE_BYTES:
                return ("image_too_big", f"image {sz // 1024}KB exceeds {_MAX_IMAGE_BYTES // 1024}KB cap — downscale first")
        return None

    def _run_task_on_api(i, task):
        # VISION (images_for) task: ride the metered API — the lanes have no image channel — governed like a lane
        # slot, with the SAME arity/shape handling. model_for(task) gives a PER-TASK model (durable cross-vendor
        # PANEL), else the single vision_model; prompt_for(task) lets the task be an opaque identity (e.g. a vendor
        # id, so each vendor is keyed separately) while every task shares the same prompt. images_for(task) → images.
        _vm = (model_for(task) if callable(model_for) else None) or vision_model
        _p = prompt_for(task) if callable(prompt_for) else task
        _raw = _vm.split(":", 1)[1] if (_vm and ":" in _vm) else (_vm or "?")
        _b = {"text": None, "lane": "api", "use_name": _raw, "model": _vm, "billed": False}
        if not _vm:
            return i, {**_b, "reason": "no_vision_model", "error": "model_for returned no model for this task"}
        _imgs = list(images_for(task) if callable(images_for) else (images_for or []))
        if not _imgs:
            return i, {**_b, "reason": "no_image", "error": "images_for returned empty — a vision task needs an image"}
        _big = _image_too_big(_imgs)
        if _big:
            return i, {**_b, "reason": _big[0], "error": _big[1]}
        _prov = adapters.provider_for(_vm)
        try:
            dispatch.acquire(_prov, _raw, deadline_s)   # governor: bound in-flight vision calls, like the lane path
        except _STOP_TYPES:
            raise                                       # a DELIBERATE stop (deadline/refusal) halts — never a per-task row
        except Exception as e:
            return i, {**_b, "reason": "dispatch", "error": f"dispatch: {str(e)[:60]}"}
        try:
            calls.set_context(intent=intent)            # tag this worker's calls with the intent (attribution)
            r = adapters.call(_vm, _p, system=system, reasoning=reasoning, sig=intent,
                              timeout_s=deadline_s, no_metered_fallback=refuse_billed, schema=schema,
                              images=_imgs, no_substitution=True)   # NAMED vision model — never swap it
        except _STOP_TYPES:
            raise
        except Exception as e:
            return i, {**_b, "reason": "call_raised", "error": str(e)[:80]}
        finally:
            dispatch.release(_prov, _raw)
        r = r if isinstance(r, dict) else {}
        _sp, _sm = r.get("provider") or _prov, r.get("model") or _raw
        # Same structured-reason contract as the lane row: adapters' code, else 'api_error' on a failed metered call
        # (vision always rides the metered API), else None when served. No error row is ever reason-less.
        _row_reason = r.get("reason") or ("api_error" if r.get("error") else None)
        row = {"text": (r.get("text") or None), "lane": r.get("executor") or "api", "use_name": _sm,
               "model": f"{_sp}:{_sm}", "billed": bool(r.get("cost")),
               "served_by_metered_api": (r.get("executor") or "api") in ("api", "api-fallback"),
               "parsed": (r.get("parsed") if schema is not None else None),
               "reason": _row_reason, "error": r.get("error")}
        return i, _arity_checked(row, task, expect_ids)

    def _pick_arm(i):
        # DYNAMIC LEAST-LOADED dispatch (replaces static round-robin arms[i % n], which HEAD-OF-LINE-BLOCKS: a task
        # was bound to lane i%n before the run, so one momentarily-slow lane queued its whole share while the fast
        # lanes finished theirs and IDLED — the wall-clock became the slowest lane's chain). Instead, pick the arm
        # with the MOST FREE dispatch capacity RIGHT NOW (dispatch.lane_free), scanning in round-robin order FROM
        # i%n so ties (all equally free — the start of a run) still fall to the round-robin arm. This PRESERVES the
        # cross-vendor SPREAD (an empty lane is fully free → filled first, so every vendor gets work — the diversity
        # the fan exists for) AND removes the idle/bottleneck (a fast lane pulls the overflow; a slow/busy lane stops
        # attracting work). Cooling arms are skipped here (subsuming the old rotation). All cooling → round-robin pick.
        _n_arms = len(arms)
        _start = i % _n_arms
        if _os.environ.get("SPENDGUARD_LANE_STATIC_DISPATCH"):   # opt-out to the OLD static round-robin (A/B + safety)
            lane, use_name = arms[_start]
            if adapters._lane_cooling(lane) and _n_arms > 1:
                for _j in range(1, _n_arms):
                    _al, _au = arms[(i + _j) % _n_arms]
                    if not adapters._lane_cooling(_al):
                        lane, use_name = _al, _au
                        break
            return lane, use_name
        return _least_loaded_arm(arms, i, dispatch.lane_free, adapters._lane_cooling)

    def _attempt_on_lane(i, task, lane, use_name, no_fallback):
        # ONE attempt on ONE named lane — the body shared by the plain path AND each side of a hedge race. `no_fallback`
        # is the caller's refuse_billed for the PRIMARY, but ALWAYS True for a HEDGE (a hedge can never bill — at worst
        # a free lane miss). Returns (i, row); a DELIBERATE stop RAISES (halts the fan), never a textless row.
        prov = lane_catalog.lane_provider(lane)
        model = f"{prov}:{use_name}"
        try:
            dispatch.acquire(prov, use_name, deadline_s)     # governor: bounds per-lane in-flight (fills, never swarms)
        except _STOP_TYPES:
            raise                                            # a DELIBERATE stop (DispatchTimeout admission shed / a
            #                                                  refusal) HALTS the fan — NOT buried as an unserved row;
            #                                                  the caller sees the raise and decides (retry lanes, or batch)
        except Exception as e:
            # reason='dispatch' — a NON-deliberate dispatch error, structurally separable from a shape/empty/quota miss.
            return i, {"text": None, "lane": lane, "use_name": use_name, "model": model, "billed": False,
                       "reason": "dispatch", "error": f"dispatch: {str(e)[:60]}"}
        try:
            calls.set_context(intent=intent)          # tag this worker thread's calls with the intent (attribution)
            r = adapters.call(model, task, system=system, reasoning=reasoning,   # sig=intent → the OUTPUT budget is this
                              sig=intent, timeout_s=deadline_s,                  # call-class's measured p99; no_fallback
                              no_metered_fallback=no_fallback,                   # → a lane miss errors, never a paid retry
                              schema=schema,                                     # STRUCTURED output: adapters folds the shape
                              #                                                    into the lane's prompt + validates locally,
                              #                                                    falling back to the API (strict) if off-shape
                              no_substitution=bool(tier or lanes))               # CONFINEMENT: tier= OR lanes= pins the
            #                                                                      arm — the bandit can NEVER swap it for a
            #                                                                      model OUTSIDE the requested set; the only
            #                                                                      fallback is THIS model's metered API,
            #                                                                      in-tier by construction
            # (receipt suppressed via set_context above, not the context manager)  each reply feeds that measurement
        except _STOP_TYPES:
            raise                                            # a deliberate stop (refusal / deadline) propagates — it is
            #                                                  NOT downgraded to a textless row (adapters.call is not
            #                                                  supposed to raise these, but if one escapes, halt not hide)
        except Exception as e:
            # reason='call_raised' — adapters.call itself raised (it normally returns an error dict); a distinct cause
            # from a shape/empty/quota miss, so a caller can tell "the call machinery broke" from "the model missed".
            return i, {"text": None, "lane": lane, "use_name": use_name, "model": model, "billed": False,
                       "reason": "call_raised", "error": str(e)[:80]}
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
        # STRUCTURED reason: adapters' code (empty/shape_miss/lane_error/quota) when it gave one; else, if this is an
        # error row with NO adapters reason, it is the refuse_billed=False path whose METERED fallback itself failed —
        # 'api_error'. A served row (text, no error) is reason=None. So NO error row is ever reason-less (from KNOWN
        # state — which branch produced it — not a judgement about the text).
        _row_reason = r.get("reason") or ("api_error" if r.get("error") else None)
        row = {"text": (r.get("text") or None), "lane": served_lane, "use_name": served_model,
               "model": f"{served_prov}:{served_model}", "billed": bool(r.get("cost")),
               # `billed`=cost>0 (true for a costing key-lane too); THIS is the field to prove metered-API service —
               # a lane miss fell through to the paid provider. A $0 or costing LANE is served_by_metered_api=False.
               "served_by_metered_api": served_lane in ("api", "api-fallback"),
               # DECODED object when a schema was requested (adapters already parsed it) — so the demux scatters the
               # object, never a re-parse of `text`. None if it did not decode; cleared to None on an arity miss.
               "parsed": (r.get("parsed") if schema is not None else None),
               # reason: empty / shape_miss / lane_error / quota (from adapters) or api_error (metered fallback failed).
               # Structurally separable from a dispatch/arity miss so a caller routes by CAUSE, never by string-sniffing
               # `error`. None only when the row was SERVED (has text). See _row_reason above.
               "reason": _row_reason, "error": r.get("error")}
        if r.get("substituted_from") and f"{served_prov}:{served_model}" != f"{prov}:{use_name}":
            row["intended"] = f"{prov}:{use_name}"           # what the pick chose, before the substitution
            row["substituted_from"] = r["substituted_from"]
        # ITEM COMPLETENESS (arity): a shape-perfect packed envelope that silently DROPPED ids becomes a retried MISS,
        # never a $0 success with items lost. One shared contract with the vision runner (_arity_checked).
        return i, _arity_checked(row, task, expect_ids)

    def _hedged_attempt(i, task, lane, use_name):
        # TAIL-HEDGING (opt-in via _hedge_ms): run the PRIMARY; if it hasn't returned a SERVED row within _hedge_ms,
        # fire a DUPLICATE on the most-free OTHER lane and take whichever yields text FIRST. Kills the per-CALL tail
        # (one stalled request stretching the whole batch wall) that dynamic dispatch alone can't — dispatch balances
        # LANES, hedging rescues a single slow CALL. CONTAINED to the per-task runner: the chunk loop, its per-chunk
        # pool, and _checkpoint are UNTOUCHED (the durability rule — branch inside the runner, never the chunk loop).
        # $0: the hedge always runs no_fallback=True, so it can only ever cost a free lane miss. DIVERSITY: the hedge
        # lands on a DIFFERENT vendor and ONLY on the tail; the winner records hedged=True + the peer lane, so any
        # skew toward fast vendors is VISIBLE/measurable, never silent. The loser is NOT joined (that would re-add the
        # tail) — pool.shutdown(wait=False); its dispatch slot releases in _attempt_on_lane's own finally when it ends.
        pool = _cf.ThreadPoolExecutor(max_workers=2)
        try:
            primary = pool.submit(_attempt_on_lane, i, task, lane, use_name, refuse_billed)
            try:
                res = primary.result(timeout=_hedge_ms / 1000.0)   # a deliberate stop re-raises here → halts the fan
            except _cf.TimeoutError:
                res = None
            if res is not None and (res[1] or {}).get("text"):
                return res                                    # primary SERVED within the window — the common case, no hedge
            # primary is SLOW (still running) or MISSED — hedge on the most-free OTHER non-cooling lane. The lambda
            # forces the primary's own free to -1 so _least_loaded_arm can never re-pick it (it prefers strictly-more).
            hlane, hname = _least_loaded_arm(
                arms, i, lambda l: -1.0 if l == lane else float(dispatch.lane_free(l)), adapters._lane_cooling)
            if hlane == lane or adapters._lane_cooling(hlane):   # no DISTINCT, non-cooling lane to hedge onto — wait it out
                return res if res is not None else primary.result()
            hedge = pool.submit(_attempt_on_lane, i, task, hlane, hname, True)   # hedge is ALWAYS $0 (no_fallback=True)
            pending = {hedge} if res is not None else {primary, hedge}
            fallback = res                                    # a primary miss we already hold; returned iff the hedge misses too
            while pending:
                done, pending = _cf.wait(pending, return_when=_cf.FIRST_COMPLETED)
                for f in done:
                    fr = f.result()                           # a deliberate stop from either side re-raises → halts the fan
                    if (fr[1] or {}).get("text"):
                        row = dict(fr[1])
                        row["hedged"] = True                  # this row was RACED; row["lane"] already names who actually served
                        row["hedge_peer"] = lane if f is hedge else hlane
                        return fr[0], row
                    fallback = fallback or fr                 # keep the first miss as the fallback if BOTH sides miss
            return fallback if fallback is not None else res
        finally:
            pool.shutdown(wait=False)                         # NON-BLOCKING — never join the loser (that re-adds the tail)

    def _run_task_on_lane(i, task):
        if _vision:                                    # vision fans across the API (lanes are text-only) — same core
            return _run_task_on_api(i, task)
        lane, use_name = _pick_arm(i)                  # DYNAMIC least-loaded (or static under SPENDGUARD_LANE_STATIC_DISPATCH)
        if _hedge_ms <= 0 or len(arms) < 2:            # hedging off, or only one lane → the plain single attempt (unchanged path)
            return _attempt_on_lane(i, task, lane, use_name, refuse_billed)
        return _hedged_attempt(i, task, lane, use_name)

    # CHUNKED: bound how many futures are in flight at once, and make each chunk's results durable before the next.
    for c0 in range(0, len(todo), max(1, int(chunk_size))):
        chunk = todo[c0:c0 + max(1, int(chunk_size))]
        with _cf.ThreadPoolExecutor(max_workers=max(1, n)) as ex:
            for fut in _cf.as_completed([ex.submit(_run_task_on_lane, i, tasks[i]) for i in chunk]):
                i, res = fut.result()
                results[i] = res
                _checkpoint(i, res)

    # on_miss="batch": the lanes ran with NO realtime fallback; take the MISS SET (rows with no text) as ONE group
    # and degrade it toward BATCH, not per-task realtime. With batch_submit, submit the whole remainder ONCE and
    # mark those rows queued_batch (async — never block on the 24h window); without it, mark them batch_eligible so
    # the caller routes them to its own batch path. A DELIBERATE stop from the submit HALTS (never hidden); any other
    # submit failure leaves the misses batch_eligible with a loud notice (never silently "queued" when it wasn't).
    if on_miss == "batch":
        miss_idx = [i for i in range(len(tasks)) if not (results[i] or {}).get("text")]
        if miss_idx:
            handle, _submitted = None, False
            if callable(batch_submit):
                try:
                    handle = batch_submit([tasks[i] for i in miss_idx])   # ONE batch for the whole remainder
                    _submitted = True
                except _STOP_TYPES:
                    raise
                except Exception as _e:
                    _bulk_notify(f"on_miss=batch: batch_submit raised ({type(_e).__name__}: {str(_e)[:60]}) — "
                                 f"{len(miss_idx)} miss(es) left batch_eligible, not queued")
            for i in miss_idx:
                row = dict(results[i] or {})
                if _submitted:
                    row["reason"], row["batch"] = "queued_batch", handle
                else:
                    row["reason"] = "batch_eligible"
                results[i] = row
    if stats is not None and _ck_failures[0]:                # a degraded RESUME guarantee rides stats, not just a notice
        stats["checkpoint_failures"] = _ck_failures[0]
    return _finalize(results)


def estimate_fan(tasks, intent, system=None, lanes=None, tier=None, out_est=None):
    """ZERO-SPEND preview of a bulk_delegate fan — the estimate-first discipline for the LANE path. bulk_delegate
    itself just runs; this answers "what would it cost / where would it land" BEFORE a single call is made. Returns
    a plan dict, never touches a lane or an API:
      · n_tasks / n_distinct — total vs DISTINCT calls (identical tasks share one content key → one real call), so
        the dedup win is visible before running;
      · arms — the (lane, model) set the fan WOULD spread across, resolved the SAME way bulk_delegate resolves it
        (tier= → each lane's right-sized model; else the bandit's GOOD lanes for this intent), reserved lanes
        filtered out identically; or `viable=False` + a reason when none is available (no spend either way);
      · est_metered_usd_worst — the CEILING: every DISTINCT task falls off its lane to that model's METERED API
        (the refuse_billed=False worst case). On plan-served lanes the real spend is $0; this is the number to
        approve a fan against, never the expected cost. Priced from `pricing` (never a literal); INPUT tokens are
        exact from the task+system text, OUTPUT from the intent's MEASURED p99 (bulkgate.maxtokens) when known,
        else `out_est`, else a config nominal — `out_basis` says which so the estimate is auditable.
    Mirrors crossllm's "budget_usd is None → estimate only, never spends" pattern for the lane fan."""
    import hashlib as _hl
    from . import adapters, lane_catalog, lane_economics, bulkgate, pricing, config, provider_tokens

    tasks = list(tasks)
    n_tasks = len(tasks)
    # DISTINCT calls: identical (system+task+intent) collapse to one content key — the same identity bulk_delegate's
    # resume uses — so the preview counts the calls that will actually run, not the raw list length.
    seen = set()
    for t in tasks:
        h = _hl.sha256((str(system) + "\x00" + str(t) + "\x00" + str(intent)).encode("utf-8", "replace"))
        seen.add(h.hexdigest()[:24])
    n_distinct = len(seen)

    # ARMS — resolved identically to bulk_delegate (tier group models, else the intent's good lanes), then the SAME
    # reserved-lane filter. A refusal here is a $0 "this fan can't run as asked" answer, not an error.
    if tier:
        _pool = lanes or lane_catalog.lanes()
        _declared = [(ln, m) for ln in _pool if (m := lane_catalog.lane_model_for_tier(ln, tier))]
        arms = [(ln, m) for ln, m in _declared if not adapters._lane_cooling(ln)]
        if not arms:
            reason = "tier_undeclared" if not _declared else "all_lanes_cooling"
            return {"viable": False, "reason": reason, "n_tasks": n_tasks, "n_distinct": n_distinct, "arms": [],
                    "est_metered_usd_worst": 0.0, "note": f"--tier {tier!r}: no runnable lane ({reason}) — nothing to estimate."}
    else:
        arms = _bulk_arms(intent, lanes=lanes)
        if not arms:
            return {"viable": False, "reason": "no_viable_lane", "n_tasks": n_tasks, "n_distinct": n_distinct,
                    "arms": [], "est_metered_usd_worst": 0.0,
                    "note": "no viable lane (set advisor.lane_models; check `spendguard lanes`) — nothing to estimate."}
    _reserved = sorted({a[0] for a in arms if lane_economics.prompt_lane_reserved(a[0])})
    if _reserved:
        arms = [a for a in arms if a[0] not in _reserved]
    if not arms:
        return {"viable": False, "reason": "all_lanes_reserved", "n_tasks": n_tasks, "n_distinct": n_distinct,
                "arms": [], "est_metered_usd_worst": 0.0,
                "note": f"every viable lane is reserved ({', '.join(_reserved)}) — nothing to estimate."}

    # OUTPUT tokens — the intent's MEASURED p99 (never a literal nobody picked); fall back to a caller value, then a
    # config nominal, recording which basis was used so the ceiling is auditable.
    mt = bulkgate.maxtokens(intent)
    if mt.get("p99"):
        out_tok, out_basis = int(mt["p99"]), f"measured p99 (n={mt.get('n')})"
    elif out_est:
        out_tok, out_basis = int(out_est), "caller out_est"
    else:
        out_tok = int(config._cfg_get("advisor", "route_est_out", None) or ROUTE_EST_OUT_DEFAULT)
        out_basis = "config nominal (no measured p99 yet)"

    # WORST-CASE metered ceiling: each DISTINCT task priced at its round-robin arm's model on the paid API. Input
    # tokens are exact from the actual task+system text; unpriced models contribute $0 and are surfaced, never hidden.
    distinct_tasks, seen2 = [], set()
    for t in tasks:
        k = _hl.sha256((str(system) + "\x00" + str(t) + "\x00" + str(intent)).encode("utf-8", "replace")).hexdigest()[:24]
        if k not in seen2:
            seen2.add(k); distinct_tasks.append(t)
    est, n_unpriced, _sys = 0.0, 0, str(system or "")
    for j, t in enumerate(distinct_tasks):
        ln, use_name = arms[j % len(arms)]
        prov = lane_catalog.lane_provider(ln)
        model = f"{prov}:{use_name}"
        _txt = (_sys + "\n" + str(t)) if _sys else str(t)
        in_tok = provider_tokens.count_text(_txt, provider=prov, model=model)   # PROVIDER-AWARE (real BPE × factor)
        try:                                             # REALTIME price (not batch) — the worst-case ceiling is the
            est += float(pricing.realtime_cost(model, in_tok, out_tok, provider=prov) or 0.0)   # metered fallback price
        except (KeyError, TypeError, ValueError):        # no price card → count it, exclude from the ceiling (never $0-hide)
            n_unpriced += 1
    return {"viable": True, "reason": None, "intent": intent, "n_tasks": n_tasks, "n_distinct": n_distinct,
            "arms": [f"{lane_catalog.lane_provider(ln)}:{m}" for ln, m in arms],
            "est_metered_usd_worst": round(est, 6), "n_unpriced": n_unpriced, "out_tok": out_tok, "out_basis": out_basis,
            "note": f"$0 on the {len(arms)} plan lane(s); worst case ${round(est, 4)} if all {n_distinct} distinct "
                    f"tasks fell to the metered API (out={out_tok} tok, {out_basis})"
                    + (f"; {n_unpriced} model(s) unpriced (excluded from the ceiling)" if n_unpriced else "")}


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


def bandit_list_coverage():
    """Which bandit allow/deny-list entries are UNMATCHED — they match NO intent recorded in the calls ledger. This
    is a FACT, not a verdict: the determination runs the SAME _intent_listed the router uses (exact / trailing ':'
    or '*' prefix) against the DISTINCT recorded intents, so an unmatched entry is one the router would never act on
    given what has actually been seen. It may be a typo, a stale name, OR an intent not yet run — which of those is
    for the operator to judge; the guard's job is only to make the absence VISIBLE (two unmatched denylist entries
    once sat in a live config and nothing said so — axis-4). {list_key: {entries, unmatched, seen_n, read_ok}} per
    configured list; unmatched is None when the ledger could not be read. Read-only, $0 — surfaced by `spendguard doctor`."""
    import sqlite3
    seen, read_ok = [], True
    try:
        con = sqlite3.connect(config.db_path(), timeout=10)
        seen = [r[0] for r in con.execute(
            "SELECT DISTINCT intent FROM calls WHERE intent IS NOT NULL AND intent != ''").fetchall()]
        con.close()
    except sqlite3.Error:
        # ONLY a sqlite read failure is handled here (a config deliberate-stop from db_path() propagates). A read
        # FAILURE is not "no intents recorded" — flagging every entry DEAD then would cry wolf. 'cannot tell' !=
        # 'all dead': report read_ok=False, dead=None, and let the surface say "could not check", not invent a finding.
        read_ok = False
    out = {}
    for key in ("bandit_denylist", "bandit_intents"):
        entries = list(config._cfg_get("advisor", key, None) or [])
        if not entries:
            continue
        out[key] = {"entries": entries, "seen_n": len(seen), "read_ok": read_ok,
                    "unmatched": ([e for e in entries if not any(_intent_listed(it, [e]) for it in seen)] if read_ok else None)}
    return out


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
