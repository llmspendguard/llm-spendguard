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


def estimate_jsonl_cost(jsonl_path, model, batch=True, avg_out_tokens=None, provider="openai"):
    """Project cost of a /v1/chat/completions batch .jsonl. No paid calls.

    Image blocks are counted by their PIXELS via content_tokens, not by the length of their base64 — measuring
    the payload over-stated real vision batches ~25× and refused every one of them at the cap."""
    from . import content_tokens, expected_output
    n = 0
    in_tok = 0
    out_ceiling = 0
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
        # NOT the caller's cap: max_tokens is a blast-radius bound, not a statement about expected output,
        # and an omitted one used to estimate output at ZERO. See expected_output.py.
        _o, out_basis = expected_output.expect(row_model,
                                               max_tokens=(body.get("max_tokens")
                                                           or body.get("max_completion_tokens")))
        if out_basis == "unknown":
            expected_output.warn_unknown(row_model)
        out_ceiling += _o
        slot["out"] += _o
    out_tok = int(avg_out_tokens * n) if measured else out_ceiling
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
    out_cap = int(max_out) if max_out else adapters.TOKEN_FLOOR   # NOBODY NAMED A NUMBER → START HIGH: the SAME floor
    #   the realtime path applies when max_tokens is unset (max(TOKEN_FLOOR, predicted)). A cap is billed by ACTUAL
    #   tokens, so over-provisioning costs nothing while under-provisioning EMPTIES a reasoning reply — batch and
    #   realtime must not drift. `max_out` (or --avg-out on the estimate) tightens it deliberately.
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
                body = {"model": model, "max_tokens": out_cap, "messages": msgs}
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


def guarded_submit(jsonl_path, model, cap_dollars, batch=True, avg_out_tokens=None,
                   expected_cost=None, submit=True, request_cap=25000,
                   overrun_tolerance=DEFAULT_OVERRUN_TOLERANCE, endpoint="/v1/chat/completions"):
    """Estimate -> enforce cap -> log -> submit. Raises RuntimeError if it won't pass. `endpoint` is the Batch API
    target the .jsonl lines address ('/v1/chat/completions' by default, '/v1/embeddings' for an embeddings batch) —
    it must match the lines' `url`, so it is a parameter, not a hardcoded literal at the batches.create call."""
    est = estimate_jsonl_cost(jsonl_path, model, batch=batch, avg_out_tokens=avg_out_tokens)
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
    config.update_json(audit_path, lambda _d: rec)      # a gate AUDIT record; losing it loses the trail

    if not submit:
        print(f"[submit_gate] PASS (estimate only, submit=False). audit: {audit_path}")
        return None

    # passed the gate — submit via OpenAI
    from openai import OpenAI
    client = OpenAI(api_key=_api_key("OPENAI_API_KEY"))
    with open(jsonl_path, "rb") as fh:        # the upload handle was never closed
        f = client.files.create(file=fh, purpose="batch")
    b = client.batches.create(input_file_id=f.id, endpoint=endpoint, completion_window="24h")
    print(f"[submit_gate] SUBMITTED batch {b.id} (projected ${est['cost']:,.2f}). "
          f"Verify after: reconcile_openai_spend.py --estimate {est['cost']:.2f}")
    return b.id


def submit_chat_tasks(tasks, model, *, system=None, schema=None, reasoning="minimal", max_out=None,
                      cap_dollars=None, submit=True, intent=None):
    """Submit a list of CHAT tasks to the OpenAI /v1/chat/completions Batch API (~half realtime, 24h window) — the
    first-class chat BATCH submitter (the chat analogue of adapters.embed_batch), and the callable that wires
    route_economics' / bulk_delegate's BATCH leg to a real submission. Each task is a prompt STRING (custom_id auto
    = 'task-<i>') OR a {custom_id, content[, system, schema, schema_name]} dict. Builds the request .jsonl through the
    ONE models.apply_call_params authority (build_chat_batch_jsonl — so it can never send a model-wrong param), then
    ESTIMATE→cap→submit through guarded_submit (the SAME chokepoint every batch passes; cap_dollars enforced). Returns
    {batch_id, jsonl, requests, error}; submit=False estimates + writes only ($0). Collect later with
    callio.guarded_collect(batch_id) — results map by custom_id. OpenAI-only (the OpenAI Batch API serves only OpenAI
    ids); a non-OpenAI model returns a clear error (the lane fan runs it instead), never a silent metered fallback."""
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
        bid = guarded_submit(req_path, model, cap_dollars, batch=True, submit=submit, endpoint="/v1/chat/completions")
        return {**base, "batch_id": bid, "jsonl": req_path, "requests": n}
    except Exception as e:
        from . import gate as _g
        if isinstance(e, _g.deliberate_stop_types()):
            raise                                    # a cap refusal HALTS — never a silent partial submission
        return {**base, "error": str(e)[:200]}
    finally:
        try:
            _os.unlink(tasks_path)                   # the request envelope (req_path) is the durable artefact, not this
        except OSError:
            pass


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
