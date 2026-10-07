"""submit_gate.py — the ONE chokepoint every batch submission must pass.

Estimates the job's cost from the .jsonl (canonical pricing.py), REFUSES if it
exceeds the cap, logs the projection, then submits. New/edited scripts call
`guarded_submit(...)` instead of `client.batches.create(...)` directly — so no
job can be launched without its cost being checked first.

    from submit_gate import guarded_submit
    bid = guarded_submit("requests.jsonl", model="gpt-5.5", cap_dollars=50)

The gate's own estimate makes ZERO paid calls. Output tokens can't be known
before generation, so it uses each request's max_tokens as a CONSERVATIVE
ceiling (over-estimates → fails safe). Pass avg_out_tokens to override with a
measured value from your tiny test (see notes/COST_RUNBOOK.md).

CLI (estimate only, never submits):
    python scripts/submit_gate.py --jsonl requests.jsonl --model gpt-5.5 --cap 50
"""
import os, sys, json, argparse

from .pricing import batch_cost, realtime_cost, normalize

from .config import HOME as _HOME, api_key as _api_key
AUDIT_DIR = str(_HOME)


def _count_tokens(text, model):
    """Provider-aware INPUT token estimate: a REAL BPE base (exact tiktoken encoding for OpenAI models) × the
    MEASURED per-provider o200k→native factor (provider_tokens). This is what makes an anthropic/gemini/glm
    estimate honest instead of an OpenAI-tokenizer proxy. Signature is unchanged (text, model) so every caller —
    bakeoff, effort_titration, experiment — improves at once. Fail-open all the way down (never raises)."""
    from . import provider_tokens, adapters
    try:
        prov = adapters.provider_for(model)
    except Exception:
        prov = None                                   # unknown id → provider_tokens uses the o200k proxy (still real BPE)
    return provider_tokens.count_text(text, provider=prov, model=model)


# How far a projection may exceed the caller's OWN stated expectation before this refuses. Not a judgement
# about the work — a tolerance band on two numbers, named here (it was an inline 1.2 with the "20%" spelled
# out in the message beside it, so changing one silently made the other a lie) and overridable per call.
DEFAULT_OVERRUN_TOLERANCE = 1.2


def estimate_jsonl_cost(jsonl_path, model, batch=True, avg_out_tokens=None, provider="openai", intent=None):
    """Project cost of a /v1/chat/completions batch .jsonl. No paid calls.

    Image blocks are counted by their PIXELS via content_tokens, not by the length of their base64 — measuring
    the payload over-stated real vision batches ~25× and refused every one of them at the cap.

    OUTPUT is estimated by the measured/declared/ceiling basis (expected_output.batch_output_estimate, keyed by
    `intent`), NOT the send ceiling — the ceiling over-stated ~100x and made the cap unusable. A caller's measured
    `avg_out_tokens` still overrides (the 'measured avg' basis); absent it, the per-model basis is named honestly."""
    from . import content_tokens, expected_output
    n = 0
    in_tok = 0
    out_est = 0        # Σ per-request EXPECTED output (measured/declared/ceiling basis) — the realistic estimate, NOT the ceiling
    media = False
    img = 0
    out_basis = "unknown"      # bound BEFORE the loop: an empty/blank-only .jsonl left it undefined and the
                               # return line raised UnboundLocalError — a crash instead of "0 requests"
    # PER-MODEL TOKENS. Tokens were already counted against each request's OWN model (`body.get("model")`),
    # then the whole total was priced at the one `model` argument. A .jsonl mixing a cheap and an expensive
    # model — the normal shape when you fan one job across tiers — was therefore costed entirely at whichever
    # rate the caller passed, in either direction. Accumulate per model and sum the per-model costs.
    by_model = {}
    measured = avg_out_tokens is not None and avg_out_tokens > 0   # 0/negative isn't a real sample → use the max_tokens ceiling, don't zero out output cost
    used_heuristic = False
    try:
        import tiktoken  # noqa
    except Exception:
        used_heuristic = True
    tt = lambda s: _count_tokens(s, model)                        # noqa: E731 — one-line adapter for the counter
    # errors="replace", not "ignore": this is a COST estimate — silently DROPPING invalid bytes undercounts the
    # tokens (and the $), whereas replace keeps a placeholder per bad byte so the count can't shrink below reality.
    with open(jsonl_path, errors="replace") as fh:     # (also: was an unclosed open() — one leaked fd per estimate)
      for line in fh:
        line = line.strip()
        if not line:
            continue
        n += 1
        body = json.loads(line).get("body", {})
        row_model = body.get("model") or model
        slot = by_model.setdefault(row_model, {"in": 0, "out": 0, "n": 0})
        slot["n"] += 1
        # EMBEDDINGS batch bodies carry `input` (a str, a list of strs, or pre-tokenized int arrays), NOT `messages`,
        # and output tokens are always 0 — priced by input alone. Counting only `messages` estimated these at $0, so
        # the cap could never see an embeddings batch coming (same fix as gate._estimate_openai_jsonl).
        if body.get("input") is not None and not body.get("messages"):
            _inp = body["input"]
            for s in (_inp if isinstance(_inp, list) else [_inp]):
                t = tt(s) if isinstance(s, str) else (len(s) if isinstance(s, (list, tuple)) else 0)
                in_tok += t
                slot["in"] += t
            out_basis = "embeddings(out=0)"
            continue                                       # no output tokens, no expected-output rung for embeddings
        for m in body.get("messages", []):
            t, d = content_tokens.count_detail(m.get("content", ""), provider=provider,
                                               model=row_model, text_tokens=tt)
            in_tok += t
            slot["in"] += t
            img += d["images"] + d["pdf_pages"]
            media = media or bool(d["images"] or d["pdf_pages"])
        # The ESTIMATE basis — measured(intent) → measured(model) → declared → ceiling, per model, named honestly in
        # out_basis — NOT the send ceiling (which over-stated ~100x and made the cap unusable). A caller's measured
        # avg_out_tokens still OVERRIDES below ('measured avg'); the ceiling now lives only as the last-resort basis.
        _o, out_basis = expected_output.batch_output_estimate(
            row_model, intent=intent, declared_out=None,
            ceiling=(body.get("max_tokens") or body.get("max_completion_tokens")))
        out_est += _o
        slot["out"] += _o
    out_tok = int(avg_out_tokens * n) if measured else out_est
    cost_fn = batch_cost if batch else realtime_cost
    # Price each model's own tokens at its own rate, then sum. A measured average output is spread over the
    # requests that produced it, pro-rata per model, rather than being priced at one arbitrary model's rate.
    cost = 0.0
    for mdl, slot in by_model.items():
        m_out = int(avg_out_tokens * slot["n"]) if measured else slot["out"]
        cost += cost_fn(mdl, slot["in"], m_out)
    models_seen = sorted(by_model)
    return dict(requests=n, in_tok=in_tok, out_tok=out_tok, cost=cost, media=media, media_units=img,
                out_basis=("measured avg" if measured else out_basis),
                token_basis=("char/4 heuristic — install tiktoken for accuracy" if used_heuristic else "tiktoken"),
                model=(normalize(models_seen[0]) if len(models_seen) == 1 else normalize(model)),
                # NAME THE MIXTURE. A single `model` field on a multi-model file reads as "this is what you
                # are buying" and hid the fact that the total spans rates; the per-model split is the receipt.
                models=[{"model": normalize(k), "requests": v["n"],
                         "in_tok": v["in"], "cost": cost_fn(k, v["in"],
                                                            int(avg_out_tokens * v["n"]) if measured else v["out"])}
                        for k, v in sorted(by_model.items())],
                mode=("batch" if batch else "realtime"))


def build_chat_batch_jsonl(tasks_path, model, system=None, max_out=None, reasoning="minimal",
                           schema=None, schema_name="result"):
    """Build an OpenAI /v1/chat/completions Batch-API request .jsonl FROM TASKS, so a caller supplies only
    {custom_id, content} lines + one shared `system` + a `model` and NEVER hand-rolls the per-model request
    envelope. spendguard builds each line's `body` through models.apply_call_params — the ONE authority for
    tokens_param (max_tokens vs max_completion_tokens) — so a caller can never send a model-wrong param again (the
    failure that returned 250/250 HTTP 400 "'max_tokens' is not supported ... use 'max_completion_tokens'"). Batch
    and realtime therefore cannot drift: both build through the same models.py path.

    `custom_id` is preserved VERBATIM — it is the caller's mapping key and comes back on each result line; spendguard
    never invents an index/hash the caller can't reconstruct. `system` is sent as a system message (once per request).

    STRUCTURED OUTPUT RIDES THE SAME BINDING AS REALTIME. `schema` (a JSON Schema) makes every line carry the vendor's
    strict `response_format` through adapters.json_schema_request — the one function the realtime path also uses —
    so a batch can never be built with a hand-rolled strict adaptation that disagrees with the realtime one. That
    function REFUSES a schema strict mode cannot serve (a dynamic-key map, or more than the provider's total-enum
    limit) with a typed SchemaNotStrictExpressible BEFORE anything is written: on a batch there is no per-row heal
    for a rejected response_format, so an unservable schema fails EVERY line, and a consumer measured exactly that —
    twelve batches, zero completions — read downstream as empty results rather than as the one refusal it was.

    A TASK MAY OVERRIDE THE SHARED DEFAULTS. A pool that coalesces several activities into one submission (the
    DataLoader pattern: same model, different prompts) has requests with different system messages and different
    shapes, and OpenAI requires one MODEL per batch, not one prompt. So a task line may carry its own `system`,
    `schema` and `schema_name`; absent keys fall back to the shared arguments. The contract stays one line = one
    request = one custom_id.

    REASONING is resolved by models.resolve_effort(model, reasoning), NOT the raw family value — because a batch
    CANNOT heal per row the way adapters.call does, so the reasoning_effort must be VERIFIABLY accepted up front.
    resolve_effort discovers + records the accepted set (gpt-5.6-luna REJECTS the family default 'minimal' → a 400
    the batch cannot recover from) and returns the accepted value, or None to OMIT the param. The output ceiling
    floors to adapters.TOKEN_FLOOR when the caller names no `max_out` — the SAME "nobody named a number → start high"
    floor the realtime path applies to EVERY model — so reasoning can't empty the reply (the max_output_poisoning
    trap). A cap is billed by ACTUAL tokens, so over-provisioning is harmless. `max_out` / `reasoning` override.

    Writes to a FRESH temp .jsonl (never a caller-supplied path — so the task input can never be clobbered) and
    returns (built_path, n_tasks); the caller submits/inspects that path and removes it when done. Refuses a
    non-OpenAI model (the OpenAI Batch API serves only OpenAI ids) and a task line missing custom_id/content —
    fail-closed, never a silent drop; a partial temp is unlinked on any error."""
    import json, os, tempfile
    from . import models, adapters
    prov = adapters.provider_for(model)
    if prov != "openai":
        raise ValueError(f"build_chat_batch_jsonl: the OpenAI /v1/chat/completions Batch API serves only OpenAI "
                         f"models; got {model!r} (provider {prov!r}). Use the lane fan / a provider batch instead.")
    # spendguard OWNS the output budget (adapters.output_budget; docs/CANONICAL_CONCERNS.json) — the caller's `max_out`
    # is IGNORED, the budget is the model CEILING. A batch can't heal per row, so starting at the ceiling is exactly
    # right: max_output is billed on ACTUAL tokens, so the max is free and no reasoning reply or structured JSON can
    # silently truncate. Batch and realtime share the ONE home, so they cannot drift on the send budget.
    if max_out:
        adapters._warn_once_caller_maxtokens("batch:" + str(model), int(max_out))   # caller max_out ignored — warned once
    out_cap = adapters.output_budget(model)
    _eff = models.resolve_effort(model, reasoning)   # the VERIFIABLY-ACCEPTED reasoning_effort — a batch can't heal
    #   per row, so this resolves up front (discovers + records the accepted set); a family default the endpoint
    #   rejects (gpt-5.6-luna: 'minimal') never reaches a batch. None → OMIT the param (model default).
    fd, out_path = tempfile.mkstemp(prefix="spendguard-batch-req-", suffix=".jsonl")
    n = 0
    try:
        with os.fdopen(fd, "w") as fout, open(tasks_path, errors="replace") as fin:   # READ input, WRITE a fresh temp
            for ln in fin:
                s = ln.strip()
                if not s:
                    continue
                try:
                    task = json.loads(s)
                except Exception as e:
                    raise ValueError(f"batch task line is not JSON ({str(e)[:80]}) — each line must be "
                                     f'{{"custom_id": <id>, "content": <text>}}') from e
                cid = task.get("custom_id") if isinstance(task, dict) else None
                content = task.get("content") if isinstance(task, dict) else None
                if cid is None or content is None:
                    raise ValueError('each batch task line must be a JSON object with "custom_id" (your mapping key, '
                                     'preserved verbatim) and "content" (the user text)')
                t_system = task.get("system", system)             # per-task overrides; absent → the shared default
                t_schema = task.get("schema", schema)
                t_name = task.get("schema_name", schema_name)
                msgs = ([{"role": "system", "content": t_system}] if t_system else []) + \
                       [{"role": "user", "content": content}]
                raw_model = model.split(":", 1)[1] if ":" in model else model   # vendor API takes the BARE id; the full
                #   'openai:…' id 404s — the SAME strip every realtime path does. apply_call_params gets the FULL id.
                body = {"model": raw_model, "max_tokens": out_cap, "messages": msgs}
                models.apply_call_params(model, body, dialect="openai")   # tokens_param (max_tokens vs max_completion_tokens)
                if _eff is None:
                    body.pop("reasoning_effort", None)   # endpoint takes no accepted effort → OMIT (model default)
                else:
                    body["reasoning_effort"] = _eff      # the resolve_effort accepted value (overrides the family guess)
                if t_schema is not None:
                    # raises SchemaNotStrictExpressible for a schema strict mode cannot serve — caught by the
                    # enclosing handler, which unlinks the partial temp: nothing unservable ever reaches an upload
                    body.update(adapters.json_schema_request("openai", t_schema, name=t_name))
                fout.write(json.dumps({"custom_id": cid, "method": "POST", "url": "/v1/chat/completions",
                                       "body": body}) + "\n")
                n += 1
        if not n:
            raise ValueError(f"no tasks in {tasks_path} (each line = a JSON object {{custom_id, content}})")
    except Exception:
        try:
            os.unlink(out_path)     # never leave a partial/empty temp envelope behind
        except OSError:
            pass
        raise
    return out_path, n


def _preflight_first_request(provider, first_request, endpoint):
    """MANDATORY pre-flight for a batch door: send the FIRST built request LIVE to the provider, synchronously, and
    require a 2xx BEFORE the batch of N is committed and BEFORE any estimate is booked. One request costs fractions of
    a cent; it catches the whole class an estimate cannot see — a model-wrong parameter (max_tokens vs
    max_completion_tokens), a rejected reasoning_effort, an unservable response_format schema, an auth failure, a
    deprecated/stale model id — turning 1,930/1,930 HTTP 400 into ONE actionable error at the boundary we actually ship
    across (and so a 'completed' batch with 0 successes can never settle as phantom spend). The probe rides the gated
    SDK (recorded), output capped tiny (it is a liveness check, not the real generation). Returns {"ok": True} or
    {"ok": False, "error": <verbatim provider error>, "hint": <agentic 'did you mean' for a stale id>}. NEVER raises —
    a probe that itself errors returns ok:False, so the caller REFUSES rather than guessing (fail-closed)."""
    try:
        req = first_request or {}
        body = dict(req.get("body") or req.get("params") or {})   # OpenAI jsonl line = 'body'; Anthropic request = 'params'
        if not body:
            return {"ok": True}                              # the builders guarantee >=1 line; nothing to probe → don't block
        raw = body.get("model") or ""
        if provider == "openai":
            from openai import OpenAI
            client = OpenAI(api_key=_api_key("OPENAI_API_KEY"))
            if endpoint == "/v1/embeddings":
                client.embeddings.create(**body)             # one input → cheap; exercises model id / auth / dimensions
            else:
                probe = dict(body)                           # cap output tiny: shape acceptance (param/schema/effort), not output
                for _k in ("max_completion_tokens", "max_tokens"):
                    if _k in probe:
                        probe[_k] = min(int(probe[_k] or 16), 16)
                client.chat.completions.create(**probe)      # tests the EXACT param name + response_format + reasoning_effort
        elif provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=_api_key("ANTHROPIC_API_KEY"))
            probe = dict(body)
            probe["max_tokens"] = min(int(probe.get("max_tokens") or 16), 16)
            client.messages.create(**probe)
        else:
            return {"ok": True}                              # unknown door — do not block (never reached for the two doors)
        return {"ok": True}
    except Exception as e:
        hint = ""
        try:                                                 # agentic 'did you mean' for a stale/unknown id — the SAME
            from . import vendor_call as _vc                 # resolver the realtime dispatch pre-flight uses (closest_served)
            _same, _live = _vc.closest_served(provider, raw)
            if _same:
                hint = f"  (did you mean {_same!r} — the currently-served same model?)"
        except Exception:
            pass
        return {"ok": False, "error": str(e)[:300], "hint": hint}


def guarded_submit(jsonl_path, model, cap_dollars, batch=True, avg_out_tokens=None,
                   expected_cost=None, submit=True, request_cap=25000,
                   overrun_tolerance=DEFAULT_OVERRUN_TOLERANCE, endpoint="/v1/chat/completions", intent=None,
                   metadata=None, preflight=True):
    """Estimate -> enforce cap -> PRE-FLIGHT one live request -> log -> submit. Raises RuntimeError if it won't pass. `endpoint` is the Batch API
    target the .jsonl lines address ('/v1/chat/completions' by default, '/v1/embeddings' for an embeddings batch) —
    it must match the lines' `url`, so it is a parameter, not a hardcoded literal at the batches.create call.

    `intent` is the caller's job-type label. It is set on the recording CONTEXT for the duration of the submit so the
    gate's provisional batch-cost row (gate._decide_and_account → calls.record_call, fired synchronously inside the
    gated files.create) attributes to it instead of '(none)' — a batch is attributable work, and its spend must carry
    its intent from submission, not only when reconcile later matches it by batch_id.

    `metadata` (an OpenAI batch metadata dict, keys/values ≤64/512 chars, ≤16 pairs) is attached to the created batch.
    batch_tracker.submit_offload uses it to stamp a DETERMINISTIC offload key on the batch so a crashed-then-retried
    offload is de-duplicated against the provider's own batch list (exactly-once) instead of double-submitting."""
    est = estimate_jsonl_cost(jsonl_path, model, batch=batch, avg_out_tokens=avg_out_tokens, intent=intent)
    print(f"[submit_gate] {est['requests']:,} req · {est['mode']} · in={est['in_tok']:,} "
          f"out={est['out_tok']:,} ({est['out_basis']}; {est['token_basis']}) -> ${est['cost']:,.2f}")

    if est["requests"] > request_cap:
        raise RuntimeError(f"REFUSED: {est['requests']:,} requests > request_cap {request_cap:,} "
                           f"(chunk it; OpenAI batch limit + blast-radius control).")
    if cap_dollars is not None and est["cost"] > cap_dollars:
        raise RuntimeError(f"REFUSED: projected ${est['cost']:,.2f} > cap ${cap_dollars:,.2f}. "
                           f"Pack more items/request, shrink the prompt, pick a cheaper model, or raise the cap deliberately.")
    if expected_cost is not None and est["cost"] > expected_cost * overrun_tolerance:
        raise RuntimeError(f"REFUSED: projected ${est['cost']:,.2f} is "
                           f">{(overrun_tolerance - 1) * 100:.0f}% over your expected "
                           f"${expected_cost:,.2f} — re-check token assumptions before submitting.")

    os.makedirs(AUDIT_DIR, exist_ok=True)
    rec = dict(est); rec["jsonl"] = jsonl_path; rec["cap"] = cap_dollars; rec["expected"] = expected_cost
    # DISAMBIGUATE by full path: keyed on basename alone, two jobs using same-named .jsonl files in DIFFERENT
    # directories mapped to one audit file and silently overwrote each other's trail. Append a short hash of the
    # absolute path (parsing, not a decision) so each source file keeps its own gate record.
    import hashlib as _hashlib
    _tag = _hashlib.sha256(os.path.abspath(jsonl_path).encode()).hexdigest()[:8]
    audit_path = os.path.join(AUDIT_DIR, f"{os.path.basename(jsonl_path)}.{_tag}.gate.json")
    from . import config
    config.update_json(audit_path, lambda _d: rec,      # a gate AUDIT record; losing it loses the trail
                       quarantine_unparseable=True)     # so a corrupt prior audit is moved aside (kept .corrupt) and the
    #                                                     current record STILL persists before submission — never a silent decline

    if not submit:
        print(f"[submit_gate] PASS (estimate only, submit=False). audit: {audit_path}")
        return None

    # MANDATORY PRE-FLIGHT — a gate whose purpose is preventing wasted spend must not submit N requests that cannot
    # succeed. Send the FIRST built request live and require a 2xx BEFORE client.batches.create AND BEFORE booking the
    # estimate (record_accepted_batch_estimate below), so a model-wrong param / rejected reasoning_effort / unservable
    # response_format / auth failure / stale id surfaces as ONE error — not N — and never settles as phantom spend.
    # preflight=False bypasses deliberately (a caller who already pre-flighted).
    if preflight:
        import json as _jpf
        _first = None
        try:
            with open(jsonl_path, errors="replace") as _pfh:
                for _ln in _pfh:
                    if _ln.strip():
                        _first = _jpf.loads(_ln)
                        break
        except Exception:
            _first = None
        if _first is not None:
            _pf = _preflight_first_request("openai", _first, endpoint)
            if not _pf["ok"]:
                raise RuntimeError(
                    f"PRE-FLIGHT FAILED — NOT submitting {est['requests']:,} requests and NOT booking the estimate. "
                    f"First request rejected by {endpoint}: {_pf['error']}{_pf.get('hint', '')}  "
                    f"(set preflight=False to bypass deliberately).")

    # passed the gate — submit via OpenAI, with the intent on the recording context so the gate's provisional batch
    # row (recorded synchronously inside the gated files.create) attributes to it, not '(none)'. Save + restore the
    # caller's exact context so the intent set here never leaks into a later call on this thread.
    from . import calls as _calls
    _prev_ctx = dict(_calls.current())
    _calls.set_context(intent=intent, defer_batch_booking=True)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=_api_key("OPENAI_API_KEY"))
        with open(jsonl_path, "rb") as fh:        # the upload handle was never closed
            f = client.files.create(file=fh, purpose="batch")
        # metadata carries batch_tracker's deterministic offload key so a crash-retried offload adopts THIS batch
        # (found by that key in the provider's batch list) instead of creating a duplicate — the exactly-once tag.
        _extra = {"metadata": metadata} if metadata else {}
        b = client.batches.create(input_file_id=f.id, endpoint=endpoint, completion_window="24h", **_extra)
        from . import gate as _gate
        _gate.record_accepted_batch_estimate({**est, "provider": "openai", "model": est["model"]})
    finally:
        _calls._local.ctx = _prev_ctx             # restore the caller's exact context (never leak the submit intent)
    print(f"[submit_gate] SUBMITTED batch {b.id} (projected ${est['cost']:,.2f}). "
          f"Verify after: reconcile_openai_spend.py --estimate {est['cost']:.2f}")
    return b.id


def submit_chat_tasks(tasks, model, *, system=None, schema=None, reasoning="minimal", max_out=None,
                      cap_dollars=None, submit=True, intent=None, metadata=None, preflight=True):
    """Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the
    first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires
    route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto
    = 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the
    ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then
    ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns
    {batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with its SETTLE twin
    callio.collect_chat_tasks(batch_id, intent, model) — results keyed by custom_id (or stream callio.guarded_collect
    directly). OpenAI-only (the OpenAI Batch API serves only OpenAI ids); a non-OpenAI model returns a clear error
    (the lane fan runs it instead), never a silent metered fallback. `metadata` is forwarded to the created batch —
    batch_tracker.submit_offload stamps its deterministic offload key there for exactly-once de-duplication."""
    import json as _json
    import os as _os
    import tempfile as _tf
    from . import adapters
    items = list(tasks or [])
    base = {"batch_id": None, "jsonl": None, "requests": len(items), "error": None}
    if not items:
        return base
    prov = adapters.provider_for(model)
    if prov != "openai":
        return {**base, "error": "chat batch is OpenAI-only (the /v1/chat/completions Batch API serves only OpenAI "
                "ids); got %r (provider %r) — run it on the lane fan / realtime instead." % (model, prov)}
    # Set the intent CONTEXT for the WHOLE submission, exactly as the realtime adapters.call path does — so EVERY
    # record of this submit attributes to the caller's intent, not '(none)': the build-time models.resolve_effort
    # discovery probes, the gate's provisional batch row, and the http_capture control-plane events. Restored in the
    # finally so the intent never leaks into a later call on this thread.
    from . import calls as _calls
    _prev_ctx = dict(_calls.current())
    if intent:
        _calls.set_context(intent=intent)
    fd, tasks_path = _tf.mkstemp(prefix="spendguard-batch-tasks-", suffix=".jsonl")   # FRESH temp — never a caller path
    try:
        with _os.fdopen(fd, "w") as fh:
            for i, t in enumerate(items):
                if isinstance(t, dict):
                    row = {"custom_id": str(t.get("custom_id", "task-%d" % i)), "content": str(t.get("content", ""))}
                    for k in ("system", "schema", "schema_name"):
                        if t.get(k) is not None:
                            row[k] = t[k]
                else:
                    row = {"custom_id": "task-%d" % i, "content": str(t)}
                fh.write(_json.dumps(row) + "\n")
        req_path, n = build_chat_batch_jsonl(tasks_path, model, system=system, max_out=max_out,
                                             reasoning=reasoning, schema=schema)
        bid = guarded_submit(req_path, model, cap_dollars, batch=True, submit=submit,
                             endpoint="/v1/chat/completions", intent=intent, metadata=metadata,
                             preflight=preflight)
        return {**base, "batch_id": bid, "jsonl": req_path, "requests": n}
    except Exception as e:
        from . import gate as _g
        if isinstance(e, _g.deliberate_stop_types()):
            raise                                    # a cap refusal HALTS — never a silent partial submission
        return {**base, "error": str(e)[:200]}
    finally:
        _calls._local.ctx = _prev_ctx                # restore the caller's exact context (never leak the submit intent)
        try:
            _os.unlink(tasks_path)                   # the request envelope (req_path) is the durable artefact, not this
        except OSError:
            pass


# The Anthropic Message Batches API limit: 100,000 requests OR 256 MB per batch, whichever is reached first (grounded
# from the Anthropic docs, 2026-09). Used as the DEFAULT request ceiling; a caller passes a tighter one for blast
# radius, and the per-$ `cap_dollars` is the real spend bound. (The 256 MB size limit is provider-enforced at create.)
_ANTHROPIC_BATCH_REQUEST_CAP = 100_000


def build_message_batch_requests(tasks, model, *, system=None, max_out=None, schema=None, schema_name="result"):
    """Build the INLINE request list for the Anthropic Message Batches API FROM an in-memory task list — the Anthropic
    twin of build_chat_batch_jsonl. Unlike OpenAI (a .jsonl FILE uploaded first), Anthropic takes requests INLINE:
    a list of {custom_id, params}, where params is a full Messages body. Returns (requests, n).

    Each task is a prompt STRING (custom_id auto = 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]}
    dict. Same discipline as the OpenAI builder: the per-request body is built through the ONE models.apply_call_params
    authority (dialect='anthropic') so a caller can never send a model-wrong param; `custom_id` is preserved VERBATIM
    (the caller's mapping key, returned on each result line); `system` rides the Anthropic TOP-LEVEL `system` param
    (NOT a system-role message — that is the Messages-API shape); structured output rides the SAME
    adapters.json_schema_request('anthropic', …) forced-tool binding the realtime path uses (refused up front with
    SchemaNotStrictExpressible if strict mode can't serve it — a batch cannot heal per row, so an unservable schema
    would fail EVERY request). A task may override system/schema/schema_name; absent keys fall back to the shared
    arguments. spendguard OWNS the output budget: max_tokens is adapters.output_budget(model) (the model CEILING — and
    Anthropic REQUIRES max_tokens, so it is always present), the caller's `max_out` is warned+ignored (billed on ACTUAL
    tokens, so the ceiling is free and no reasoning/JSON reply can silently truncate). Refuses a non-Anthropic model
    and a task with no content — fail-closed, never a silent drop."""
    from . import models, adapters
    prov = adapters.provider_for(model)
    if prov != "anthropic":
        raise ValueError(f"build_message_batch_requests: the Anthropic Message Batches API serves only Anthropic "
                         f"models; got {model!r} (provider {prov!r}). Use build_chat_batch_jsonl / the lane fan instead.")
    if max_out:
        adapters._warn_once_caller_maxtokens("msgbatch:" + str(model), int(max_out))   # caller max_out ignored — warned once
    out_cap = adapters.output_budget(model)
    requests = []
    for i, t in enumerate(tasks or []):
        if isinstance(t, dict):
            cid = str(t.get("custom_id", "task-%d" % i))
            content = t.get("content")
            t_system, t_schema, t_name = t.get("system", system), t.get("schema", schema), t.get("schema_name", schema_name)
        else:
            cid, content = "task-%d" % i, t
            t_system, t_schema, t_name = system, schema, schema_name
        if content is None:
            raise ValueError('each batch task must be a prompt string or a {"custom_id","content"[,…]} object; '
                             'got one with no "content"')
        raw_model = model.split(":", 1)[1] if ":" in model else model   # the vendor API takes the BARE id; the full
        #   spendguard id ('anthropic:claude-haiku-4-5') 404s as a model name — the SAME strip every realtime path does
        #   (adapters._call_once). apply_call_params below still receives the FULL id for its fact/pricing lookups.
        params = {"model": raw_model, "max_tokens": out_cap, "messages": [{"role": "user", "content": str(content)}]}
        if t_system:
            params["system"] = t_system        # Anthropic: system is a TOP-LEVEL param, not a system-role message
        models.apply_call_params(model, params, dialect="anthropic")
        if t_schema is not None:
            params.update(adapters.json_schema_request("anthropic", t_schema, name=t_name))   # forced tool (enforces shape)
        requests.append({"custom_id": cid, "params": params})
    if not requests:
        raise ValueError("no tasks to submit (each task = a prompt string or a {custom_id, content} object)")
    return requests, len(requests)


def submit_message_batch(tasks, model, *, system=None, schema=None, max_out=None, expected_out_tokens=None,
                         cap_dollars=None, submit=True, request_cap=_ANTHROPIC_BATCH_REQUEST_CAP, intent=None,
                         preflight=True):
    """Submit a list of tasks to the Anthropic Message Batches API (~half realtime, 29-day result window) — the
    Anthropic twin of submit_chat_tasks, and the Messages-API half of spendguard's batch surface. Each task is a prompt
    STRING (custom_id auto = 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the
    INLINE requests through the ONE models.apply_call_params authority (build_message_batch_requests), ESTIMATES $0 via
    gate.estimate_message_batch (the SAME estimator the gate re-runs at create — no drift), REFUSES over the cap, then
    submits via client.messages.batches.create — which the gate intercepts (_gate_anthropic) for the global/daily/monthly
    + per-batch caps AND the provisional batch-cost row (attributed to `intent` via the recording context set here).
    Returns {batch_id, requests, estimate, error}; submit=False estimates only ($0). Collect with its SETTLE twin
    callio.collect_message_batch(batch_id, intent, model).

    COST ESTIMATE BASIS (the authorization number) is what the job will PLAUSIBLY emit, NOT the ceiling we send: the
    MEASURED per-intent output (seeded as this `intent` accrues history) → a DECLARED `expected_out_tokens` (per request)
    → the ceiling, named honestly in est['out_basis']. `expected_out_tokens` lets a caller who has measured their output
    authorize a cold-intent job without the ceiling's ~100x over-statement; `max_out` informs NEITHER the send (spendguard
    sends the ceiling, billed on ACTUAL tokens) NOR the estimate, so a passed max_out is pointed at expected_out_tokens.

    DUAL CAP, named: the result states BOTH the caller `cap_dollars` AND the global GATE_CAP and WHICH one bound — so a
    pass here can't be followed by a surprise gate refusal citing the other. A genuinely runaway request set is still
    caught by the gate's SEPARATE worst-case (ceiling) guard. REFUSE, NEVER DEGRADE: over a cap the result carries the
    estimate + a REFUSED error and NO batch is created; a gate refusal at create PROPAGATES — never a silent realtime
    fallback. Anthropic-only; a non-Anthropic model returns a clear error.

    Unlike OpenAI there is NO `metadata` parameter: Anthropic batches carry no server-side metadata, so the
    exactly-once offload key (batch_tracker) rides each request's custom_id instead — see batch_tracker.submit_offload."""
    from . import adapters
    items = list(tasks or [])
    base = {"batch_id": None, "requests": len(items), "estimate": None, "error": None}
    if not items:
        return base
    prov = adapters.provider_for(model)
    if prov != "anthropic":
        return {**base, "error": "message batch is Anthropic-only (the Message Batches API serves only Anthropic ids); "
                "got %r (provider %r) — run it on submit_chat_tasks (OpenAI) / the lane fan instead." % (model, prov)}
    if max_out and not expected_out_tokens:          # max_out informs neither the send nor the estimate — point at the real knob
        print(f"[submit_gate] max_out={max_out} informs NEITHER the send (spendguard sends the model ceiling, billed on "
              f"ACTUAL tokens) NOR the cost estimate. To inform the ESTIMATE for authorization, pass "
              f"expected_out_tokens=<measured per-request output>.", file=sys.stderr)
    # Set intent + the DECLARED output on the recording CONTEXT for the whole submit, so the gate's provisional row (fired
    # inside the gated create) attributes to intent AND the gate's at-create estimate uses the same declared basis.
    # Restored in the finally so neither leaks into a later call on this thread.
    from . import calls as _calls, gate
    _prev_ctx = dict(_calls.current())
    _calls.set_context(intent=intent, batch_expected_out=expected_out_tokens, defer_batch_booking=True)
    try:
        requests, n = build_message_batch_requests(items, model, system=system, max_out=max_out, schema=schema)
        est = gate.estimate_message_batch(requests, intent=intent, declared_out=expected_out_tokens)
        out = {**base, "requests": n, "estimate": est}
        global_cap = gate._cap()                      # the global GATE_CAP — named alongside the caller cap so neither surprises
        print(f"[submit_gate] {n:,} req · batch · anthropic · in={est['in_tok']:,} out={est['out_tok']:,} "
              f"({est.get('out_basis', '?')}) -> ${est['cost']:,.2f}  "
              f"(caps: caller {('$%.2f' % cap_dollars) if cap_dollars is not None else '—'} · global ${global_cap:.0f})")
        if n > request_cap:
            return {**out, "error": f"REFUSED: {n:,} requests > request_cap {request_cap:,} (chunk it; the Anthropic "
                    f"batch limit is 100,000 requests / 256 MB)."}
        # BOTH caps, and which binds (Part 4). est['cost'] is the REALISTIC basis (out_basis), never the ceiling.
        binding = None
        if cap_dollars is not None and est["cost"] > cap_dollars:
            binding = ("caller cap", cap_dollars)
        elif est["cost"] > global_cap:
            binding = ("global GATE_CAP", global_cap)
        if binding:
            which, capv = binding
            other = (f"global GATE_CAP ${global_cap:.0f}" if which == "caller cap"
                     else (f"caller cap ${cap_dollars:,.2f}" if cap_dollars is not None else "no caller cap set"))
            return {**out, "error": f"REFUSED: projected ${est['cost']:,.2f} ({est.get('out_basis', '?')}) > {which} "
                    f"${capv:,.2f} [also {other}]. Declare expected_out_tokens, pack more items/request, pick a cheaper "
                    f"model, or raise the cap deliberately."}
        if not submit:
            print("[submit_gate] PASS (estimate only, submit=False).")
            return out
        # MANDATORY PRE-FLIGHT — send the FIRST inline request live and require a 2xx BEFORE messages.batches.create
        # and BEFORE booking the estimate, so a model-wrong param / unservable schema / auth / stale id surfaces as ONE
        # error, never N, and never settles as phantom spend. preflight=False bypasses deliberately.
        if preflight:
            _pf = _preflight_first_request("anthropic", requests[0] if requests else {}, "/v1/messages")
            if not _pf["ok"]:
                return {**out, "error": f"PRE-FLIGHT FAILED — NOT submitting {n:,} requests and NOT booking the "
                        f"estimate. First request rejected by the Messages API: {_pf['error']}{_pf.get('hint', '')} "
                        f"(set preflight=False to bypass deliberately)."}
        import anthropic
        client = anthropic.Anthropic(api_key=_api_key("ANTHROPIC_API_KEY"))
        # The gate (_gate_anthropic) re-estimates these inline requests (same basis), enforces the global/daily/monthly +
        # per-batch caps + the SEPARATE worst-case ceiling guard (raising SpendGateRefused — which propagates below), and
        # records the provisional batch-cost row at the realistic estimate.
        b = client.messages.batches.create(requests=requests)
        gate.record_accepted_batch_estimate(est)
        print(f"[submit_gate] SUBMITTED message batch {b.id} (projected ${est['cost']:,.2f}, {est.get('out_basis', '?')}). "
              f"Collect with callio.collect_message_batch({b.id!r}, intent, {model!r}).")
        return {**out, "batch_id": b.id}
    except Exception as e:
        from . import gate as _g
        if isinstance(e, _g.deliberate_stop_types()):
            raise                                    # a cap refusal / deadline HALTS — never a silent realtime fallback
        return {**base, "error": str(e)[:200]}
    finally:
        _calls._local.ctx = _prev_ctx                # restore the caller's exact context (never leak the submit intent)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--cap", type=float, help="refuse if projected cost exceeds this $")
    ap.add_argument("--avg-out", type=float, help="measured avg output tokens/item (else uses max_tokens ceiling)")
    ap.add_argument("--realtime", action="store_true")
    a = ap.parse_args()
    est = estimate_jsonl_cost(a.jsonl, a.model, batch=not a.realtime, avg_out_tokens=a.avg_out)
    print(json.dumps(est, indent=2))
    # `is not None`, NOT truthiness: `--cap 0` is the STRICTEST cap a user can express ("refuse any spend"),
    # and `if a.cap` read it as "no cap set" and passed everything. guarded_submit() a few lines up already
    # got this right with `is not None`, so the library refused what its own CLI waved through.
    if a.cap is not None and est["cost"] > a.cap:
        print(f"\nWOULD REFUSE: ${est['cost']:,.2f} > cap ${a.cap:,.2f}")
        sys.exit(2)
    print(f"\nWOULD PASS (cap ${a.cap if a.cap else 'none'}).")


if __name__ == "__main__":
    main()
