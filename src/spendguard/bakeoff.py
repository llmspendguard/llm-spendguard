"""spendguard bakeoff — the EXPLORE half of the model-advisor: measure cost×quality for models you have NOT
used on a job-type, so advise/recommend can rank the whole universe, not just your history.

advise/recommend rank from RECORDED evidence, so a model you have never run is invisible to them. A bakeoff
fills that in the only honest way there is — it RUNS a slate of candidate models on a SAMPLE of the intent's
real tasks, JUDGES each output for quality, and RECORDS the cost + good/bad per (intent, model) into the same
`calls` corpus advise reads. After a bakeoff, the untried models have a $/good and fall into the ranking.

Inside spendguard's rails, always: ESTIMATE-FIRST (run=False is a zero-spend projection + the plan; a
`budget_usd` refuses before spending), the fan-out prefers $0 subscription LANES and is metered by the gate
otherwise, and the quality JUDGE is an LLM (never a keyword rule) — the same judge advisor.reconstruct_advisor_history uses.

  bakeoff('code-review', candidates=['openai:gpt-5.5','deepseek:deepseek-v4-flash'], sample_n=5)  # estimate
  bakeoff('code-review', candidates=[...], sample_n=5, run=True, budget_usd=0.50)                 # measure
"""

from .capability_cli_parsing import add_registered_cli_argument
import json

from . import calls, config, pricing, callio
from .submit import _count_tokens

_BAKEOFF_OUT_EST = 500     # per-call OUTPUT tokens for the PRE-FLIGHT estimate only — a budget guard, not a cap;
#                            the real call floors output at the model max and bills ACTUAL tokens. Named, not inline.
_JUDGE_OUT = 20            # room for the structured verdict {"good": true|false}; still tiny, keeps the judge cheap
_JUDGE_CONF = 0.9          # confidence stamped on a bakeoff quality label — an LLM judge on a fresh output, not a guess
# The judge is an LLM (agentic decision) that returns a STRUCTURED boolean — never a free-text verdict a string
# rule then interprets. A schema-forced {"good": bool} removes the "GOOD enough … BAD" misread class entirely: the
# model decides, the code reads a typed field, and anything that is not a clean boolean is treated as UNLABELED.
_JUDGE_SYS = ("You evaluate whether an LLM OUTPUT is a usable, CORRECT result for its PROMPT. Return ONLY JSON: "
              '{"good": true} when the output is a usable, correct result; {"good": false} otherwise. No prose.')
_JUDGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["good"],
                 "properties": {"good": {"type": "boolean"}}}


def _task_parts(task):
    """A bakeoff task is EITHER a plain prompt STRING (the text-only surface, unchanged) OR a dict
    {"prompt": str, "images": [path | data-url, …]} for a VISION task. Returns (prompt_text, images) where
    `images` is None for a text task or the list of image refs for a vision task — the SAME shape
    adapters.call(images=…) accepts. Backward-compatible: a string yields (string, None), so every text path is
    untouched."""
    if isinstance(task, dict):
        imgs = task.get("images") or None
        return str(task.get("prompt") or ""), imgs
    return str(task), None


def _task_sample_key(task):
    """The stable content KEY hashed into measurement.item_id — incorporates the image reference(s) so two captions
    of DIFFERENT images are distinct samples (a text-only task keys exactly as before: the bare prompt string)."""
    txt, imgs = _task_parts(task)
    if isinstance(task, dict) and any(task.get(k) for k in ("system", "schema", "max_tokens")):
        return json.dumps(task, sort_keys=True, ensure_ascii=False)
    if not imgs:
        return txt
    return txt + "\n\x00images\x00\n" + "\n".join(str(i) for i in imgs)


def _sample_prompts(intent, n, include_shape=False):
    """Up to `n` DISTINCT real prompts recorded for this intent (the tasks to replay on the candidates). Returns
    [] ONLY when the intent genuinely has no recorded prompts — the caller must then pass prompts explicitly (a
    bakeoff cannot invent representative work). Reads callio's OWN connection: the call_io table lives in
    config.db_path(), NOT a HOME/call_io.sqlite path — the old getattr(callio,'DB_PATH')-or-that-path read an empty
    WRONG file, so auto-sample silently returned nothing and every bakeoff demanded explicit prompts. A real DB
    failure PROPAGATES (it is not masked as 'no prompts' — that fail-open hid the wrong-path bug for exactly this
    reason); the table is auto-created, so an empty corpus is a clean [] while a broken db surfaces."""
    # include_shape keeps system/schema/ceiling/effort together for replay consumers.
    # The default text view remains compatible with display and prompt-only callers.
    with callio.config.ledger_op('callio', callio._ensure_callio_schema) as con:
        # EXCLUDE truncated rows: a prompt CUT at the capture cap is a different task, so replaying it measures
        # something other than the work it claims to (the 800-char silent-truncation failure). Only full-fidelity
        # bodies feed a bakeoff/titration; an intent whose corpus is entirely truncated returns [] → the caller
        # refuses cleanly ("no sample tasks — re-fetch with a higher callio.snip_chars"), never a partial replay.
        replay_columns = "prompt, system, req_schema, req_max_tokens, effort" if include_shape else "prompt"
        rows = con.execute("SELECT DISTINCT " + replay_columns + " FROM call_io "
                           "WHERE intent IS ? AND prompt IS NOT NULL AND length(prompt) > 0 "
                           "AND COALESCE(truncated,0) = 0 AND shape_complete = 1 LIMIT ?", (intent, int(n))).fetchall()
    if not include_shape:
        return [r[0] for r in rows]
    # Decode the entire stored contract. Invalid schema data refuses sampling via JSONDecodeError;
    # it must never silently turn a strict-output task into an unconstrained prompt.
    replay_tasks = []
    for prompt, system, schema, max_tokens, effort in rows:
        decoded_schema = json.loads(schema) if schema else None
        # Batch recovery stores OpenAI's response_format envelope; live capture stores the
        # JSON Schema itself. Parse the envelope rather than treating it as the schema.
        if isinstance(decoded_schema, dict) and decoded_schema.get("type") == "json_schema":
            decoded_schema = decoded_schema["json_schema"]["schema"]
        elif isinstance(decoded_schema, dict) and decoded_schema.get("type") == "json_object":
            decoded_schema = {"type": "object"}
        replay_tasks.append(dict(prompt=prompt, system=system or None, schema=decoded_schema,
                                 max_tokens=max_tokens or None, effort=effort))
    return replay_tasks


def _judge_one(prompt, output, judge_model, images=None):
    """Is this (prompt, output) a good result? Decided by an LLM (the quality signal), returned as a STRUCTURED
    boolean — not a free-text verdict a string rule then interprets. Returns True (good) / False (bad) / None
    (the judge failed or gave no clean boolean → UNLABELED, never guessed). For a VISION task the judge RECEIVES
    the image (images=…): a caption's faithfulness CANNOT be judged from text alone, so a judge that scored it
    without seeing the image would be measuring nothing. images is passed ONLY when present — a text task is
    byte-for-byte the prior call."""
    from . import adapters, advisor, calls
    # WHOLE evidence: the judge scores the candidate's FULL output — a [:4000] slice would rate only its head,
    # evidence-truncating the very thing being judged. no_substitution PINS the judge so every candidate is rated
    # by the SAME ruler, never a lane-swapped one (a judge that varies per candidate is not a comparable number).
    # META CONTEXT: this is spendguard's OWN ruler call, so it must be attributed to spendguard:* and meta-capped —
    # NOT the caller's workload intent. Passing sig="spendguard:…" is not enough: inside a caller's ambient
    # `with calls.context(intent=…)` (a bakeoff runs under one), adapters.call does NOT override the ambient intent, so
    # the judge's $ would land under the caller's intent (the warden 2026-09-25 attribution split). An explicit nested
    # context forces it, exactly as requirement_judge._meta_call already does.
    with calls.context(intent="spendguard:bakeoff-judge"):
        r = adapters.call(judge_model, advisor._judge_prompt(prompt, output or ""),
                          max_tokens=_JUDGE_OUT, system=_JUDGE_SYS, schema=_JUDGE_SCHEMA,
                          sig="spendguard:bakeoff-judge", no_substitution=True,
                          **({"images": images} if images else {}))
    if r.get("error") or not r.get("text"):
        return None
    # The adapter's OWN fence-tolerant decode (r['parsed']) — a bare json.loads here choked on a $0 lane's
    # ```json fence, so the generic judge silently labeled 0/N on lane-served candidates.
    j = adapters.structured_reply(r)
    return bool(j["good"]) if isinstance(j, dict) and isinstance(j.get("good"), bool) else None


def _plan(intent, candidates, prompts, judge_model, efforts, requirement_aware=False, adjudicator_model=None):
    """The zero-spend plan + cost estimate: every candidate runs every sample prompt AT EVERY EFFORT, then each
    output is judged. Coarse and slightly high on purpose (a budget guard, not the ledger): a per-prompt input
    count, a fixed output estimate, multiplied by the number of effort arms. Returns
    (estimate_usd, per_candidate_detail, n_runs, n_judge). `efforts` is the ordinal ladder ([None] = one arm at
    the model's default effort); the estimate scales linearly with it so a wide sweep can never surprise-spend.
    `requirement_aware` adds the two-tier judge's extra cost as a CEILING: requirement extraction once per prompt +
    a worst-case one opus adjudication per judge call (real runs escalate only when the screen is unsure, so actual
    lands under this — a budget guard is meant to be high, never low). A VISION task (dict with images) adds the
    IMAGE input tokens — via adapters._image_input_tokens, the provider-aware corrected estimator, never a hardcoded
    per-image number — to EVERY call that sees the image: the candidate run, the screen/generic judge, and the
    adjudicator (extraction reads the prompt TEXT only). A text-only slate is priced exactly as before."""
    from . import gate, adapters
    _stop = gate.deliberate_stop_types()               # SpendGateRefused / DispatchTimeout must PROPAGATE — never be
    n_eff = max(1, len(efforts))                        # swallowed into a false-low estimate by the broad guards below
    tasks = [_task_parts(p) for p in prompts]           # (prompt_text, images_or_None) — a string yields (string, None)
    loaded = [([adapters._load_image(i) for i in imgs] if imgs else None) for _txt, imgs in tasks]  # dims for the token est ($0 read)
    contract_inputs = []
    for task, (txt, _im) in zip(prompts, tasks):
        contract = ({k: task[k] for k in ("system", "schema", "max_tokens") if task.get(k) is not None}
                    if isinstance(task, dict) else {})
        contract_inputs.append(json.dumps({"prompt": txt, **contract}, ensure_ascii=False) if contract else txt)
    in_text = [_count_tokens(txt, candidates[0] if candidates else "gpt-5.5") for txt in contract_inputs]

    def _img_tok(model, i):                             # provider-aware IMAGE input tokens for task i on `model` (0 = text task)
        return (adapters._image_input_tokens(loaded[i], adapters.provider_for(model), model.split(":", 1)[-1])
                if loaded[i] else 0)

    total, detail = 0.0, {}
    for c in candidates:
        c_cost = 0.0
        for i in range(len(tasks)):
            try:
                c_cost += pricing.realtime_cost(c, in_text[i] + _img_tok(c, i), _BAKEOFF_OUT_EST)
            except _stop:
                raise
            except Exception:
                c_cost = None
                break
        detail[c] = (c_cost * n_eff) if c_cost is not None else None   # one run per (prompt, EFFORT)
        total += (detail[c] or 0.0)
    n_judge = len(candidates) * len(prompts) * n_eff                    # one small judge call per (candidate, prompt, effort)
    try:                                                               # the generic/screen judge SEES the image on a vision task
        judge_in = sum(_count_tokens(contract_inputs[i], judge_model) + _img_tok(judge_model, i)
                       for i, (txt, _im) in enumerate(tasks)) * len(candidates) * n_eff
        total += pricing.realtime_cost(judge_model, judge_in, _JUDGE_OUT * n_judge)
    except _stop:
        raise
    except Exception:
        pass
    if requirement_aware:                                              # extraction (once/prompt, TEXT) + opus-adjudication CEILING
        from . import requirement_judge
        ex, adj = config.advisor_model(), (adjudicator_model or config.advisor_adjudicator_model())
        try:
            total += pricing.realtime_cost(ex, sum(_count_tokens(txt, ex) for txt in contract_inputs),
                                           requirement_judge._EXTRACT_OUT * len(prompts))
            adj_in = sum(_count_tokens(contract_inputs[i], adj) + _img_tok(adj, i)     # the adjudicator SEES the image too
                         for i, (txt, _im) in enumerate(tasks)) * len(candidates) * n_eff
            total += pricing.realtime_cost(adj, adj_in, requirement_judge._JUDGE_OUT * n_judge)
        except _stop:
            raise
        except Exception:
            pass
    return total, detail, len(candidates) * len(prompts) * n_eff, n_judge


def bakeoff(intent, candidates=None, prompts=None, sample_n=5, run=False, budget_usd=None,
            judge_model=None, parent_reading=None, efforts=None, requirement_aware=False, adjudicator_model=None,
            temperature=None, top_p=None, seed=None):
    """Measure cost×quality for `candidates` on a SAMPLE of `intent`'s real tasks, judge each output, and (run=True)
    record it so advise/recommend rank the candidates. `candidates` = ['vendor:model', …] (required — the slate to
    test). `prompts` overrides the auto-sample (from the intent's recorded prompts). ESTIMATE-FIRST: run=False
    returns the plan + $ estimate and spends nothing; run=True executes (refusing if the estimate exceeds
    `budget_usd`). `judge_model` PINS a specific judge (else config.advisor_judge_model()) — this is how a
    measurement rerun keeps the SAME ruler for a comparable number; `parent_reading` links the emitted receipt to
    the reading it re-runs (a lineage).

    `efforts` sweeps the REASONING axis: an ordinal ladder like ['minimal','low','medium','high'] runs each
    candidate at each effort as a separate arm, so the bakeoff explores not just models but (model, EFFORT) points.
    Omitted (or []) → one arm per model at its default effort (the prior behaviour). Each arm's rows land per-(intent,
    model, effort) in the `calls` corpus (that IS the learning advise reads back — there is no second store). The
    per-(intent, model) effort VERDICT (cheapest effort that holds, agentic) is `effort_titration`'s job, not this
    broad explorer's.

    `requirement_aware=True` swaps the generic good/bad judge for the two-tier REQUIREMENT judge (requirement_judge):
    it extracts each prompt's own success criteria, a cheap screen model rules met/not, and an OPUS `adjudicator_model`
    (default config.advisor_adjudicator_model()) rules the calls the screen flags as not-confident. The applied
    criteria are stamped into the measurement receipt's rubric (reusable context). The estimate accounts for it as a
    ceiling. Returns a structured dict."""
    _judge_pinned = judge_model is not None
    judge_model = judge_model or config.advisor_judge_model()
    candidates = [c.strip() for c in (candidates or []) if c and c.strip()]
    if not candidates:
        return dict(intent=intent, error="no candidates — pass candidates=['vendor:model', …] (the slate to test). "
                    "spendguard_recommend suggests a slate from your priced catalogue.")
    prompts = list(prompts) if prompts else _sample_prompts(intent, sample_n, include_shape=True)
    if not prompts:
        return dict(intent=intent, candidates=candidates, error="no sample tasks — this intent has no recorded "
                    "prompts to replay. Pass prompts=[…] with a few representative tasks.")
    # The effort ladder to sweep. [None] means "one arm at the model's default effort" (send no reasoning_effort).
    efforts_list = [str(e).strip() for e in (efforts or []) if e and str(e).strip()] or [None]

    est, detail, n_runs, n_judge = _plan(intent, candidates, prompts, judge_model, efforts_list,
                                         requirement_aware=requirement_aware, adjudicator_model=adjudicator_model)
    unpriced = [c for c, v in detail.items() if v is None]
    if not run:
        return dict(intent=intent, candidates=candidates, sample=len(prompts), runs=n_runs, judge_calls=n_judge,
                    efforts=efforts_list, estimate_usd=round(est, 5), per_candidate=detail, unpriced=unpriced,
                    estimate_only=True,
                    note=f"estimate only (~${est:.4f} + judging) over {len(efforts_list)} effort arm(s) per model; "
                         "call with run=True to measure. $0 subscription lanes are used where available.")
    if budget_usd is not None and est > float(budget_usd):
        return dict(intent=intent, candidates=candidates, estimate_usd=round(est, 5), refused=True,
                    note=f"estimate ~${est:.4f} exceeds budget_usd ${float(budget_usd):.4f} — not run. Raise budget_usd "
                         "or shrink the slate/sample/efforts.")

    with config.scoped_intent_cap((intent, "spendguard:bakeoff-judge", "spendguard:requirement-extract", "spendguard:requirement-screen", "spendguard:requirement-adjudicate"), budget_usd):
        from . import adapters
        results = {}
        req_seen = []                                                   # requirement-aware: the union of criteria the judge applied (for the receipt rubric)
        for c in candidates:
            for eff in efforts_list:                                    # the REASONING axis — one arm per (model, effort)
                arm = c + ("@" + eff if eff else "")
                n_good = n_lab = n_run = n_err = 0
                spent, last_error = 0.0, None
                for p in prompts:
                    txt, imgs = _task_parts(p)
                    replay_contract = ({k: p[k] for k in ("system", "schema", "max_tokens")
                                        if p.get(k) is not None} if isinstance(p, dict) else {})
                    replay_effort = eff if eff is not None else (p.get("effort") if isinstance(p, dict) else None)
                    judge_task = json.dumps({"prompt": txt, **replay_contract}, ensure_ascii=False) if replay_contract else txt
                    # no_substitution PINS the candidate: a bakeoff MEASURES this exact model, so the lane/bandit must
                    # never swap it — else the arm records ANOTHER model's cost×quality under c's name (the corruption
                    # that feeds advise/recommend). Still $0 on c's OWN lane where available (pinning suppresses
                    # cross-model substitution, not same-provider lane use). images= is passed ONLY for a vision task,
                    # so a text run is byte-for-byte the prior call (a vision call skips the lane by construction).
                    # DETERMINISM (optional): when the caller passes temperature/top_p/seed, run each arm as a MEASUREMENT
                    # with those knobs — one deterministic pass per arm instead of n replicates to separate the effect from
                    # run noise. This FORCES the metered API (a $0 lane has no knob channel), so it is opt-in per the caller.
                    # Unset (the default) → arms stay on their $0 lane where available (the production-settings behaviour).
                    _det = {k: v for k, v in (("temperature", temperature), ("top_p", top_p), ("seed", seed)) if v is not None}
                    import uuid as _uuid
                    _measurement_call_id = _uuid.uuid4().hex
                    _prior_call_context = dict(calls.current() or {})
                    calls.set_context(intent=intent, who="bakeoff", root_call=_measurement_call_id, attempt=1,
                                      requested_model=c, measurement=True)
                    try:
                        r = adapters.call(c, txt, sig=intent, timeout_s=120, reasoning=replay_effort, no_substitution=True,
                                          **replay_contract,
                                          **({"measurement": True} if _det else {}), **_det,
                                          **({"images": imgs} if imgs else {}))
                    finally:
                        calls._local.ctx = _prior_call_context
                    if r.get("error"):
                        n_err += 1                                      # a dropped run is COUNTED + surfaced, never silent —
                        last_error = (r.get("error") or "")[:140]      # a candidate/effort that fails every prompt is visible,
                        continue                                       # not read as a clean zero-run bakeoff
                    if not (r.get("text") or "").strip():              # a SUCCESSFUL call that produced NO usable text is a
                        n_err += 1                                      # FAILED arm, not a clean run — count + skip, never hand
                        last_error = "empty reply (no text)"           # an empty output to the judge as if it were an answer
                        continue
                    n_run += 1
                    cost = float(r.get("cost") or 0.0)
                    spent += cost
                    # THE JUDGE MUST NOT ABORT THE SLATE. A judge that RAISES (an unparseable verdict, a transport error, a
                    # requirement_judge exception) would otherwise crash the whole bakeoff mid-loop and discard every arm
                    # already measured. Contain it: the run is counted as UNLABELED (verdict=None), the error is surfaced in
                    # last_error, and the loop continues — the same counted-and-skipped discipline a failed ARM gets.
                    try:
                        if requirement_aware:                           # judge by the PROMPT'S OWN requirements, two-tier (screen→opus)
                            from . import requirement_judge
                            _v = requirement_judge.judge_requirements(judge_task, r.get("text"), screen_model=judge_model,
                                                                      adjudicator_model=adjudicator_model,
                                                                      **({"images": imgs} if imgs else {}))
                            verdict = _v.get("good")                    # same True/False/None contract as _judge_one
                            spent += float(_v.get("cost") or 0.0)      # the judge's own meta-cost, folded into the arm's spend
                            for _rq in (_v.get("requirements") or []): # collect the applied rubric (deduped) for the receipt
                                if _rq not in req_seen:
                                    req_seen.append(_rq)
                        else:
                            verdict = _judge_one(judge_task, r.get("text"), judge_model,
                                                 **({"images": imgs} if imgs else {}))  # generic LLM judge — the quality signal
                    except Exception as _je:
                        from . import gate
                        if gate.is_deliberate_stop(_je):
                            raise
                        verdict = None                                  # UNLABELED, not a crash — the arm's output still counts as a run
                        last_error = f"judge error: {type(_je).__name__}: {str(_je)[:100]}"
                    q = None if verdict is None else ("good" if verdict else "bad")
                    if q is not None:
                        n_lab += 1
                        n_good += 1 if verdict else 0
                    # RECORD per (intent, model, EFFORT) into the corpus advise/best_value read — this is the learning.
                    _served_model = r.get("model") or c.split(":", 1)[-1]
                    calls.finalize_bakeoff_measurement(
                        _measurement_call_id, r.get("provider") or adapters.provider_for(_served_model),
                        c, _served_model, cost, r.get("in_tok"), r.get("out_tok"),
                        intent, q, r.get("chosen_effort", replay_effort),
                        r.get("resolved_lane") or r.get("executor") or "api",
                        r.get("resolution") or r.get("substitution"))
                results[arm] = dict(model=c, effort=eff, runs=n_run, failed=n_err, last_error=last_error,
                                    labeled=n_lab, good=n_good, good_rate=(n_good / n_lab if n_lab else None),
                                    spent=round(spent, 6), per_good=(spent / n_good if n_good else None))

        # LOUD ON ZERO LABELS: a bakeoff whose runs produced NO quality labels measured nothing — good_rate=null across
        # every arm reads downstream as 'no findings', not 'the judge failed'. runs>0 with labeled==0 means the JUDGE
        # could not rule on a single output (unreachable / unparseable verdict), NOT that the candidates failed (those
        # are counted in `failed`). Surface it as a first-class `warning` (and on stderr) so it can never pass as clean.
        _tot_run = sum(results[a]["runs"] for a in results)
        _tot_lab = sum(results[a]["labeled"] for a in results)
        _zero_label_warn = None
        if _tot_run > 0 and _tot_lab == 0:
            _zero_label_warn = (
                "ZERO quality labels across %d run(s) in %d arm(s): the judge (%s) ruled on NOTHING, so every good_rate "
                "is null — this bakeoff measured no quality. Verify the judge is reachable and returns a parseable "
                "verdict (`spendguard health --run`)." % (_tot_run, len(results), judge_model))
            import sys as _sys
            _sys.stderr.write("[spendguard] bakeoff WARNING: %s\n" % _zero_label_warn)

        from . import advise, measurement
        import time as _time
        ranked = advise.ranked(intent=intent)                          # per-model re-rank (existing shape)
        frontier = advise.ranked(intent=intent, by_effort=True)        # the (model, effort) frontier advise/titration read
        # MEASUREMENT RECEIPT: stamp WHAT produced these good_rates — judge, sample, rubric — so the number is
        # reproducible + comparable later (docs/MEASUREMENT_RECEIPTS.md). Candidates are the ARM labels (model@effort)
        # so effort is part of the instrument identity and two efforts of one model never collapse in the receipt.
        _values = {arm: {"good": results[arm]["good"], "labeled": results[arm]["labeled"],
                         "good_rate": results[arm]["good_rate"]} for arm in results}
        reading_id = None
        try:
            reading_id = measurement.record_reading(
                intent=intent, kind="bakeoff", judge_mix=[judge_model], judge_basis="configured",
                judge_pinned=_judge_pinned, parent_id=parent_reading,
                sample_ids=[measurement.item_id(_task_sample_key(p)) for p in prompts],   # image ref folded in → distinct vision samples
                rubric=({"mode": "requirement-aware", "screen": judge_model,
                         "adjudicator": adjudicator_model or config.advisor_adjudicator_model(), "requirements": req_seen}
                        if requirement_aware else {"system": _JUDGE_SYS, "schema": _JUDGE_SCHEMA}),
                candidates=list(results.keys()),
                values=_values, aggregation="single",
                spend_usd=sum((results[a]["spent"] or 0.0) for a in results), ts=_time.time())
        except Exception as _e:
            import sys as _sys
            _sys.stderr.write("[spendguard] bakeoff: measurement receipt NOT recorded (%s: %s) — the bakeoff "
                              "result stands\n" % (type(_e).__name__, str(_e)[:80]))
        return dict(intent=intent, sample=len(prompts), efforts=efforts_list, judged_by=judge_model,
                    reading_id=reading_id, per_candidate=results, warning=_zero_label_warn,
                    ranking=ranked["models"], frontier_by_effort=frontier["models"],
                    pick=ranked["pick"], ranked_by=ranked["metric"],
                    note="recorded per (intent, model, effort) to the corpus + a measurement receipt — "
                         "`spendguard measurement inspect %s` shows the judge mix / sample / rubric." % reading_id)


def main(argv=None):
    import argparse
    import json
    ap = argparse.ArgumentParser(prog="spendguard bakeoff",
                                 description="Measure cost×quality for a slate of models on a sample of an intent's tasks.")
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/intent')
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--candidates')
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--sample', type=int)
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--efforts')
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--run')
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--budget', type=float)
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--requirement-aware')
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--adjudicator')
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--temperature', type=float)
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--top-p', type=float)
    add_registered_cli_argument(ap, 'bakeoff/bakeoff.main/ap/--seed', type=int)
    a = ap.parse_args(argv)
    r = bakeoff(a.intent, candidates=[c for c in a.candidates.split(",") if c.strip()],
                sample_n=a.sample, run=a.run, budget_usd=a.budget,
                efforts=[e for e in (a.efforts or "").split(",") if e.strip()] or None,
                requirement_aware=a.requirement_aware, adjudicator_model=a.adjudicator,
                temperature=a.temperature, top_p=a.top_p, seed=a.seed)
    print(json.dumps(r, indent=1, default=str))
    return 0 if not r.get("error") else 1
