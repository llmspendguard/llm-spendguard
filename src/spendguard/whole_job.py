"""#2 the WHOLE-JOB contract: the caller hands spendguard a whole SET of jobs + a GOAL, and spendguard decides the
plan (batch vs lane vs metered, capability-matched per job's schema), enforces the budget, executes, and returns —
so the caller never hand-tunes metered_only / batch / lanes / model per call (the "stupid param issues"). This is the
ergonomic front door over the machinery that already exists: route_economics.route_report (the true-$ batch-vs-lane-vs
-metered pick), bulk_delegate (the realtime/lane fan, with #1 capability-aware auto-route built in), and
submit_chat_tasks / callio.collect_chat_tasks (the OpenAI Batch-API legs). It ADDS the simple contract, the per-intent
economic method choice under the goal, and the estimate-first budget gate — it does not reimplement execution.

CONTRACT
  run_jobs(jobs, goal) -> {results, pending, plan, receipt}
    jobs: [ {prompt, intent, schema?, system?, id?} ]   — intent is the routing + attribution key (required per job)
    goal: { budget_usd?, urgency?, quality_bar? }
        urgency: "auto" (default; route_report's true-$ pick decides batch vs realtime) | "realtime" (never batch —
                 results now) | "batch" (prefer the Batch API where eligible — ~half cost, async)
        budget_usd: an ESTIMATE-FIRST hard gate — the whole set is priced BEFORE any spend and REFUSED if the
                    estimate exceeds it, OR if any group cannot be estimated (fail-CLOSED: an unknown cost under a
                    budget is a refusal, never a silent run), honouring the API-spend protocol.
        quality_bar: when set, each group runs reasoning='best-value' for its intent (spendguard picks the cheapest
                     model whose measured quality holds), instead of the caller pinning a model.
    returns:
        results: {job_id: {text, error, cost, model, lane, ...}}   — the jobs that ran SYNCHRONOUSLY (realtime/lane)
        pending: [ {batch_id, intent, model, job_ids} ]            — batch groups submitted async; settle with
                                                                      collect_jobs(pending) (~24h window, OpenAI)
        plan:    [ {intent, n, method, est_usd, why} ]             — the per-group decision, auditable
        receipt: {est_usd, groups, ran, pending, batch_failures, refused}  — the whole-set summary

Estimate-first, fail-closed on unknown cost, no silent loss, decisions are economics (not regex) — the doctrines apply.
"""
from . import route_economics


_REALTIME, _BATCH = "realtime", "batch"


def _group_by_intent(jobs):
    """Jobs keyed by intent (the routing/attribution unit route_report + bulk_delegate operate on). A job with no
    intent is an ERROR surfaced to the caller, never silently bucketed — attribution is the core mission."""
    groups, bad = {}, []
    for i, j in enumerate(jobs or []):
        jid = str(j.get("id", "job-%d" % i))
        intent = j.get("intent")
        if not intent:
            bad.append(jid)
            continue
        groups.setdefault(intent, []).append({**j, "id": jid})
    return groups, bad


def _method_for(intent, n, goal, in_tok, out_tok):
    """The execution method for one intent-group — ECONOMICS + the goal's urgency, never a keyword guess. Returns
    (method, est_usd, why). est_usd is None when route_report cannot price the group — an UNKNOWN cost, which the
    budget gate treats as fail-closed (a refusal under a budget), NEVER as $0. urgency forces the axis the caller
    cares about; 'auto' defers to route_report's TRUE-$ recommendation (lane_only/combo → realtime, batch_only →
    batch). $0 — route_report reads measured state only."""
    urgency = (goal or {}).get("urgency", "auto")
    try:
        rep = route_economics.route_report(intent, n, in_tok=in_tok, out_tok=out_tok)
        rec = (rep or {}).get("recommend") or {}
        est = rec.get("usd")
        est = float(est) if est is not None else None
        rec_path = rec.get("path") or "lane_only"
    except Exception as e:
        # UNKNOWN cost, not $0: est=None so the budget gate fails CLOSED (refuses under a budget) instead of letting
        # an unpriced group slip through the gate. The METHOD still defaults to realtime so an ungated caller (no
        # budget) can still run — but the cost is honestly reported as unknown, never invented as free.
        return _REALTIME, None, "route_report could not price this group (%s) — cost UNKNOWN" % (str(e)[:50])
    if urgency == _REALTIME:
        return _REALTIME, est, "urgency=realtime (forced)"
    if urgency == _BATCH:
        return _BATCH, est, "urgency=batch (forced; falls back to realtime if not batch-eligible)"
    if rec_path == "batch_only":
        return _BATCH, est, "auto → route_report recommends batch_only"
    return _REALTIME, est, "auto → route_report recommends %s" % rec_path


def plan_jobs(jobs, goal=None):
    """The PLAN for a whole job set — the per-intent method + estimate, and the whole-set estimate — with NO execution
    and NO spend. run_jobs calls this first (estimate-first) so the budget gate can refuse before a cent is spent; a
    caller can also call it alone to preview what spendguard would do. `est_usd` is None (unknown) if ANY group could
    not be priced; `unpriced` names those groups. Returns {plan, est_usd, unpriced, groups, bad_jobs}."""
    groups, bad = _group_by_intent(jobs)
    plan, total, unpriced = [], 0.0, []
    for intent, items in groups.items():
        n = len(items)
        # per-task input size from THIS set (chars/4, the neutral basis route_report defaults to); out_tok left to
        # route_report (expected_output for the intent) so the estimate reflects both the set and the intent's history.
        in_tok = max(1, sum(len(str(j.get("prompt", ""))) for j in items) // max(1, n) // 4)
        method, est, why = _method_for(intent, n, goal, in_tok, None)
        if est is None:
            unpriced.append(intent)
        else:
            total += est
        plan.append({"intent": intent, "n": n, "method": method,
                     "est_usd": (round(est, 6) if est is not None else None), "why": why})
    return {"plan": plan, "est_usd": (round(total, 6) if not unpriced else None),
            "priced_est_usd": round(total, 6), "unpriced": unpriced, "groups": groups, "bad_jobs": bad}


def run_jobs(jobs, goal=None, checkpoint=None):
    """Plan + execute a whole job set under a goal, returning ready results + async batch handles + the plan/receipt.
    Estimate-first, fail-CLOSED: the whole set is priced BEFORE any spend and REFUSED if the estimate exceeds
    goal.budget_usd OR if any group is unpriced under a budget. `checkpoint` (a jsonl path) makes the realtime results
    durable (crash-resume via bulk_delegate); when None a per-run checkpoint is created under the spendguard home so a
    crash never loses a completed result (the chunk-never-single-shot rule)."""
    goal = goal or {}
    planned = plan_jobs(jobs, goal)
    # Every refusal carries a STRUCTURED `refused_code` (a stable enum a consumer routes on) alongside the human
    # `refused` message — so no caller has to parse the prose to know WHY it was refused.
    if planned["bad_jobs"]:
        return {"results": {}, "pending": [], "plan": planned["plan"],
                "receipt": {"refused_code": "missing_intent",
                            "refused": "jobs missing an intent: %s" % ", ".join(planned["bad_jobs"][:10]),
                            "est_usd": planned["priced_est_usd"], "unpriced": planned["unpriced"]}}
    budget = goal.get("budget_usd")
    if budget is not None:
        if planned["unpriced"]:
            # FAIL-CLOSED: a budget was set but a group's cost is UNKNOWN — refuse rather than spend blind.
            return {"results": {}, "pending": [], "plan": planned["plan"],
                    "receipt": {"refused_code": "unpriced_under_budget",
                                "refused": "cannot price group(s) %s under budget_usd $%.4f — refusing (no spend)"
                                % (", ".join(planned["unpriced"]), float(budget)),
                                "est_usd": None, "unpriced": planned["unpriced"]}}
        if planned["priced_est_usd"] > float(budget):
            return {"results": {}, "pending": [], "plan": planned["plan"],
                    "receipt": {"refused_code": "budget_exceeded",
                                "refused": "estimate $%.4f exceeds budget_usd $%.4f (no spend)"
                                % (planned["priced_est_usd"], float(budget)), "est_usd": planned["priced_est_usd"]}}
    checkpoint = checkpoint or _default_checkpoint()
    results, pending, batch_failures, ran = {}, [], [], 0
    remaining = (float(budget) if budget is not None else None)
    for entry in planned["plan"]:
        intent = entry["intent"]
        items = planned["groups"][intent]
        did_realtime = False
        if entry["method"] == _BATCH:
            pend = _run_batch_group(intent, items, goal, remaining)
            if pend.get("batch_id"):
                pending.append(pend)
            elif pend.get("eligible"):
                # batch was ELIGIBLE but the SUBMISSION FAILED (e.g. Batch API outage) — record the failure (never
                # discard it) AND still get the caller a result by running the group realtime.
                batch_failures.append({"intent": intent, "error": pend.get("error"), "n": len(items)})
                did_realtime = True
            else:
                did_realtime = True                   # not batch-eligible (non-OpenAI) → realtime is the correct path
        if entry["method"] != _BATCH or did_realtime:
            _res = _run_realtime_group(intent, items, goal, remaining, checkpoint)
            results.update(_res)
            ran += len(_res)
        if remaining is not None and entry["est_usd"] is not None:
            remaining = max(0.0, remaining - entry["est_usd"])
    return {"results": results, "pending": pending, "plan": planned["plan"],
            "receipt": {"est_usd": planned["priced_est_usd"], "groups": len(planned["plan"]), "ran": ran,
                        "pending": len(pending), "batch_failures": batch_failures,
                        "refused_code": None, "refused": None, "checkpoint": checkpoint}}


def _default_checkpoint():
    """A per-run durable jsonl under the spendguard home so a crash mid-run RESUMES instead of losing completed
    results (the chunk-never-single-shot rule) — even when the caller passes no checkpoint of their own."""
    import datetime
    import os
    from . import config
    d = os.path.join(getattr(config, "HOME", os.path.expanduser("~/.spendguard")), "whole_job")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "run-%s.jsonl" % datetime.datetime.now().strftime("%Y%m%dT%H%M%S%f"))


def _run_realtime_group(intent, items, goal, budget_remaining, checkpoint):
    """Execute one intent-group on the realtime/lane fan (bulk_delegate — #1 capability-aware auto-route is applied
    per task inside it, and `checkpoint` makes each completed result durable). Returns {job_id: row}. One schema per
    group (a group shares an intent + contract); quality_bar → reasoning='best-value' so spendguard picks the cheapest
    model that holds quality."""
    from . import lane_balance
    schema = next((j.get("schema") for j in items if j.get("schema") is not None), None)
    system = next((j.get("system") for j in items if j.get("system") is not None), None)
    reasoning = "best-value" if goal.get("quality_bar") else None
    by_key = {j["id"]: j for j in items}
    keyed = lane_balance.bulk_delegate(
        list(by_key.keys()), intent=intent, system=system, schema=schema, reasoning=reasoning,
        prompt_for=lambda jid: by_key[jid]["prompt"], task_key=lambda jid: jid,
        return_keyed=True, budget_usd=budget_remaining, checkpoint=checkpoint)
    return keyed if isinstance(keyed, dict) else {}


def _run_batch_group(intent, items, goal, budget_remaining):
    """Submit one intent-group to the OpenAI Batch API (async, ~half cost). Returns {batch_id, intent, model, job_ids}
    on success; {batch_id: None, eligible: False} when the group is NOT batch-eligible (a non-OpenAI model — realtime
    is then the correct path, not a failure); or {batch_id: None, eligible: True, error} when it IS eligible but the
    submission FAILED (surfaced by run_jobs, never discarded). Eligibility is decided STRUCTURALLY (the provider), not
    by parsing an error string. The cap is the remaining budget (submit_chat_tasks estimates→caps→submits through the
    ONE guarded chokepoint)."""
    from . import adapters, config, submit as _submit
    model = config.advisor_model()                    # v1: the account's advisor model (an OpenAI id rides the Batch API)
    if adapters.provider_for(model) != "openai":      # structural eligibility — the Batch API serves only OpenAI ids
        return {"batch_id": None, "eligible": False, "intent": intent}
    schema = next((j.get("schema") for j in items if j.get("schema") is not None), None)
    tasks = []
    for j in items:
        t = {"custom_id": j["id"], "content": j["prompt"]}
        if j.get("schema") is not None:
            t["schema"] = j["schema"]
        if j.get("system") is not None:
            t["system"] = j["system"]
        tasks.append(t)
    r = _submit.submit_chat_tasks(tasks, model, schema=schema, intent=intent, cap_dollars=budget_remaining)
    if r.get("error") or not r.get("batch_id"):
        return {"batch_id": None, "eligible": True, "intent": intent, "error": r.get("error") or "no batch_id returned"}
    return {"batch_id": r["batch_id"], "intent": intent, "model": model, "job_ids": [j["id"] for j in items]}


def collect_jobs(pending):
    """Settle the async BATCH handles from a prior run_jobs (the OpenAI Batch API's collect twin). Returns
    {job_id: row}, keying each succeeded/failed job by its custom_id (== the job id) from callio.collect_chat_tasks's
    STRUCTURED return ({results, failed, not_ready, anomalies, ...}) — a succeeded row {text}, a failed row {error},
    both under the job id; a batch still running is reported under its batch_id as {status:'pending'}, unkeyable
    anomalies under '<batch_id>-anomalies', and a handle with no batch_id under 'handle-<i>' — so NOTHING is dropped
    (each row is a unit of work). $ is booked by callio.collect_chat_tasks under each handle's intent (attribution
    preserved). A DELIBERATE stop (spend refusal / deadline) propagates — never downgraded to 'keep going'."""
    from . import callio, gate
    out = {}
    for idx, h in enumerate(pending or []):
        bid = h.get("batch_id")
        intent = h.get("intent")
        if not bid:
            out["handle-%d" % idx] = {"kind": "no_batch_id", "error": "handle has no batch_id",
                                      "intent": intent, "batch_error": h.get("error")}
            continue
        try:
            res = callio.collect_chat_tasks(bid, intent, h.get("model"))
        except Exception as e:
            if gate.is_deliberate_stop(e):
                raise                                 # a spend refusal / deadline HALTS collection, never continues past it
            out[bid] = {"error": "collect failed: %s" % (str(e)[:80]), "intent": intent}
            continue
        res = res if isinstance(res, dict) else {}
        for cid, text in (res.get("results") or {}).items():
            out[cid] = {"text": text, "batch_id": bid, "intent": intent}
        for cid, err in (res.get("failed") or {}).items():
            out[cid] = {"error": err, "batch_id": bid, "intent": intent}
        if res.get("not_ready"):                       # this batch's output file isn't ready yet — poll again later
            out[bid] = {"status": "pending", "intent": intent}
        if res.get("anomalies"):                       # rows with a missing/unparseable custom_id — surfaced, not dropped
            out["%s-anomalies" % bid] = {"kind": "anomalies", "anomalies": res["anomalies"], "intent": intent}
    return out
