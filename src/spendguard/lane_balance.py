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

# Thresholds are CONFIG, never hardcoded: a plan whose est-value is below IDLE_RATIO of its fee has spare capacity;
# above HOT_RATIO of its fee it is saturated. Defaults are starting points, tunable per `advisor.lane_*_ratio`.
IDLE_RATIO_DEFAULT = 0.5
HOT_RATIO_DEFAULT = 1.5


def _util_ratio_cfg(name, default):
    try:
        return float(config._cfg_get("advisor", name, None) or default)
    except (TypeError, ValueError):
        return default


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
                    "fee_exact": fee_exact, "state": state, "fresh": fresh})
    return {"lanes": out, "total_fee": round(float(total_fee), 2), "fee_is_default": fee_default,
            "asof": receipt._windows()[0]}


def hot_lanes():
    """Lanes that are saturated (shed work FROM these)."""
    return [l["lane"] for l in lane_utilization()["lanes"] if l["state"] == "hot"]


def idle_lanes():
    """Lanes with spare capacity (route overflow TO these), least-utilised first — the order the router prefers."""
    idle = [l for l in lane_utilization()["lanes"] if l["state"] == "idle"]
    return [l["lane"] for l in sorted(idle, key=lambda l: (l["utilization"] if l["utilization"] is not None else 0.0))]


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
        with calls.context(intent="spendguard:delegate"):
            r = adapters.call(model, task, system=system, reasoning=reasoning, max_tokens=max_tokens)
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


def bulk_delegate(tasks, intent, system=None, reasoning=None, max_workers=None, deadline_s=120.0,
                  checkpoint=None, chunk_size=100):
    """Fan a LIST of similar tasks across ALL viable idle lanes CONCURRENTLY — the right shape for a BULK job (e.g.
    symgrep's ~6k one-sentence symbol descriptions) that the per-call bandit would trickle one at a time. Each task
    runs on a lane (round-robin across the lanes the bandit rates GOOD for this intent), each admission BOUNDED by
    the dispatch GOVERNOR (per-lane in-flight cap, so it FILLS the plans without tripping their throttle). So it is
    FAST (parallel across lanes), $0 (plan-served; a lane failure falls back to that provider's API, flagged
    `billed`), and spread across EVERY good lane, not one.

    DURABLE (the CHUNK-never-single-shot rule): tasks run in chunks of `chunk_size`; when `checkpoint` (a jsonl path)
    is given, EACH completed result is appended before the next chunk, so a crash RESUMES (re-run with the same
    checkpoint skips finished tasks) instead of losing the whole run. One task that errors returns an error result
    and never wedges the chunk. Returns [{text, lane, use_name, billed, error}] in task order."""
    import os as _os
    import json as _json
    import threading as _th
    import concurrent.futures as _cf
    from . import adapters, calls, dispatch, lane_catalog

    tasks = list(tasks)
    if not tasks:
        return []
    arms = _bulk_arms(intent)
    if not arms:
        return [{"text": None, "lane": None, "use_name": None, "billed": False,
                 "error": "no viable lane (set advisor.lane_models; check `spendguard lanes`)"} for _ in tasks]

    results = [None] * len(tasks)
    if checkpoint and _os.path.exists(checkpoint):   # RESUME: reload finished tasks so a re-run never re-pays them
        try:
            with open(checkpoint) as f:
                for ln in f:
                    try:
                        rec = _json.loads(ln)
                        if 0 <= int(rec["i"]) < len(tasks):
                            results[int(rec["i"])] = rec["r"]
                    except Exception:
                        continue
        except Exception:
            pass
    todo = [i for i in range(len(tasks)) if results[i] is None]
    if not todo:
        return results                                # fully resumed from the checkpoint — nothing left to run

    _cklock = _th.Lock()

    def _checkpoint(i, res):
        if not checkpoint:
            return
        try:
            with _cklock, open(checkpoint, "a") as f:        # append one durable line per finished task
                f.write(_json.dumps({"i": i, "r": res}) + "\n")
        except Exception:
            pass                                             # a checkpoint-write failure must not lose the in-hand result

    n = int(max_workers or dispatch._limit("global_concurrency", 24))

    def _run_task_on_lane(i, task):
        lane, use_name = arms[i % len(arms)]          # round-robin — spread across the good lanes for parallelism
        prov = lane_catalog.lane_provider(lane)
        model = f"{prov}:{use_name}"
        try:
            dispatch.acquire(prov, use_name, deadline_s)     # governor: bounds per-lane in-flight (fills, never swarms)
        except Exception as e:
            return i, {"text": None, "lane": lane, "use_name": use_name, "billed": False,
                       "error": f"dispatch: {str(e)[:60]}"}
        try:
            calls.set_context(intent=intent)          # tag this worker thread's calls with the intent (attribution)
            r = adapters.call(model, task, system=system, reasoning=reasoning,   # sig=intent → the OUTPUT budget is
                              sig=intent, timeout_s=deadline_s)                  # this call-class's measured p99, and
            # (receipt suppressed via set_context above, not the context manager)  each reply feeds that measurement
        except Exception as e:
            return i, {"text": None, "lane": lane, "use_name": use_name, "billed": False, "error": str(e)[:80]}
        finally:
            dispatch.release(prov, use_name)
        r = r if isinstance(r, dict) else {}
        served = r.get("executor") or ("api-fallback" if r.get("cost") else lane)
        return i, {"text": (r.get("text") or None), "lane": served, "use_name": use_name,
                   "billed": bool(r.get("cost")), "error": r.get("error")}

    # CHUNKED: bound how many futures are in flight at once, and make each chunk's results durable before the next.
    for c0 in range(0, len(todo), max(1, int(chunk_size))):
        chunk = todo[c0:c0 + max(1, int(chunk_size))]
        with _cf.ThreadPoolExecutor(max_workers=max(1, n)) as ex:
            for fut in _cf.as_completed([ex.submit(_run_task_on_lane, i, tasks[i]) for i in chunk]):
                i, res = fut.result()
                results[i] = res
                _checkpoint(i, res)
    return results


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
        _allow = config._cfg_get("advisor", "bandit_intents", None) or []
        if (str(config._cfg_get("advisor", "lane_bandit", False)).strip().lower() in ("1", "true", "yes", "on")
                and not str(intent).startswith(_META) and intent in _allow):
            try:
                from . import lane_bandit, lane_catalog
                _prim_lane = adapters._LANES.get(adapters.provider_for(model), (None,))[0]
                _arms = [a for a in lane_catalog.arms(config._cfg_get("advisor", "delegate_lanes", None))
                         if a[0] != _prim_lane]
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
    # rank the acceptable, available substitutes by plan utilisation — LEAST-used first (fill the emptiest plan)
    ranked = []
    for spec in subs:
        slane = adapters._LANES.get(spec.split(":", 1)[0], (None,))[0]
        if not slane or slane == primary_lane or adapters._lane_cooling(slane):   # skip unknown/self/cooling lanes
            continue
        _su = (util.get(slane) or {}).get("utilization")
        ranked.append((float(_su) if _su is not None else 0.0, spec, slane))
    if not ranked:
        return None, "no available substitute lane right now (all cooling, or same plan as primary)"
    ranked.sort(key=lambda t: t[0])
    su, spec, slane = ranked[0]
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
