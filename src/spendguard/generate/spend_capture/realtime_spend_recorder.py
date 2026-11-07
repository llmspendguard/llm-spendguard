"""Realtime spend recording owned by the Spend Capture context."""

import atexit as _atexit
import datetime
import json
import os
import sys
import threading
import time

from ... import pricing
from ...config import RT_LOG
from ...manage.admission import admission_accessors as _admission_accessors
from ...store.call_corpus import call_corpus_accessors as _call_corpus_accessors

MEASUREMENT_EFFORT_DEFAULT = "provider-default"
_REALTIME_FLUSH_CALL_COUNT = 200
_rt_lock = threading.Lock()
_rt_spent = 0.0
_rt_agg = {}
_rt_since_flush = 0

_gate_warn_once = _admission_accessors.resolve__gate_warn_once
_warn_implausible = _admission_accessors.resolve__warn_implausible
_budget_record = _admission_accessors.resolve__budget_record
_emit = _admission_accessors.resolve__emit
_provider_of = _admission_accessors.resolve__provider_of
_meta_intent = _admission_accessors.resolve__meta_intent
_prompt_text = _admission_accessors.resolve__prompt_text
_output_text = _admission_accessors.resolve__output_text
_finish = _admission_accessors.resolve__finish
_stream_out_estimate = _admission_accessors.resolve__stream_out_estimate
_embed_per_item_max = _admission_accessors.resolve__embed_per_item_max
_usage_bearer = _admission_accessors.resolve__usage_bearer
_cached_in = _admission_accessors.resolve__cached_in
_cache_creation = _admission_accessors.resolve__cache_creation
_rt_reasoning_report = _admission_accessors.resolve__rt_reasoning_report
_tool_fee_count = _admission_accessors.resolve__tool_fee_count
_warn_unpriced_unit = _admission_accessors.resolve__warn_unpriced_unit
_log = _admission_accessors.resolve__log
budget_basis_estimate = _admission_accessors.resolve_budget_basis_estimate
budget_basis_billed = _admission_accessors.resolve_budget_basis_billed
_StreamProxy = _admission_accessors.resolve__StreamProxy
_AsyncStreamProxy = _admission_accessors.resolve__AsyncStreamProxy
_est_oai_embeddings = _admission_accessors.resolve__est_oai_embeddings()
_act_oai_chat = _admission_accessors.resolve__act_oai_chat()
_act_oai_resp = _admission_accessors.resolve__act_oai_resp()
_act_oai_embeddings = _admission_accessors.resolve__act_oai_embeddings()


def _now_day():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def realtime_spend_total():
    """Return the process-local realtime spend booked by this capture module."""
    with _rt_lock:
        return _rt_spent

def _rt_flush():
    global _rt_agg, _rt_since_flush
    with _rt_lock:
        if not _rt_agg:
            return
        items = list(_rt_agg.items()); _rt_agg = {}; _rt_since_flush = 0
    try:
        with open(RT_LOG, "a") as f:
            for (day, prov, mdl), v in items:
                calls, cost = v[0], v[1]
                in_tok, out_tok, cached = (v[2], v[3], v[4]) if len(v) >= 5 else (None, None, None)
                f.write(json.dumps({"day": day, "provider": prov, "model": mdl, "calls": calls,
                                    "cost": round(cost, 6), "in_tok": in_tok, "out_tok": out_tok,
                                    "cached_in_tok": cached}) + "\n")
    except Exception as e:
        # THE AGGREGATE WAS ALREADY EMPTIED ABOVE, so swallowing here DELETED real spend. These rows are
        # calls that were made and BILLED; the log is the only record the gate has of them, and a full disk
        # or a permissions change silently dropped every one — in the tool whose entire purpose is not
        # losing track of spend. Put them back so the next flush retries, and say it happened.
        with _rt_lock:
            for k, v in items:
                cur = _rt_agg.get(k)
                if cur is None:
                    _rt_agg[k] = list(v)
                else:
                    for i in range(min(len(cur), len(v))):
                        cur[i] = None if cur[i] is None or v[i] is None else cur[i] + v[i]
        _gate_warn_once(f"[spendguard] realtime spend log could not be written ({type(e).__name__}: "
                   f"{str(e)[:60]}) — {len(items)} aggregated row(s) HELD for the next flush, not dropped.")


def _rt_record(provider, model, cost, in_tok=0, out_tok=0, cached=0, basis=None, per_item_max=None):
    # LANE PARITY. The impossibility rail was wired into the two BATCH estimators only, which is exactly the
    # asymmetry that lets a bug live: realtime records from actual usage when the SDK returns it, but falls
    # back to the ESTIMATE when it doesn't (see _record_realtime) — the same path that produced the invented
    # $54.51. One request, so the bound is simply in_tok vs the model's context window. It also catches a
    # misread `usage` field, which no amount of estimator-fixing would. EMBEDDINGS pass per_item_max so the
    # bound is checked per ITEM (a batched list bills the sum but each input must fit the window, not the sum).
    _bad = _warn_implausible(model, int(in_tok or 0), 1, per_item_max=per_item_max) if in_tok else {}
    global _rt_spent, _rt_since_flush
    with _rt_lock:
        _rt_spent += cost
        k = (_now_day(), provider, pricing.normalize(model) if model else "?")
        a = _rt_agg.get(k, [0, 0.0, 0, 0, 0])
        a[0] += 1; a[1] += cost
        for telemetry_index, token_observation in enumerate((in_tok, out_tok, cached), start=2):
            a[telemetry_index] = (None if a[telemetry_index] is None or token_observation is None
                                  else a[telemetry_index] + int(token_observation))
        _rt_agg[k] = a
        _rt_since_flush += 1
        flush = _rt_since_flush >= _REALTIME_FLUSH_CALL_COUNT
    if flush:
        _rt_flush()
    _m = pricing.normalize(model) if model else "?"
    _budget_record(cost, _m, provider, "realtime", quarantine=bool(_bad),   # cross-process ledger (sqlite)
                   basis=basis or budget_basis_estimate(), in_tok=in_tok, out_tok=out_tok)
    _ev = {"kind": "realtime", "provider": provider, "model": _m, "cost": cost, "decision": "recorded",
           "in_tok": in_tok, "out_tok": out_tok, "cached_in_tok": cached}
    try:                       # the call's PURPOSE rides on the event (intent/chain)
        _cur = _call_corpus_accessors.resolve_calls_current()
        if (_cur.get("intent") or "").strip():
            _ev["intent"] = _cur["intent"].strip()
        if (_cur.get("chain") or "").strip():
            _ev["chain"] = _cur["chain"].strip()
    except Exception:
        pass
    _emit(_ev)


def _record_rt(model, kw, in_tok, out_tok, cached=0, latency=None, output=None, finish=None, cost=None,
               provider=None, basis=None, per_item_max=None, cache_creation=0, reasoning_tokens=None,
               cache_read_tok=None, cache_write_tok=None):
    """Record ONE realtime call's usage → cost · cross-process ledger · max_tokens truncation telemetry · call log.
    Shared by _rt_account (non-stream), the streaming proxy (ACTUAL usage as the stream is consumed), and the
    provider-breadth adapters (LiteLLM / Bedrock / Vertex). `cost` lets a caller supply an authoritative price (e.g.
    LiteLLM's own computed cost) instead of re-pricing; `provider` overrides the inferred ledger label.
    `cached` = cache-READ tokens (priced at the read discount); `cache_creation` = cache-WRITE tokens (priced at
    CACHE_WRITE_5M_MULTIPLIER) so a cache-heavy call is counted, never undercounted.
    The nullable cache_*_tok observations are separate from estimator defaults: only measured usage with a
    known write tier can support exact integrity reconstruction. Absent evidence stays a coverage gap."""
    prov = provider or _provider_of(model)
    _call_corpus_accessors.resolve_calls_check_output(output)      # output contracts are independent of monetary/token completeness
    # Normalize NEW split observations only; preserve raw legacy semantics on unmeasured rows. Anthropic
    # reports fresh input, OpenAI reports input inclusive of reads; writes are separate for both.
    call_input = (in_tok + cache_read_tok
                  if in_tok is not None and prov == "anthropic" and cache_read_tok is not None else in_tok)
    if (in_tok is None or out_tok is None) and cost is None:
        # Provider usage is partial: the input charge is a floor, never a settlement.
        in_for_floor = (in_tok + cached) if in_tok is not None and prov == "anthropic" else in_tok
        input_floor = None
        try:
            input_floor = pricing.input_cost_lower_bound(
                model, in_for_floor, provider=prov, cache_read_tok=cache_read_tok,
                cache_write_tok=cache_creation)
        except Exception as exc:
            _gate_warn_once(f"[spend_gate] incomplete output usage for {model}; input floor unavailable: {exc}")
        _call_corpus_accessors.resolve_calls_record_call(prov, model, "realtime", None, in_tok=call_input, out_tok=out_tok,
                           latency=latency, finish=finish, executor="api",
                           cache_read_tok=cache_read_tok, cache_write_tok=cache_write_tok,
                           input_cost_floor=input_floor)
        if input_floor is not None:
            if _meta_intent():
                _admission_accessors.resolve_budget_record_meta(prov, model, input_floor)
            else:
                _rt_record(prov, model, input_floor, in_tok=in_tok, out_tok=out_tok, cached=cached,
                           basis=basis, per_item_max=per_item_max)
        _gate_warn_once(f"[spend_gate] INCOMPLETE settlement for {model}: token count unreported; "
                        "only the known input floor is booked; total spend remains unknown")
        return
    if cost is None:
        # normalize to OpenAI token semantics (input INCLUDES cached) before pricing: Anthropic's input_tokens
        # EXCLUDES cache_read, so add it back or _cost double-subtracts and under-bills ~2x.
        in_for_cost = (in_tok + cached) if prov == "anthropic" else in_tok
        try:
            cost = pricing.realtime_cost(model, in_for_cost, out_tok, cached,
                                         cache_creation_tok=cache_creation) if model else 0.0
        except Exception:     # unpriced model → keep the TOKENS, refuse to invent a price, and MARK the row.
            # $0 and "we cannot price this" are different claims, and only the second is true. The row is
            # recorded with the UNPRICED marker: excluded from every total (so it never reads as free) and
            # SURFACED on the receipt with the one command that fixes it. A warning alone scrolls away; the
            # ledger then says $0 forever, which is the capture leak.
            cost = 0.0
            try:
                _admission_accessors.resolve_budget_record_unpriced(prov, model, "realtime", in_tok=in_tok, out_tok=out_tok)
            except Exception:
                pass
            _gate_warn_once(f"[spend_gate] NO PRICE for '{model}' — {in_tok}+{out_tok} tok recorded as UNPRICED "
                       f"(NOT $0, and excluded from totals so it cannot read as free). Fix it with:\n"
                       f"  spendguard price {model} --in <$/1M> --out <$/1M> --source '<url or invoice>'")
            return
    # BOOK the prompt-cache SAVING — a MEASURED counterfactual: the cache-READ tokens billed at the discount instead
    # of full input, NET of the one-time cache-WRITE premium — into the guarded-savings ledger under a DISTINCT
    # 'prompt_cache' source, so `spendguard receipt` / `savings` / the MCP surface it (reconciled to real usage).
    # NOT semcache's 'cache' (which avoids the whole call; this discounts a call that still happens). Rates from
    # pricing.py only (read `cached_in`, write CACHE_WRITE_5M_MULTIPLIER) — never a hardcoded discount.
    if model and (cached or cache_creation):
        try:
            _pp = pricing.price(model)
            _pin = _pp.get("in_") or 0.0
            _prd = _pp.get("cached_in")
            _saved = 0.0
            if cached and _prd is not None:
                _saved += cached * (_pin - _prd)                                     # reads at the discount, not full
            if cache_creation:
                _saved -= cache_creation * _pin * (pricing.CACHE_WRITE_5M_MULTIPLIER - 1)   # the write premium (offset)
            if _saved > 0:
                _admission_accessors.resolve_guard_record_saving("prompt_cache", _saved / 1_000_000)
        except Exception:
            pass
    # THE EFFORT TIER ACTUALLY SENT — read off the request body (a fixed field, not a judgement), so the calls
    # corpus can slice cost×quality per (intent, model, effort). None when no reasoning_effort rode the request.
    _effort = kw.get("reasoning_effort") if isinstance(kw, dict) else None
    if _effort is None:
        # A MEASUREMENT call that sent NO reasoning_effort (a non-reasoning model has none to send) must not record an
        # ambiguous NULL — a replicate then can't be told from an effort change. Record the EXPLICIT sentinel: the
        # model ran at the provider default. Only for a measurement (the context flag); production NULLs are unchanged.
        try:
            if (_call_corpus_accessors.resolve_calls_current() or {}).get("measurement"):
                _effort = MEASUREMENT_EFFORT_DEFAULT
        except Exception:
            pass
    if _meta_intent():                            # meta call → meta ledger only (not workload realtime)
        _admission_accessors.resolve_budget_record_meta(prov, model, cost)
        if _call_corpus_accessors.resolve_calls_call_recording_consent_enabled():
            _call_corpus_accessors.resolve_calls_record_call(prov, model, "realtime", cost, in_tok=call_input, out_tok=out_tok, latency=latency,
                          prompt=_prompt_text(kw), output=output, finish=finish, effort=_effort, executor="api",
                          cache_read_tok=cache_read_tok, cache_write_tok=cache_write_tok)
        return
    _rt_record(prov, model, cost, in_tok=in_tok, out_tok=out_tok, cached=cached, basis=basis, per_item_max=per_item_max)
    try:                                              # max_tokens TRUNCATION detection (a fact) + per-sig telemetry
        _intent = (_call_corpus_accessors.resolve_calls_current().get("intent") or "").strip()
        if not _intent.startswith("spendguard:"):
            _admission_accessors.resolve_bulkgate_note_response(
                                   _admission_accessors.resolve_bulkgate_sig(model or "", template_id=_intent or None),
                                   model or "", out_tok,
                                   kw.get("max_tokens") or kw.get("max_completion_tokens") or kw.get("max_output_tokens"),
                                   finish, reasoning_tokens=reasoning_tokens,
                                   visible_tokens=max(0, out_tok - reasoning_tokens) if reasoning_tokens is not None else None)
    except Exception:
        pass
    if _call_corpus_accessors.resolve_calls_call_recording_consent_enabled():
        _call_corpus_accessors.resolve_calls_record_call(prov, model, "realtime", cost, in_tok=call_input, out_tok=out_tok, latency=latency,
                      prompt=_prompt_text(kw), output=output, finish=finish, effort=_effort,
                      cache_read_tok=cache_read_tok, cache_write_tok=cache_write_tok,
                      # EXPLICIT 'api' (not NULL): this is the metered realtime path. A lane call records via the
                      # adapters subscription path with executor=<lane>, so stamping 'api' here makes EVERY row
                      # classify $0-lane vs cash-metered with no guessing (legacy NULL rows read as metered too).
                      executor="api")


def _rt_account(model, kw, result, est_fn, act_fn, latency=None, record_rt=None):
    """Record a realtime call's spend from the provider's ACTUAL usage (act_fn) when available — INCLUDING a completed
    stream, whose final message the stream proxy (_GatedStreamManager.record) hands us here → billed at the real count.
    Only a call with no usable provider usage falls back to the estimate; a stream that reported none is the projection."""
    recorder = record_rt or _record_rt
    try:
        act = act_fn(result) if result is not None else None
        if act:
            # ACTUAL provider usage. This INCLUDES a COMPLETED stream, whose final message the stream proxy
            # (_GatedStreamManager.record) hands us here — a finished stream is BILLED at the provider's real count.
            # The old `if kw.get("stream"): estimate` FIRST discarded the very usage the proxy passed, so EVERY streamed
            # call recorded a projection instead of the truth (and the cached/output/finish fields below were lost too).
            in_tok, out_tok = act
            basis = budget_basis_billed()
        elif kw.get("stream"):
            # a stream with NO usable usage (the wrap FAILED, or the final message carried none) → OUR projection, said so.
            in_tok, out_tok = _stream_out_estimate(model, kw, est_fn)
            recorder(model, kw, in_tok, out_tok, 0, latency, basis=budget_basis_estimate())
            return
        else:
            _, in_tok, out_tok = est_fn(kw)                                  # non-stream, no provider usage → estimate
            basis = budget_basis_estimate()
        # EMBEDDINGS bill the SUM of a LIST input but the context window bounds each ITEM — pass the largest single
        # input so the impossibility rail checks per-item, not the sum (a 1000-string batch is legitimate).
        pim = _embed_per_item_max(kw) if est_fn is _est_oai_embeddings else None
        # Keep absence distinct from a reported zero. Positive Anthropic writes need the provider's TTL
        # breakdown: one-hour/mixed/unknown writes cannot be verified using the five-minute price axis.
        cache_read_tok = cache_write_tok = None
        if act:
            usage = getattr(_usage_bearer(result), "usage", None)
            cache_read_tok = getattr(usage, "cache_read_input_tokens", None)
            for details_field in ("prompt_tokens_details", "input_tokens_details"):
                details = getattr(usage, details_field, None)
                if details is not None:
                    cache_read_tok = getattr(details, "cached_tokens", None)
            creation = getattr(usage, "cache_creation_input_tokens", None)
            if creation == 0:
                cache_write_tok = creation
            elif creation is not None:
                tiers = getattr(usage, "cache_creation", None)
                if (getattr(tiers, "ephemeral_1h_input_tokens", None) == 0
                        and getattr(tiers, "ephemeral_5m_input_tokens", None) == creation):
                    cache_write_tok = creation
            elif cache_read_tok is not None and act_fn in (_act_oai_chat, _act_oai_resp, _act_oai_embeddings):
                cache_write_tok = 0  # OpenAI automatic caching has no separate cache-write charge.
        bearer = _usage_bearer(result)
        recorder(model, kw, in_tok, out_tok, _cached_in(bearer), latency, _output_text(result), _finish(result),
                   basis=basis, per_item_max=pim, cache_creation=_cache_creation(bearer), reasoning_tokens=_rt_reasoning_report(result),
                   cache_read_tok=cache_read_tok, cache_write_tok=cache_write_tok)
        _record_tool_fees(model, kw, result, record_rt=recorder)
    except Exception as e:
        print(f"[spend_gate] WARN real-time accounting failed ({e})", file=sys.stderr)


def _record_tool_fees(model, kw, result, record_rt=None):
    """Record per-call tool fees as their own ledger entry (a SECOND row — fees are not tokens and
    hiding them inside a token row would be unauditable). Unpriced → $0 + loud warn, never silent."""
    n = _tool_fee_count(result)
    if n is None:
        # UNKNOWN IS NOT ZERO, one level up too. `if not n` treated a failed count and a genuine no-fee
        # response identically, which put the collapse straight back after _tool_fee_count stopped making
        # it. The warning already fired there; recording nothing here is the only available action, but the
        # two cases must not read the same in the code either.
        return
    if n == 0:
        return
    try:
        fee = n * pricing.unit_price("web_search_call", model)
    except KeyError:
        _warn_unpriced_unit("web_search_call", model)
        fee = 0.0
    (record_rt or _record_rt)(model, kw, 0, 0, cost=fee)


def _pull_usage(chunk, acc):
    """Accumulate token usage from ONE streaming chunk — OpenAI chat's final chunk `.usage` (needs stream_options
    include_usage), OpenAI Responses' `.response.usage`, Anthropic's message_start (input) + message_delta (output) —
    plus the finish/stop reason. Best-effort: unknown chunk shapes are ignored (fail-open)."""
    try:
        reported = _rt_reasoning_report(getattr(chunk, "response", None) or chunk)
        if reported is not None:
            acc["reasoning_tokens"] = reported
        response = getattr(chunk, "response", None)
        if response is not None and _finish(response):
            acc["finish"] = _finish(response)
        u = getattr(chunk, "usage", None)                    # OpenAI chat final chunk / Anthropic message_delta
        if u is not None:
            for src, dst in (("prompt_tokens", "in"), ("input_tokens", "in"),
                             ("completion_tokens", "out"), ("output_tokens", "out")):
                v = getattr(u, src, None)
                if v is not None:
                    acc[dst] = int(v)
            for dattr in ("prompt_tokens_details", "input_tokens_details"):
                d = getattr(u, dattr, None)
                if d is not None and getattr(d, "cached_tokens", None) is not None:
                    acc["cached"] = int(d.cached_tokens)
            if getattr(u, "cache_read_input_tokens", None) is not None:
                acc["cached"] = int(u.cache_read_input_tokens)
            if getattr(u, "cache_creation_input_tokens", None):
                acc["cache_creation"] = int(u.cache_creation_input_tokens)
        m = getattr(chunk, "message", None)                  # Anthropic message_start carries input usage
        mu = getattr(m, "usage", None) if m is not None else None
        if mu is not None:
            if getattr(mu, "input_tokens", None):
                acc["in"] = int(mu.input_tokens)
            if getattr(mu, "output_tokens", None) is not None:
                acc["out"] = int(mu.output_tokens)
            if getattr(mu, "cache_read_input_tokens", None) is not None:  # Anthropic message.usage
                acc["cached"] = int(mu.cache_read_input_tokens)
            if getattr(mu, "cache_creation_input_tokens", None):
                acc["cache_creation"] = int(mu.cache_creation_input_tokens)
        resp = getattr(chunk, "response", None)              # OpenAI Responses API: final event .response.usage
        ru = getattr(resp, "usage", None) if resp is not None else None
        if ru is not None:
            if getattr(ru, "input_tokens", None):
                acc["in"] = int(ru.input_tokens)
            if getattr(ru, "output_tokens", None) is not None:
                acc["out"] = int(ru.output_tokens)
            details = getattr(ru, "input_tokens_details", None)
            if details is not None and getattr(details, "cached_tokens", None) is not None:
                acc["cached"] = int(details.cached_tokens)
        ch = getattr(chunk, "choices", None)                 # OpenAI finish_reason
        if ch and getattr(ch[0], "finish_reason", None):
            acc["finish"] = ch[0].finish_reason
        d = getattr(chunk, "delta", None)                    # Anthropic message_delta stop_reason
        sr = getattr(chunk, "stop_reason", None) or (getattr(d, "stop_reason", None) if d is not None else None)
        if sr:
            acc["finish"] = sr
    except Exception:
        pass


def _observe_stream(stream, model, kw, est_fn, t0, is_async, record_rt=None):
    """Wrap a streaming response in a TRANSPARENT proxy that passes every chunk through, captures usage as it streams,
    and on exhaustion records the ACTUAL usage (or the estimate if none was emitted) — 'capture as it happens'.
    Returns the proxy; raises only if a proxy can't be built (caller falls back to the estimate). Fail-open inside."""
    acc = {}
    recorder = record_rt or _record_rt

    def _done():
        try:
            if "in" in acc or "out" in acc:
                recorder(model, kw, acc.get("in", 0), acc.get("out"), acc.get("cached", 0),
                           time.time() - t0, finish=acc.get("finish"), basis=budget_basis_billed(),
                           cache_creation=acc.get("cache_creation", 0), reasoning_tokens=acc.get("reasoning_tokens"),
                           cache_read_tok=acc.get("cached"))
            else:                                            # usage not emitted (e.g. include_usage off) → estimate
                in_tok, out_tok = _stream_out_estimate(model, kw, est_fn)
                recorder(model, kw, in_tok, out_tok, 0, time.time() - t0, basis=budget_basis_estimate())
        except Exception as e:
            # FAIL-OPEN, never fail-silent: a broken recorder must not break the user's stream, but a stream
            # whose spend was dropped has to say so — otherwise realtime accounting can go quietly to zero.
            print(f"[spend_gate] WARN stream accounting FAILED ({type(e).__name__}: {e}) — this call's "
                  f"realtime spend was NOT recorded", file=sys.stderr)

    return (_AsyncStreamProxy if is_async else _StreamProxy)(stream, acc, _done)


def realtime_by_day(since=None):
    """Real-time $ by day and by model, from the gate's log. Flushes in-process first."""
    _rt_flush()
    by_day, by_model = {}, {}
    if not os.path.exists(RT_LOG):
        return by_day, by_model
    for ln in open(RT_LOG):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        d = r.get("day", "")
        if since and d < since:
            continue
        by_day[d] = by_day.get(d, 0.0) + r.get("cost", 0.0)
        m = r.get("model", "?"); by_model[m] = by_model.get(m, 0.0) + r.get("cost", 0.0)
    return by_day, by_model


def _act_images(kw, result, record_rt=None):
    model = kw.get("model") or ""
    n = len(getattr(result, "data", None) or []) or int(kw.get("n") or 1)
    variant = f"{kw.get('size') or ''}:{kw.get('quality') or ''}".strip(":")
    try:
        cost = n * pricing.unit_price("image", model, variant or None)
    except KeyError:
        _warn_unpriced_unit("image", model)
        cost = 0.0
    (record_rt or _record_rt)(model, kw, 0, 0, cost=cost, provider="openai")


def _act_transcription(kw, result, record_rt=None):
    model = kw.get("model") or ""
    u = getattr(result, "usage", None)               # gpt-4o-transcribe bills TOKENS and reports usage
    if u is not None and getattr(u, "input_tokens", None) is not None:
        (record_rt or _record_rt)(model, kw, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", None),
                   provider="openai")
        return
    dur = getattr(result, "duration", None)          # whisper verbose_json reports seconds
    if dur:
        try:
            cost = float(dur) * pricing.unit_price("audio_second", model)
        except KeyError:
            _warn_unpriced_unit("audio_second", model)
            cost = 0.0
        (record_rt or _record_rt)(model, kw, 0, 0, cost=cost, provider="openai")
        return
    print(f"[spend_gate] WARN transcription on '{model}' returned neither usage nor duration — recorded "
          f"at $0 (request response_format=verbose_json so seconds are billable-visible)", file=sys.stderr)
    (record_rt or _record_rt)(model, kw, 0, 0, cost=0.0, provider="openai")


def _act_speech(kw, result, record_rt=None):
    model = kw.get("model") or ""
    chars = len(kw.get("input") or "")               # deterministic: TTS bills the input characters
    try:
        cost = chars * pricing.unit_price("tts_char", model)
    except KeyError:
        _warn_unpriced_unit("tts_char", model)
        cost = 0.0
    (record_rt or _record_rt)(model, kw, 0, 0, cost=cost, provider="openai")


def _act_finetune(kw, result):
    """A fine-tune job's training cost lands at RECONCILE (provider billing) — the submission itself is
    recorded LOUDLY so an unestimated paid operation is never invisible between now and then."""
    model = kw.get("model") or ""
    job_id = getattr(result, "id", None) or ""
    print(f"[spend_gate] fine-tune job {job_id or '(id unknown)'} on '{model}' submitted — training cost is "
          f"UNESTIMATED here and will land at reconcile from provider billing.", file=sys.stderr)
    _log({"kind": "finetune_job", "provider": "openai", "model": model, "job": job_id,
          "decision": "unestimated_submission"})

_atexit.register(_rt_flush)

_admission_accessors.set__rt_flush_resolver(_rt_flush)
_admission_accessors.set__now_day_resolver(_now_day)
_admission_accessors.set__rt_record_resolver(_rt_record)
_admission_accessors.set__record_rt_resolver(_record_rt)
_admission_accessors.set__rt_account_resolver(_rt_account)
_admission_accessors.set__record_tool_fees_resolver(_record_tool_fees)
_admission_accessors.set__pull_usage_resolver(_pull_usage)
_admission_accessors.set__observe_stream_resolver(_observe_stream)
_admission_accessors.set_realtime_by_day_resolver(realtime_by_day)
_admission_accessors.set__act_images_resolver(_act_images)
_admission_accessors.set__act_transcription_resolver(_act_transcription)
_admission_accessors.set__act_speech_resolver(_act_speech)
_admission_accessors.set__act_finetune_resolver(_act_finetune)
_admission_accessors.set_realtime_spend_total_resolver(realtime_spend_total)
