"""spend_gate — global pre-submit cost gate for BOTH OpenAI and Anthropic batches.

Installed via the venv's sitecustomize.py, so EVERY script run in this venv (all 211
submitters + future ones) is gated with zero per-script edits. It estimates each
batch's cost from its content (canonical pricing.py), logs every submission, and
HARD-STOPS a single batch projected over the cap — then, if interactive, asks
whether to allow it anyway (so you can override and "find a better way" next time).

Design for safety on a live submit path:
  * FAIL-OPEN on any internal/estimation error — never breaks a job by accident.
  * Only the deliberate over-cap stop blocks (raises SpendGateRefused).
  * Env knobs (read per-call, so tunable per process):
      GATE_CAP=<dollars>   per-batch cap (default 75)
      GATE_DISABLE=1       skip the gate entirely
      GATE_ALLOW=1         allow over-cap without prompting (intentional big runs / non-interactive)
  * Conservative estimate: input via tiktoken, output via each request's max_tokens
    ceiling -> over-estimates -> fails safe.

Audit trail: data/spend_audit/gate_log.jsonl (one line per submission, with decision).
"""
import os, sys, json, functools, datetime, time

from . import pricing
from . import calls as _calls
from .config import LOG, FLAG, cap as _cap, disabled as _disabled, allow as _allow
from .emit import emit as _emit


def _prompt_text(kw):
    parts = []
    if kw.get("system"):
        parts.append(str(kw["system"]))
    for m in (kw.get("messages") or []):
        c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        parts.append(c if isinstance(c, str) else json.dumps(c, default=str))
    return "\n".join(parts)


def _output_text(result):
    try:
        ch = getattr(result, "choices", None)
        if ch:
            return ch[0].message.content or ""
        cont = getattr(result, "content", None)
        if cont:
            return "".join(getattr(b, "text", "") for b in cont if getattr(b, "type", None) == "text")
    except Exception:
        pass
    return ""


def _finish(result):
    try:
        ch = getattr(result, "choices", None)
        if ch:
            return getattr(ch[0], "finish_reason", None)
        return getattr(result, "stop_reason", None)
    except Exception:
        return None


def _call_orig(orig, self, a, kw):
    """Run the REAL SDK call with the raw-HTTP layer suppressed — the SDKs ride httpx underneath, and
    without this flag http_capture would record every gated SDK call a second time."""
    from . import http_capture
    tok = http_capture.in_sdk_call.set(True)
    try:
        return orig(self, *a, **kw)
    finally:
        http_capture.in_sdk_call.reset(tok)


async def _call_orig_async(orig, self, a, kw):
    from . import http_capture
    tok = http_capture.in_sdk_call.set(True)
    try:
        return await orig(self, *a, **kw)
    finally:
        http_capture.in_sdk_call.reset(tok)


class SpendGateRefused(RuntimeError):
    """Raised to block a submission the user/policy declined. Propagates out of the SDK call."""


def _ct(text):
    try:
        import tiktoken
        return len(tiktoken.get_encoding("o200k_base").encode(text))
    except Exception:
        return max(1, len(text) // 4)


def _content_tokens(content):
    if isinstance(content, str):
        return _ct(content)
    try:
        return _ct(json.dumps(content, default=str))
    except Exception:
        return _ct(str(content))


def _estimate_openai_jsonl(data: bytes):
    in_tok = out = n = 0
    model = None
    for line in data.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        n += 1
        body = json.loads(line).get("body", {})
        model = model or body.get("model")
        for m in body.get("messages", []):
            in_tok += _content_tokens(m.get("content", ""))
        if not body.get("messages") and body.get("input") is not None:
            # embeddings (and Responses-style) batch bodies carry `input`, not `messages` — these used
            # to estimate as $0, so the cap could never see an embeddings batch coming.
            inp = body["input"]
            for item in (inp if isinstance(inp, list) else [inp]):
                if isinstance(item, str):
                    in_tok += _ct(item)
                elif isinstance(item, list):
                    in_tok += len(item)              # pre-tokenized int array
                elif isinstance(item, int):
                    in_tok += 1
        out += body.get("max_tokens", body.get("max_completion_tokens", 0)) or 0
    cost = pricing.batch_cost(model, in_tok, out) if model else 0.0
    return dict(provider="openai", model=model, requests=n, in_tok=in_tok, out_tok=out, cost=cost)


def _estimate_anthropic_requests(requests):
    in_tok = out = n = 0
    model = None
    for r in requests:
        n += 1
        params = r.get("params") if isinstance(r, dict) else getattr(r, "params", None)
        if params is None:
            continue
        g = (params.get if isinstance(params, dict) else (lambda k, d=None, _p=params: getattr(_p, k, d)))
        model = model or g("model")
        sysp = g("system")
        if sysp:
            in_tok += _content_tokens(sysp)
        for m in (g("messages") or []):
            c = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
            in_tok += _content_tokens(c)
        out += (g("max_tokens") or 0)
    cost = pricing.batch_cost(model, in_tok, out) if model else 0.0
    return dict(provider="anthropic", model=model, requests=n, in_tok=in_tok, out_tok=out, cost=cost)


def _log(rec):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        rec = dict(rec); rec["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        with open(LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
    _emit({**rec, "kind": rec.get("kind", "batch")})


def _submit_receipt(est):
    """After the gate APPROVES a batch, show the running tally next to the estimate — the per-flow receipt for a
    submit (the gate already printed the est). Verbosity-gated, stderr, fully guarded: must NEVER affect the gate's
    decision or raise. The batch's ACTUAL cost trues up later at reconcile; this shows the est + the billed tally."""
    try:
        from . import receipt
        if receipt.level() in ("flow", "verbose"):
            print(receipt.render_tally(), file=sys.stderr)
    except Exception:
        pass


def _decide(est):
    """Proceed (return) if under cap or allowed; raise SpendGateRefused to block."""
    cap = _cap()
    line = (f"[spend_gate] {est['provider']} {est.get('model')} · {est['requests']} req · "
            f"in~{est['in_tok']:,} out≤{est['out_tok']:,} -> ~${est['cost']:.2f} (cap ${cap:.0f})")
    if est["cost"] <= cap:
        _log({**est, "decision": "under_cap"}); print(line + "  OK", file=sys.stderr); _submit_receipt(est); return
    if _allow():
        _log({**est, "decision": "allowed_env"}); print(line + "  ALLOWED (GATE_ALLOW=1)", file=sys.stderr); _submit_receipt(est); return
    print(f"\n*** SPEND GATE: this single batch is projected at ${est['cost']:.2f}, over the ${cap:.0f} cap. ***\n"
          f"{line}\nBetter first: pack 25–40 items/request · trim max_tokens · use the cheaper executor "
          f"(opus-4.8 output < gpt-5.5) · split the scope. (raise GATE_CAP or GATE_ALLOW=1 to force.)", file=sys.stderr)
    if sys.stdin and sys.stdin.isatty():
        try:
            ans = input(f"Allow this ${est['cost']:.2f} submission anyway? type 'yes' to proceed: ").strip().lower()
        except Exception:
            ans = ""
        if ans in ("yes", "y"):
            _log({**est, "decision": "allowed_prompt"}); _submit_receipt(est); return
        _log({**est, "decision": "refused_prompt"})
        from . import guard
        guard.record_saving("block", est["cost"])     # guarded: a blocked submission's spend, prevented
        raise SpendGateRefused(f"submission refused at gate (${est['cost']:.2f} > ${cap:.0f})")
    _log({**est, "decision": "refused_noninteractive"})
    from . import guard
    guard.record_saving("block", est["cost"])         # guarded: a blocked submission's spend, prevented
    raise SpendGateRefused(
        f"submission ${est['cost']:.2f} > cap ${cap:.0f} (non-interactive). "
        f"Set GATE_ALLOW=1 to permit this run, raise GATE_CAP, or pack/trim/cheaper-model.")


def _read_filelike(file):
    """Read bytes from an OpenAI files.create `file` arg WITHOUT consuming the caller's stream."""
    if hasattr(file, "read"):
        pos = file.tell() if hasattr(file, "tell") else None
        data = file.read()
        if pos is not None and hasattr(file, "seek"):
            file.seek(pos)
        return data if isinstance(data, bytes) else str(data).encode()
    if isinstance(file, (bytes, bytearray)):
        return bytes(file)
    if isinstance(file, (tuple, list)) and len(file) >= 2:
        f1 = file[1]
        return bytes(f1) if isinstance(f1, (bytes, bytearray)) else open(f1, "rb").read()
    if isinstance(file, str):
        return open(file, "rb").read()
    raise TypeError(f"unhandled file type {type(file)}")


def _budget_check(cost, model, provider, kind):
    """Cross-process daily/monthly cap (only when budget.backend=sqlite). Hard stop — but ASK first
    when interactive (same as the per-batch cap); GATE_ALLOW=1 skips the prompt for big intentional runs."""
    from . import config
    if config.budget_backend() != "sqlite" or _allow():
        return
    from . import budget
    ex = budget.exceeded(cost, kind="llm")          # the gate governs LLM calls; checks the LLM sub-cap + total ceiling
    if not ex:
        return
    w, capv, proj = ex
    print(f"\n*** SPEND GATE: this call would push {w} spend to ${proj:.2f}, over the ${capv:.0f} {w} cap. ***",
          file=sys.stderr)
    if sys.stdin and sys.stdin.isatty():
        try:
            ans = input(f"Allow it anyway (over the {w} cap)? type 'yes' to proceed: ").strip().lower()
        except Exception:
            ans = ""
        if ans in ("yes", "y"):
            _emit({"kind": kind, "provider": provider, "model": model, "cost": cost, "decision": f"allowed_prompt_{w}"})
            return
    _emit({"kind": kind, "provider": provider, "model": model, "cost": cost, "decision": f"refused_{w}"})
    raise SpendGateRefused(f"{w} budget ${capv:.0f} would be exceeded (projected ${proj:.2f}). "
                           f"Raise caps.{w.replace('-', '.')}, or set GATE_ALLOW=1.")


def _budget_record(cost, model, provider, kind):
    from . import config
    if config.budget_backend() == "sqlite":
        from . import budget
        budget.record(provider, model, kind, cost)


def _meta_intent():
    """True if the current context intent is spendguard's own (spendguard:*) — segregated budget+tracking."""
    try:
        return (_calls.current().get("intent") or "").startswith("spendguard:")
    except Exception:
        return False


def _meta_gate(cost, model, provider):
    """Enforce the SEPARATE meta cap + record to the meta ledger. Returns True if handled (meta)."""
    if not _meta_intent():
        return False
    from . import budget, config
    if not _allow():
        ex = budget.meta_exceeded(cost)
        if ex:
            _emit({"kind": "meta", "provider": provider, "model": model, "cost": cost, "decision": "refused_meta"})
            raise SpendGateRefused(f"spendguard meta budget ${config.meta_cap():.0f}/day would be exceeded "
                                   f"(projected ${ex[2]:.2f}). Raise caps.meta or set GATE_ALLOW=1.")
    budget.record_meta(provider, model, cost)
    return True


def _on(name):
    return (os.getenv(name) or "").lower() in ("1", "true", "yes", "on")


def _batch1_check(est):
    """Mechanize the batch-1 discipline. Before a LARGE batch for an intent that has NO recent small/realtime test
    of the same shape, WARN (and prompt if interactive) — or hard-refuse with GATE_REQUIRE_BATCH1. The #1 batch
    waste is a prompt/tool bug a 1–5 item realtime test would have caught for ~$0; the cost cap can't see it (a
    buggy-but-cheap batch passes). Heuristic + opt-out, so it never breaks a legit job by default.

    Knobs: GATE_BATCH1_MIN (req count that counts as 'large', default 50) · GATE_BATCH1_USD (or ≥ this $, default 5)
    · GATE_BATCH1_DAYS (look-back, default 14) · GATE_REQUIRE_BATCH1 (refuse non-interactive) · GATE_NO_BATCH1 (off).
    GATE_ALLOW=1 bypasses (like the cost cap). Needs the calls corpus + an intent to reason about 'shape'."""
    if _on("GATE_NO_BATCH1") or _allow() or not _calls.enabled():
        return
    try:
        intent = (_calls.current().get("intent") or "").strip()
    except Exception:
        intent = ""
    if not intent or intent.startswith("spendguard:"):
        return                                                        # no intent to key on / our own meta → skip
    n = int(est.get("requests", 0) or 0)
    cost = float(est.get("cost", 0) or 0)
    if n < int(os.getenv("GATE_BATCH1_MIN", "50") or 50) and cost < float(os.getenv("GATE_BATCH1_USD", "5") or 5):
        return                                                        # not a 'large' batch → nothing to gate
    days = int(os.getenv("GATE_BATCH1_DAYS", "14") or 14)
    if _calls.tested_recently(intent, None, days):                    # any realtime test of this intent counts
        return                                                        # (a prompt/tool bug shows on any model)
    prov, mdl = est.get("provider"), est.get("model")
    print(f"\n*** [spend_gate] BATCH-1 CHECK: a {n:,}-request '{intent}' batch ({mdl}, ~${cost:.2f}) with NO "
          f"realtime/batch-1 test of this intent in the last {days}d. The #1 batch waste is a prompt/tool bug a "
          f"1–5 item realtime test catches for ~$0 — run that first (PROMPT-CHECK → batch-1). ***", file=sys.stderr)
    if sys.stdin and sys.stdin.isatty():
        try:
            ans = input("Submit the full batch anyway? type 'yes' to proceed: ").strip().lower()
        except Exception:
            ans = ""
        if ans in ("yes", "y"):
            _emit({"kind": "batch", "provider": prov, "model": mdl, "cost": cost, "decision": "batch1_override_prompt"})
            return
        _emit({"kind": "batch", "provider": prov, "model": mdl, "cost": cost, "decision": "batch1_refused"})
        raise SpendGateRefused("batch-1 check: refused — test this intent on a few items (realtime) first, "
                               "or set GATE_ALLOW=1 / GATE_NO_BATCH1=1.")
    if _on("GATE_REQUIRE_BATCH1"):
        _emit({"kind": "batch", "provider": prov, "model": mdl, "cost": cost, "decision": "batch1_refused_strict"})
        raise SpendGateRefused("batch-1 check (GATE_REQUIRE_BATCH1): large batch for an intent with no prior "
                               "realtime/batch-1 test. Run a small test first, or GATE_ALLOW=1 / GATE_NO_BATCH1=1.")
    _emit({"kind": "batch", "provider": prov, "model": mdl, "cost": cost, "decision": "batch1_warned"})  # warn, allow


def _bulkgate_check(est):
    """TEST-FIRST + ESTIMATE-FIRST enforcement (defense-in-depth, universal — every installed-hook consumer gets it).
    For a bulk batch (> preview_max requests), require a FRESH estimate + verified test for its call-class sig, else
    block. Sig = the consumer's stamped est['sg_sig'] if present, else a fallback from model + intent. Default mode is
    `warn` (logs would-block, allows) so it doesn't break consumers on day one; SPENDGUARD_ENFORCE=block to enforce.
    Fail-OPEN on any internal error (never break a legit job because the gate itself hiccuped)."""
    from . import bulkgate
    try:
        n = int(est.get("requests", 0) or 0)
        if n <= bulkgate.preview_max():
            return
        intent = ""
        try:
            intent = (_calls.current().get("intent") or "").strip()
        except Exception:
            pass
        if intent.startswith("spendguard:"):
            return                                                       # our own meta calls — not workload bulk
        model = est.get("model") or ""
        s = est.get("sg_sig") or bulkgate.sig(model, template_id=intent or None, prompt=est.get("prompt_sample"))
        bulkgate.check_bulk(s, model, n, float(est.get("cost", 0) or 0))  # warn→log+allow; block→raise GateBlocked
    except bulkgate.GateBlocked:
        raise                                                            # a real block — propagate (stops the batch)
    except Exception as e:
        print(f"[spend_gate] WARN bulkgate check failed ({e}); allowing (fail-open)", file=sys.stderr)


def _decide_and_account(est):
    if _meta_gate(est["cost"], est.get("model"), est.get("provider")):   # spendguard's own use → meta cap
        return
    _batch1_check(est)            # batch-1 discipline: warn/refuse a LARGE batch for an untested intent (may raise)
    _bulkgate_check(est)          # estimate+test-first: sig-keyed flags, blocks an unestimated/untested bulk (may raise)
    _budget_check(est["cost"], est.get("model"), est.get("provider"), "batch")   # daily/monthly (sqlite)
    _decide(est)                                                                  # per-batch cap (may raise)
    _budget_record(est["cost"], est.get("model"), est.get("provider"), "batch")  # ledger (sqlite)
    if _calls.enabled():                                                          # job-level call-context row
        _calls.record(est.get("provider"), est.get("model"), "batch", est["cost"],
                      in_tok=est.get("in_tok", 0), out_tok=est.get("out_tok", 0))


def _gate_openai_files(kw, args=()):
    if kw.get("purpose") != "batch":
        return
    try:
        data = _read_filelike(kw.get("file"))
        est = _estimate_openai_jsonl(data)
    except SpendGateRefused:
        raise
    except Exception as e:
        print(f"[spend_gate] WARN openai estimate failed ({e}); allowing (fail-open)", file=sys.stderr)
        return
    _decide_and_account(est)  # per-batch cap + cross-process daily/monthly (sqlite)


def _gate_anthropic(kw, args=()):
    reqs = kw.get("requests")
    if reqs is None and args:
        reqs = args[0]
    if reqs is None:
        return
    try:
        est = _estimate_anthropic_requests(list(reqs))
    except SpendGateRefused:
        raise
    except Exception as e:
        print(f"[spend_gate] WARN anthropic estimate failed ({e}); allowing (fail-open)", file=sys.stderr)
        return
    _decide_and_account(est)  # per-batch cap + cross-process daily/monthly (sqlite)


# ─────────────────────────── REAL-TIME cumulative budget ───────────────────────────
# Real-time cost can't be known before the call (output tokens), so this layer ACCOUNTS
# actual usage AFTER each call (and logs it → closes the "real-time spend is invisible to
# reconcile" gap), and HARD-STOPS before the next call once per-process cumulative spend
# crosses GATE_RT_BUDGET (default $50). The runaway-loop protection (e.g. the 47,771-call balloon).
import threading as _threading, atexit as _atexit
from .config import RT_LOG, rt_budget as _rt_budget

_rt_lock = _threading.Lock()
_rt_spent = 0.0          # per-process cumulative real-time $
_rt_agg = {}             # (day, provider, model) -> [calls, cost]  pending flush
_rt_since_flush = 0
_rt_warned = False
_rt_bypass = False        # interactive "allow rest of run's real-time calls" — bypasses ONLY the RT budget


def _now_day():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


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
                in_tok, out_tok, cached = (v[2], v[3], v[4]) if len(v) >= 5 else (0, 0, 0)
                f.write(json.dumps({"day": day, "provider": prov, "model": mdl, "calls": calls,
                                    "cost": round(cost, 6), "in_tok": in_tok, "out_tok": out_tok,
                                    "cached_in_tok": cached}) + "\n")
    except Exception:
        pass


_atexit.register(_rt_flush)


def _rt_record(provider, model, cost, in_tok=0, out_tok=0, cached=0):
    global _rt_spent, _rt_since_flush
    with _rt_lock:
        _rt_spent += cost
        k = (_now_day(), provider, pricing.normalize(model) if model else "?")
        a = _rt_agg.get(k, [0, 0.0, 0, 0, 0])
        a[0] += 1; a[1] += cost; a[2] += int(in_tok or 0); a[3] += int(out_tok or 0); a[4] += int(cached or 0)
        _rt_agg[k] = a
        _rt_since_flush += 1
        flush = _rt_since_flush >= 200
    if flush:
        _rt_flush()
    _m = pricing.normalize(model) if model else "?"
    _budget_record(cost, _m, provider, "realtime")   # cross-process ledger (sqlite backend)
    _ev = {"kind": "realtime", "provider": provider, "model": _m, "cost": cost, "decision": "recorded",
           "in_tok": in_tok, "out_tok": out_tok, "cached_in_tok": cached}
    try:                       # the call's PURPOSE rides on the event (intent/chain)
        _cur = _calls.current()
        if (_cur.get("intent") or "").strip():
            _ev["intent"] = _cur["intent"].strip()
        if (_cur.get("chain") or "").strip():
            _ev["chain"] = _cur["chain"].strip()
    except Exception:
        pass
    _emit(_ev)


def _rt_precheck(provider, model, in_tok, est_out):
    try:
        est = pricing.realtime_cost(model, in_tok, est_out) if model else 0.0
    except Exception:
        est = 0.0
    _rt_precheck_usd(provider, model, est)


def _rt_precheck_usd(provider, model, est):
    """The $-denominated core of the realtime precheck — token surfaces price first (_rt_precheck);
    unit surfaces (images/audio/TTS) arrive here with dollars directly."""
    global _rt_warned, _rt_bypass
    if _meta_intent():                                # spendguard's own use → separate meta cap, skip workload
        if not _allow():
            from . import budget, config
            ex = budget.meta_exceeded(est)
            if ex:
                _emit({"kind": "meta", "provider": provider, "model": model, "cost": est, "decision": "refused_meta"})
                raise SpendGateRefused(f"spendguard meta budget ${config.meta_cap():.0f}/day would be exceeded "
                                       f"(projected ${ex[2]:.2f}). Raise caps.meta or set GATE_ALLOW=1.")
        return
    try:                                              # REALTIME BURST test-first gate — a loop of realtime calls is the
        from . import bulkgate                        # discouraged alternative to Batch; same estimate+test-first rule.
        intent = ""
        try:
            intent = (_calls.current().get("intent") or "").strip()
        except Exception:
            pass
        if not intent.startswith("spendguard:"):
            bulkgate.check_realtime(bulkgate.sig(model or "", template_id=intent or None), model or "", est)
    except bulkgate.GateBlocked:
        raise                                         # untested burst in block mode → stop the loop
    except Exception:
        pass                                          # fail-open (never break a legit call on a gate hiccup)
    _budget_check(est, model, provider, "realtime")   # cross-process daily/monthly cap (sqlite backend)
    with _rt_lock:
        projected = _rt_spent + est
        spent = _rt_spent
    budget = _rt_budget()
    if projected <= budget or _allow() or _rt_bypass:
        return
    msg = (f"[spend_gate] REAL-TIME budget: spent ${spent:.2f} + next ~${est:.2f} would exceed "
           f"${budget:.0f}/process (GATE_RT_BUDGET).")
    if sys.stdin and sys.stdin.isatty():
        try:
            ans = input(msg + " Allow the rest of this run's real-time calls? type 'yes': ").strip().lower()
        except Exception:
            ans = ""
        if ans in ("yes", "y"):
            _rt_bypass = True          # bypass ONLY the RT budget — NOT the per-batch / daily / monthly caps
            return
    _emit({"kind": "realtime", "provider": provider, "model": model, "cost": est, "decision": "refused_budget"})
    raise SpendGateRefused(msg + " Raise GATE_RT_BUDGET, set GATE_ALLOW=1, or stop the loop.")


def _est_oai_chat(kw):
    return (kw.get("model"),
            sum(_content_tokens(m.get("content", "")) for m in (kw.get("messages") or []) if isinstance(m, dict)),
            kw.get("max_tokens") or kw.get("max_completion_tokens") or 0)


def _act_oai_chat(result):
    u = getattr(result, "usage", None)
    return None if not u else (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)


def _est_oai_resp(kw):
    """OpenAI Responses API (client.responses.create) — the modern surface Codex + newer SDK code use. `input` is a
    string OR a list of message/items; `instructions` is the system text; the output ceiling is max_output_tokens."""
    n = 0
    instr = kw.get("instructions")
    if instr:
        n += _content_tokens(instr)
    inp = kw.get("input")
    if isinstance(inp, str):
        n += _content_tokens(inp)
    elif isinstance(inp, list):
        for m in inp:
            n += _content_tokens(m.get("content", m.get("text", "")) if isinstance(m, dict) else str(m))
    return kw.get("model"), n, (kw.get("max_output_tokens") or 0)


def _act_oai_resp(result):
    u = getattr(result, "usage", None)              # Responses usage = input_tokens / output_tokens (not prompt/completion)
    return None if not u else (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)


def _est_oai_embeddings(kw):
    """OpenAI embeddings (client.embeddings.create) — input is a string, a list of strings, or
    pre-tokenized int arrays; output tokens are always 0 (the table prices embedding out at $0)."""
    inp = kw.get("input")
    n = 0
    for item in (inp if isinstance(inp, list) else [inp]):
        if isinstance(item, str):
            n += _ct(item)
        elif isinstance(item, list):                 # already token ids — the count IS the length
            n += len(item)
        elif isinstance(item, int):
            n += 1
    return kw.get("model"), n, 0


def _act_oai_embeddings(result):
    u = getattr(result, "usage", None)               # embeddings usage = prompt_tokens/total_tokens only
    return None if not u else ((getattr(u, "prompt_tokens", 0) or 0), 0)


def _est_anth_msg(kw):
    n = 0
    s = kw.get("system")
    if s:
        n += _content_tokens(s)
    for m in (kw.get("messages") or []):
        c = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        n += _content_tokens(c)
    return kw.get("model"), n, (kw.get("max_tokens") or 0)


def _act_anth_msg(result):
    u = getattr(result, "usage", None)
    return None if not u else (getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)


def _cached_in(result):
    """Cached input tokens from usage — OpenAI prompt_tokens_details.cached_tokens / Anthropic
    cache_read_input_tokens. Lets cache-audit measure the realized hit rate (the 28% lever)."""
    u = getattr(result, "usage", None)
    if not u:
        return 0
    d = getattr(u, "prompt_tokens_details", None)         # OpenAI chat.completions
    if d is not None:
        return getattr(d, "cached_tokens", 0) or 0
    d = getattr(u, "input_tokens_details", None)          # OpenAI Responses API
    if d is not None:
        return getattr(d, "cached_tokens", 0) or 0
    return getattr(u, "cache_read_input_tokens", 0) or 0  # Anthropic


def _record_rt(model, kw, in_tok, out_tok, cached=0, latency=None, output=None, finish=None, cost=None, provider=None):
    """Record ONE realtime call's usage → cost · cross-process ledger · max_tokens truncation telemetry · call log.
    Shared by _rt_account (non-stream), the streaming proxy (ACTUAL usage as the stream is consumed), and the
    provider-breadth adapters (LiteLLM / Bedrock / Vertex). `cost` lets a caller supply an authoritative price (e.g.
    LiteLLM's own computed cost) instead of re-pricing; `provider` overrides the inferred ledger label."""
    prov = provider or ("anthropic" if str(model).startswith("claude") else "openai")   # o3/embeddings are OpenAI
    if cost is None:
        # normalize to OpenAI token semantics (input INCLUDES cached) before pricing: Anthropic's input_tokens
        # EXCLUDES cache_read, so add it back or _cost double-subtracts and under-bills ~2x.
        in_for_cost = (in_tok + cached) if prov == "anthropic" else in_tok
        try:
            cost = pricing.realtime_cost(model, in_for_cost, out_tok, cached) if model else 0.0
        except Exception:     # unpriced model (e.g. a new Bedrock/Vertex one) → keep the TOKENS, $0 + a visible flag.
            cost = 0.0         # NOT a guessed price (discipline); surfaced so the price can be added, never silently dropped.
            print(f"[spend_gate] WARN no price for '{model}' — recorded {in_tok}+{out_tok} tok at $0 "
                  f"(add it to prices.json with a source)", file=sys.stderr)
    if _meta_intent():                            # meta call → meta ledger only (not workload realtime)
        from . import budget
        budget.record_meta(prov, model, cost)
        if _calls.enabled():
            _calls.record(prov, model, "realtime", cost, in_tok=in_tok, out_tok=out_tok, latency=latency,
                          prompt=_prompt_text(kw), output=output, finish=finish)
        return
    _rt_record(prov, model, cost, in_tok=in_tok, out_tok=out_tok, cached=cached)
    try:                                              # max_tokens TRUNCATION detection (a fact) + per-sig telemetry
        from . import bulkgate
        _intent = (_calls.current().get("intent") or "").strip()
        if not _intent.startswith("spendguard:"):
            bulkgate.note_response(bulkgate.sig(model or "", template_id=_intent or None),
                                   model or "", out_tok, kw.get("max_tokens"), finish)
    except Exception:
        pass
    if _calls.enabled():
        _calls.record(prov, model, "realtime", cost, in_tok=in_tok, out_tok=out_tok, latency=latency,
                      prompt=_prompt_text(kw), output=output, finish=finish)


def _stream_out_estimate(model, kw, est_fn):
    """Fallback when a stream's usage couldn't be captured: size output from the call-class's MEASURED history
    (per-sig p99×1.5), capped at the max_tokens ceiling (the ceiling alone over-counts streaming 2-5x)."""
    _, in_tok, out_tok = est_fn(kw)
    try:
        from . import bulkgate
        _it = (_calls.current().get("intent") or "").strip()
        if not _it.startswith("spendguard:"):
            mt = bulkgate.maxtokens(bulkgate.sig(model or "", template_id=_it or None))
            if (mt.get("n") or 0) >= 5 and mt.get("p99"):
                out_tok = min(out_tok or 10 ** 9, int(mt["p99"] * 1.5))
    except Exception:
        pass
    return in_tok, out_tok


def _rt_account(model, kw, result, est_fn, act_fn, latency=None):
    """Record a NON-streaming realtime call from its actual usage (act_fn), else the estimate. Streaming calls are
    recorded by the stream proxy on exhaustion; reaching here for a stream is the wrap-FAILED fallback → estimate."""
    try:
        if kw.get("stream"):
            in_tok, out_tok = _stream_out_estimate(model, kw, est_fn)
            _record_rt(model, kw, in_tok, out_tok, 0, latency)
            return
        act = act_fn(result)
        if act:
            in_tok, out_tok = act
        else:
            _, in_tok, out_tok = est_fn(kw)
        _record_rt(model, kw, in_tok, out_tok, _cached_in(result), latency, _output_text(result), _finish(result))
        _record_tool_fees(model, kw, result)
    except Exception as e:
        print(f"[spend_gate] WARN real-time accounting failed ({e})", file=sys.stderr)


def _tool_fee_count(result):
    """How many PER-CALL-billed tool invocations this response carried — OpenAI Responses output items of
    type web_search_call, or Anthropic usage.server_tool_use.web_search_requests. Token usage never
    includes these fees; without this they'd only ever appear as a day-level reconcile residual."""
    n = 0
    try:
        for item in (getattr(result, "output", None) or []):
            if getattr(item, "type", None) == "web_search_call":
                n += 1
        stu = getattr(getattr(result, "usage", None), "server_tool_use", None)
        n += int(getattr(stu, "web_search_requests", 0) or 0)
    except Exception:
        pass
    return n


def _record_tool_fees(model, kw, result):
    """Record per-call tool fees as their own ledger entry (a SECOND row — fees are not tokens and
    hiding them inside a token row would be unauditable). Unpriced → $0 + loud warn, never silent."""
    n = _tool_fee_count(result)
    if not n:
        return
    try:
        fee = n * pricing.unit_price("web_search_call", model)
    except KeyError:
        _warn_unpriced_unit("web_search_call", model)
        fee = 0.0
    _record_rt(model, kw, 0, 0, cost=fee)


def _pull_usage(chunk, acc):
    """Accumulate token usage from ONE streaming chunk — OpenAI chat's final chunk `.usage` (needs stream_options
    include_usage), OpenAI Responses' `.response.usage`, Anthropic's message_start (input) + message_delta (output) —
    plus the finish/stop reason. Best-effort: unknown chunk shapes are ignored (fail-open)."""
    try:
        u = getattr(chunk, "usage", None)                    # OpenAI chat final chunk / Anthropic message_delta
        if u is not None:
            for src, dst in (("prompt_tokens", "in"), ("input_tokens", "in"),
                             ("completion_tokens", "out"), ("output_tokens", "out")):
                v = getattr(u, src, None)
                if v:
                    acc[dst] = int(v)
            for dattr in ("prompt_tokens_details", "input_tokens_details"):
                d = getattr(u, dattr, None)
                if d is not None and getattr(d, "cached_tokens", None):
                    acc["cached"] = int(d.cached_tokens)
            if getattr(u, "cache_read_input_tokens", None):
                acc["cached"] = int(u.cache_read_input_tokens)
        m = getattr(chunk, "message", None)                  # Anthropic message_start carries input usage
        mu = getattr(m, "usage", None) if m is not None else None
        if mu is not None:
            if getattr(mu, "input_tokens", None):
                acc["in"] = int(mu.input_tokens)
            if getattr(mu, "output_tokens", None):
                acc["out"] = int(mu.output_tokens)
        resp = getattr(chunk, "response", None)              # OpenAI Responses API: final event .response.usage
        ru = getattr(resp, "usage", None) if resp is not None else None
        if ru is not None:
            if getattr(ru, "input_tokens", None):
                acc["in"] = int(ru.input_tokens)
            if getattr(ru, "output_tokens", None):
                acc["out"] = int(ru.output_tokens)
        ch = getattr(chunk, "choices", None)                 # OpenAI finish_reason
        if ch and getattr(ch[0], "finish_reason", None):
            acc["finish"] = ch[0].finish_reason
        d = getattr(chunk, "delta", None)                    # Anthropic message_delta stop_reason
        sr = getattr(chunk, "stop_reason", None) or (getattr(d, "stop_reason", None) if d is not None else None)
        if sr:
            acc["finish"] = sr
    except Exception:
        pass


def _observe_stream(stream, model, kw, est_fn, t0, is_async):
    """Wrap a streaming response in a TRANSPARENT proxy that passes every chunk through, captures usage as it streams,
    and on exhaustion records the ACTUAL usage (or the estimate if none was emitted) — 'capture as it happens'.
    Returns the proxy; raises only if a proxy can't be built (caller falls back to the estimate). Fail-open inside."""
    acc = {}

    def _done():
        try:
            if acc.get("in") or acc.get("out"):
                _record_rt(model, kw, acc.get("in", 0), acc.get("out", 0), acc.get("cached", 0),
                           time.time() - t0, finish=acc.get("finish"))
            else:                                            # usage not emitted (e.g. include_usage off) → estimate
                in_tok, out_tok = _stream_out_estimate(model, kw, est_fn)
                _record_rt(model, kw, in_tok, out_tok, 0, time.time() - t0)
        except Exception:
            pass

    return (_AsyncStreamProxy if is_async else _StreamProxy)(stream, acc, _done)


class _StreamProxy:
    """Sync transparent proxy over a streaming response: observe iteration (capture usage), delegate everything else
    (.response/.close/...) so the consumer sees a normal stream. Records on exhaustion OR context-manager exit."""
    def __init__(self, stream, acc, done):
        object.__setattr__(self, "_sg", [stream, acc, done, False])

    def _fire(self):
        sg = object.__getattribute__(self, "_sg")
        if not sg[3]:
            sg[3] = True
            sg[2]()

    def __iter__(self):
        sg = object.__getattribute__(self, "_sg")
        try:
            for chunk in sg[0]:
                try:
                    _pull_usage(chunk, sg[1])     # usage capture is best-effort — NEVER let it drop a chunk
                except Exception:
                    pass
                yield chunk
        finally:
            self._fire()

    def __enter__(self):
        object.__getattribute__(self, "_sg")[0].__enter__()
        return self

    def __exit__(self, *a):
        r = object.__getattribute__(self, "_sg")[0].__exit__(*a)
        self._fire()
        return r

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_sg")[0], name)


class _AsyncStreamProxy:
    """Async transparent proxy — same as _StreamProxy for `async for` / `async with`."""
    def __init__(self, stream, acc, done):
        object.__setattr__(self, "_sg", [stream, acc, done, False])

    def _fire(self):
        sg = object.__getattribute__(self, "_sg")
        if not sg[3]:
            sg[3] = True
            sg[2]()

    async def __aiter__(self):
        sg = object.__getattribute__(self, "_sg")
        try:
            async for chunk in sg[0]:
                try:
                    _pull_usage(chunk, sg[1])     # usage capture is best-effort — NEVER let it drop a chunk
                except Exception:
                    pass
                yield chunk
        finally:
            self._fire()

    async def __aenter__(self):
        await object.__getattribute__(self, "_sg")[0].__aenter__()
        return self

    async def __aexit__(self, *a):
        r = await object.__getattribute__(self, "_sg")[0].__aexit__(*a)
        self._fire()
        return r

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_sg")[0], name)


def _inject_usage(kw, est_fn):
    """For OpenAI CHAT streaming, request usage in the final chunk (stream_options include_usage) so the gate can
    CAPTURE actual tokens. Anthropic streams + OpenAI Responses carry usage natively (no injection). No-op otherwise."""
    try:
        if kw.get("stream") and est_fn is _est_oai_chat:
            so = kw.get("stream_options")
            if so is None:
                kw["stream_options"] = {"include_usage": True}
            elif isinstance(so, dict):
                so.setdefault("include_usage", True)
    except Exception:
        pass


AUTOTUNE_MIN_OBS = 30          # a call-class needs this many observed outputs before autotune trusts them
AUTOTUNE_SLACK = 1.2           # only act when the caller's cap exceeds the recommendation by this factor
_autotune_said = set()         # one suggest/apply line per (sig, mode) per process — informative, not spammy


def _autotune_mode():
    v = os.environ.get("SPENDGUARD_AUTOTUNE")
    if v is not None:
        return v.strip().lower()
    try:
        from . import config
        return str(config._cfg_get("gate", "autotune", "suggest")).lower()
    except Exception:
        return "suggest"


def _autotune(kw, model):
    """Learned max_tokens at CALL TIME — the anti-amnesia principle applied to spend: what `spendguard
    maxtokens` measures becomes a default you can't forget. Modes (gate.autotune / SPENDGUARD_AUTOTUNE):
      off      nothing
      suggest  print the measured delta once per call-class (default — visibility, zero risk)
      apply    SHRINK a wasteful cap to the measured p99×1.5 — logged, visible, per-call override
               via kw autotune=False; NEVER raises a cap, NEVER adds one where the caller set none.
    Self-healing vetoes: < AUTOTUNE_MIN_OBS observations, or ANY truncation history on the class — and
    because every response's truncation is recorded per-sig (note_response), one truncated output after
    a clamp permanently vetoes further clamps for that class. Honest accounting: no counterfactual
    'saving' is recorded — the value is accurate estimates + runaway-output protection, not a claimed $.
    """
    mode = _autotune_mode()
    if mode not in ("suggest", "apply") or kw.pop("autotune", None) is False:
        return
    cap = kw.get("max_tokens")
    if not cap or not model:
        return
    from . import bulkgate
    intent = ""
    try:
        intent = (_calls.current().get("intent") or "").strip()
    except Exception:
        pass
    sig = bulkgate.sig(model, template_id=intent or None)
    b = bulkgate.maxtokens(sig, cap)
    if not b or (b.get("n") or 0) < AUTOTUNE_MIN_OBS or (b.get("truncations") or 0) > 0:
        return
    rec = int(b["recommend"])
    if rec <= 0 or cap <= rec * AUTOTUNE_SLACK:
        return
    key = (sig, mode)
    if mode == "apply":
        kw["max_tokens"] = rec
        _log({"kind": "autotune", "sig": sig, "model": model, "intent": intent or None,
              "from": cap, "to": rec, "n_obs": b["n"], "p99": b.get("p99")})
        if key not in _autotune_said:
            _autotune_said.add(key)
            print(f"[spend_gate] AUTOTUNE max_tokens {cap} → {rec} for '{intent or model}' "
                  f"(measured p99 {b.get('p99')}, n={b['n']}, 0 truncations — kw autotune=False to opt out)",
                  file=sys.stderr)
    elif key not in _autotune_said:
        _autotune_said.add(key)
        print(f"[spend_gate] autotune(suggest): max_tokens {cap} vs measured p99×1.5 = {rec} for "
              f"'{intent or model}' (n={b['n']}) — gate.autotune=apply clamps this automatically",
              file=sys.stderr)


def _rt_precheck_guard(est_fn, kw):
    """PRE-call: autotune + estimate + realtime precheck + usage injection, FAIL-OPEN. Only a DELIBERATE
    enforcement decision (SpendGateRefused / bulkgate.GateBlocked) propagates to block the call; ANY other
    error — a bug in est_fn, a gate hiccup — is swallowed so a legitimate call is never broken."""
    from . import bulkgate
    try:
        try:
            _autotune(kw, (est_fn(kw) or (None,))[0])
        except Exception:
            pass                                              # autotune must never affect the call
        m, i, o = est_fn(kw)
        _rt_precheck(None, m, i, o)
        _inject_usage(kw, est_fn)
    except (SpendGateRefused, bulkgate.GateBlocked):
        raise                                                 # deliberate enforcement — propagate
    except Exception as e:
        print(f"[spend_gate] WARN realtime precheck error ({e}); allowing (fail-open)", file=sys.stderr)


def _account_failopen(r, model, kw, est_fn, act_fn, t0, is_async):
    """POST-call: record usage, FAIL-OPEN. Returns exactly what the caller must receive — a transparent stream proxy
    for streams (yields the same chunks), else `r` UNCHANGED. NEVER raises and NEVER substitutes a different value,
    so accounting can fail any way it likes without altering or breaking the call's result."""
    try:
        if kw.get("stream"):                                  # capture actual usage as the stream is consumed
            try:
                return _observe_stream(r, model, kw, est_fn, t0, is_async)
            except Exception:
                _rt_account(model, kw, r, est_fn, act_fn, time.time() - t0)   # proxy build failed → estimate, keep r
                return r
        _rt_account(model, kw, r, est_fn, act_fn, time.time() - t0)
        return r
    except Exception as e:
        print(f"[spend_gate] WARN realtime accounting failed ({e}); call unaffected", file=sys.stderr)
        return r


def _wrap_rt(orig, est_fn, act_fn, is_async):
    # Two fail-open halves around the real call: pre-check (only deliberate enforcement may block) and post-account
    # (never raises, never alters the result). The user's LLM call must be untouched by any gate bug. See
    # tests/test_gate_properties.py for the invariants this upholds.
    if is_async:
        @functools.wraps(orig)
        async def w(self, *a, **kw):
            if not _disabled():
                _rt_precheck_guard(est_fn, kw)
            t0 = time.time()
            r = await _call_orig_async(orig, self, a, kw)
            if not _disabled():
                r = _account_failopen(r, kw.get("model"), kw, est_fn, act_fn, t0, True)
            return r
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            if not _disabled():
                _rt_precheck_guard(est_fn, kw)
            t0 = time.time()
            r = _call_orig(orig, self, a, kw)
            if not _disabled():
                r = _account_failopen(r, kw.get("model"), kw, est_fn, act_fn, t0, False)
            return r
    w._spend_gated = True
    return w


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


RT_INTERCEPTORS = [
    # (module, class, method, est_fn, act_fn, is_async)
    ("openai.resources.chat.completions", "Completions", "create", _est_oai_chat, _act_oai_chat, False),
    ("openai.resources.chat.completions", "AsyncCompletions", "create", _est_oai_chat, _act_oai_chat, True),
    ("openai.resources.responses", "Responses", "create", _est_oai_resp, _act_oai_resp, False),
    ("openai.resources.responses", "AsyncResponses", "create", _est_oai_resp, _act_oai_resp, True),
    ("anthropic.resources.messages", "Messages", "create", _est_anth_msg, _act_anth_msg, False),
    ("anthropic.resources.messages", "AsyncMessages", "create", _est_anth_msg, _act_anth_msg, True),
    ("openai.resources.embeddings", "Embeddings", "create", _est_oai_embeddings, _act_oai_embeddings, False),
    ("openai.resources.embeddings", "AsyncEmbeddings", "create", _est_oai_embeddings, _act_oai_embeddings, True),
]


# ── UNIT surfaces (non-token billing: images / transcription / TTS / fine-tune jobs) ──────────────
# est_usd_fn(kw) → (model, est_usd) for the $-direct precheck; act_fn(kw, result) does its OWN
# recording (each surface's actuals live in a different place). Unpriced units → the call is still
# RECORDED at $0 with a loud per-model warn (visibility without guessing) — same discipline as an
# unpriced model. All fail-open: only deliberate enforcement may block, accounting never breaks a call.
_unit_warned = set()


def _warn_unpriced_unit(kind, model):
    if (kind, model) not in _unit_warned:
        _unit_warned.add((kind, model))
        print(f"[spend_gate] WARN no {kind} unit price for '{model}' — recorded at $0 "
              f"(add prices.json unit_prices.{kind} with a source, or `spendguard sync-prices`)", file=sys.stderr)


def _est_usd_images(kw):
    model = kw.get("model") or ""
    n = int(kw.get("n") or 1)
    variant = f"{kw.get('size') or ''}:{kw.get('quality') or ''}".strip(":")
    try:
        return model, n * pricing.unit_price("image", model, variant or None)
    except KeyError:
        return model, 0.0


def _act_images(kw, result):
    model = kw.get("model") or ""
    n = len(getattr(result, "data", None) or []) or int(kw.get("n") or 1)
    variant = f"{kw.get('size') or ''}:{kw.get('quality') or ''}".strip(":")
    try:
        cost = n * pricing.unit_price("image", model, variant or None)
    except KeyError:
        _warn_unpriced_unit("image", model)
        cost = 0.0
    _record_rt(model, kw, 0, 0, cost=cost, provider="openai")


def _est_usd_transcription(kw):
    return kw.get("model") or "", 0.0                # duration unknown pre-call — actuals carry the $


def _act_transcription(kw, result):
    model = kw.get("model") or ""
    u = getattr(result, "usage", None)               # gpt-4o-transcribe bills TOKENS and reports usage
    if u is not None and getattr(u, "input_tokens", None) is not None:
        _record_rt(model, kw, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0,
                   provider="openai")
        return
    dur = getattr(result, "duration", None)          # whisper verbose_json reports seconds
    if dur:
        try:
            cost = float(dur) * pricing.unit_price("audio_second", model)
        except KeyError:
            _warn_unpriced_unit("audio_second", model)
            cost = 0.0
        _record_rt(model, kw, 0, 0, cost=cost, provider="openai")
        return
    print(f"[spend_gate] WARN transcription on '{model}' returned neither usage nor duration — recorded "
          f"at $0 (request response_format=verbose_json so seconds are billable-visible)", file=sys.stderr)
    _record_rt(model, kw, 0, 0, cost=0.0, provider="openai")


def _est_usd_speech(kw):
    model = kw.get("model") or ""
    chars = len(kw.get("input") or "")
    try:
        return model, chars * pricing.unit_price("tts_char", model)
    except KeyError:
        return model, 0.0


def _act_speech(kw, result):
    model = kw.get("model") or ""
    chars = len(kw.get("input") or "")               # deterministic: TTS bills the input characters
    try:
        cost = chars * pricing.unit_price("tts_char", model)
    except KeyError:
        _warn_unpriced_unit("tts_char", model)
        cost = 0.0
    _record_rt(model, kw, 0, 0, cost=cost, provider="openai")


def _est_usd_finetune(kw):
    return kw.get("model") or "", 0.0                # training tokens unknown until the job runs


def _act_finetune(kw, result):
    """A fine-tune job's training cost lands at RECONCILE (provider billing) — the submission itself is
    recorded LOUDLY so an unestimated paid operation is never invisible between now and then."""
    model = kw.get("model") or ""
    job_id = getattr(result, "id", None) or ""
    print(f"[spend_gate] fine-tune job {job_id or '(id unknown)'} on '{model}' submitted — training cost is "
          f"UNESTIMATED here and will land at reconcile from provider billing.", file=sys.stderr)
    _log({"kind": "finetune_job", "provider": "openai", "model": model, "job": job_id,
          "decision": "unestimated_submission"})


def _wrap_rt_units(orig, est_usd_fn, act_fn, is_async):
    if is_async:
        @functools.wraps(orig)
        async def w(self, *a, **kw):
            if not _disabled():
                try:
                    m, usd = est_usd_fn(kw)
                    _rt_precheck_usd(None, m, usd)
                except SpendGateRefused:
                    raise
                except Exception as e:
                    print(f"[spend_gate] WARN unit precheck error ({e}); allowing (fail-open)", file=sys.stderr)
            r = await _call_orig_async(orig, self, a, kw)
            if not _disabled():
                try:
                    act_fn(kw, r)
                except Exception as e:
                    print(f"[spend_gate] WARN unit accounting failed ({e}); call unaffected", file=sys.stderr)
            return r
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            if not _disabled():
                try:
                    m, usd = est_usd_fn(kw)
                    _rt_precheck_usd(None, m, usd)
                except SpendGateRefused:
                    raise
                except Exception as e:
                    print(f"[spend_gate] WARN unit precheck error ({e}); allowing (fail-open)", file=sys.stderr)
            r = _call_orig(orig, self, a, kw)
            if not _disabled():
                try:
                    act_fn(kw, r)
                except Exception as e:
                    print(f"[spend_gate] WARN unit accounting failed ({e}); call unaffected", file=sys.stderr)
            return r
    w._spend_gated = True
    return w


def _apply_units(module_path, class_name, method, est_usd_fn, act_fn, is_async):
    import importlib
    cls = getattr(importlib.import_module(module_path), class_name)
    cur = getattr(cls, method)
    if getattr(cur, "_spend_gated", False):
        return
    setattr(cls, method, _wrap_rt_units(cur, est_usd_fn, act_fn, is_async))


UNIT_INTERCEPTORS = [
    ("openai.resources.images", "Images", "generate", _est_usd_images, _act_images, False),
    ("openai.resources.images", "AsyncImages", "generate", _est_usd_images, _act_images, True),
    ("openai.resources.audio.transcriptions", "Transcriptions", "create", _est_usd_transcription, _act_transcription, False),
    ("openai.resources.audio.transcriptions", "AsyncTranscriptions", "create", _est_usd_transcription, _act_transcription, True),
    ("openai.resources.audio.speech", "Speech", "create", _est_usd_speech, _act_speech, False),
    ("openai.resources.audio.speech", "AsyncSpeech", "create", _est_usd_speech, _act_speech, True),
    ("openai.resources.fine_tuning.jobs", "Jobs", "create", _est_usd_finetune, _act_finetune, False),
    ("openai.resources.fine_tuning.jobs", "AsyncJobs", "create", _est_usd_finetune, _act_finetune, True),
]


# ── Interceptor registry — adding a provider/surface = one entry + a gate_fn ──
# Each entry: (module_path, class_name, method, gate_fn(kw, args), is_async)
# gate_fn estimates from the call's kwargs/args and calls _decide() (which may raise
# SpendGateRefused to block). To add a future SDK: write its gate_fn, then either append
# here or call register(...) — no other code changes.
INTERCEPTORS = [
    ("openai.resources.files", "Files", "create", _gate_openai_files, False),
    ("openai.resources.files", "AsyncFiles", "create", _gate_openai_files, True),
    ("anthropic.resources.messages.batches", "Batches", "create", _gate_anthropic, False),
    ("anthropic.resources.messages.batches", "AsyncBatches", "create", _gate_anthropic, True),
]
_EXTRA = []


def register(module_path: str, class_name: str, method: str, gate_fn, is_async: bool = False) -> None:
    """Register a new SDK surface to gate (e.g. a future provider). gate_fn(kw, args) -> None|raise."""
    _EXTRA.append((module_path, class_name, method, gate_fn, is_async))


def _guard(gate_fn, kw, a):
    """Run a gate_fn fail-OPEN: only a deliberate SpendGateRefused blocks; any other error (e.g. a
    `database is locked` under fleet concurrency, or a third-party register()'d fn) logs and lets the
    call proceed — the gate must never break a legitimate job by accident."""
    try:
        gate_fn(kw, a)
    except SpendGateRefused:
        raise
    except Exception as e:
        print(f"[spend_gate] WARN gate error ({e}); allowing (fail-open)", file=sys.stderr)


def _wrap(orig, gate_fn, is_async):
    if is_async:
        @functools.wraps(orig)
        async def w(self, *a, **kw):
            if not _disabled():
                _guard(gate_fn, kw, a)   # only SpendGateRefused propagates; all else fails open
            return await _call_orig_async(orig, self, a, kw)
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            if not _disabled():
                _guard(gate_fn, kw, a)
            return _call_orig(orig, self, a, kw)
    w._spend_gated = True
    return w


def _apply(module_path, class_name, method, gate_fn, is_async):
    import importlib
    cls = getattr(importlib.import_module(module_path), class_name)
    cur = getattr(cls, method)
    if getattr(cur, "_spend_gated", False):
        return
    setattr(cls, method, _wrap(cur, gate_fn, is_async))


def _apply_rt(module_path, class_name, method, est_fn, act_fn, is_async):
    import importlib
    cls = getattr(importlib.import_module(module_path), class_name)
    cur = getattr(cls, method)
    if getattr(cur, "_spend_gated", False):
        return
    setattr(cls, method, _wrap_rt(cur, est_fn, act_fn, is_async))


def install(cap: "float | None" = None) -> None:
    """Idempotently patch every registered SDK surface. Fail-open per entry (a missing
    SDK or changed internal just logs a warning; other surfaces still install).
    Optional cap=<dollars> sets GATE_CAP for this process."""
    if cap is not None:
        os.environ["GATE_CAP"] = str(cap)
    for spec in INTERCEPTORS + _EXTRA:
        try:
            _apply(*spec)
        except ModuleNotFoundError:
            pass  # that SDK isn't installed in this env — silently skip
        except Exception as e:
            print(f"[spend_gate] WARN patch {spec[0]}.{spec[1]}.{spec[2]} skipped: {e}", file=sys.stderr)
    for spec in RT_INTERCEPTORS:               # real-time accounting + cumulative budget
        try:
            _apply_rt(*spec)
        except ModuleNotFoundError:
            pass
        except Exception as e:
            print(f"[spend_gate] WARN rt-patch {spec[0]}.{spec[1]}.{spec[2]} skipped: {e}", file=sys.stderr)
    for spec in UNIT_INTERCEPTORS:             # non-token billing (images / transcription / TTS / ft jobs)
        try:
            _apply_units(*spec)
        except ModuleNotFoundError:
            pass
        except Exception as e:
            print(f"[spend_gate] WARN unit-patch {spec[0]}.{spec[1]}.{spec[2]} skipped: {e}", file=sys.stderr)
    import importlib
    for _mod in ("litellm_adapter", "bedrock_adapter", "vertex_adapter"):
        try:                                    # provider breadth (LiteLLM / direct Bedrock / direct Vertex) — wire
            importlib.import_module("." + _mod, __package__).install(force=False)   # only if the underlying SDK is
        except Exception:                        # ALREADY imported; never force-import a heavy optional dep at startup.
            pass                                 # Users call spendguard.install_{litellm,bedrock,vertex}() explicitly.
    try:                                         # third-party providers: `pip install spendguard-provider-X` is all a
        from . import provider_plugins           # user does — entry points activate here, fail-open per plugin
        provider_plugins.load()                  # (recipe: docs/PROVIDERS.md; conformance: spendguard.provider_kit)
    except Exception as e:
        print(f"[spend_gate] WARN provider plugins skipped: {e}", file=sys.stderr)
    try:                                         # raw-HTTP visibility net (capture-first, never blocks) — SDK-originated
        from . import http_capture               # traffic is suppressed via the _call_orig ContextVar (no double count)
        http_capture.install()
    except Exception as e:
        print(f"[spend_gate] WARN raw-HTTP capture skipped: {e}", file=sys.stderr)


def _any_patched():
    """True iff at least one SDK surface is actually gated in THIS interpreter (enforcement is live here)."""
    import importlib
    for spec in INTERCEPTORS + _EXTRA + RT_INTERCEPTORS + UNIT_INTERCEPTORS:
        module_path, class_name, method = spec[0], spec[1], spec[2]
        try:
            cls = getattr(importlib.import_module(module_path), class_name)
            if getattr(getattr(cls, method, None), "_spend_gated", False):
                return True
        except Exception:
            continue
    return False


def require(cap: "float | None" = None) -> None:
    """FAIL-CLOSED guard — ensure the gate is actually ENFORCING in this interpreter, else raise. Put at the
    top of any script that must not spend ungated; it refuses to run if spendguard was bypassed or disabled:

        import spendguard; spendguard.require()      # then your client.batches.create(...) / chat calls

    This is the fix for the #1 bypass: running under a python/venv where the gate never auto-installed."""
    if _disabled():
        raise SpendGateRefused("spendguard is DISABLED (GATE_DISABLE / `spendguard off`) but this script "
                               "called require() — refusing to spend ungated. `spendguard on` to re-enable.")
    install(cap=cap)
    if not _any_patched():
        raise SpendGateRefused(
            "spendguard is NOT enforcing in this interpreter — no OpenAI/Anthropic SDK was patched (wrong "
            "python/venv, or the SDK isn't importable here). require() refuses to spend ungated. Fix: run under "
            "a gated venv, or `pip install llm-spendguard` and `import spendguard` before the SDK is used.")


def _cli(cmd="status", live=False):
    if cmd == "off":
        open(FLAG, "w").write("disabled\n")
        print(f"🔴 spend gate DISABLED (persistent). Re-enable: spendguard on\n  flag: {FLAG}")
    elif cmd == "on":
        if os.path.exists(FLAG):
            os.remove(FLAG)
        print("🟢 spend gate ENABLED.")
    else:  # status / doctor
        print(f"spend gate: {'🔴 DISABLED' if _disabled() else '🟢 ENABLED'}   (cap ${_cap():.0f})")
        print(f"  python    : {sys.executable}")
        install()
        enforcing = _any_patched()
        print(f"  ENFORCING HERE: {'🟢 YES — calls from this interpreter are gated' if enforcing else '🔴 NO — calls from THIS interpreter are NOT gated (bypass!)'}")
        if not enforcing:
            print("    fix: run under a gated venv, `spendguard install-hook --venv <v>` / `--user`, "
                  "or `import spendguard; spendguard.require()` at the top of the script.")
        try:
            from . import bulkgate
            _bm = bulkgate.mode()
            print(f"  ENFORCING bulk test+estimate: {'🟢 ' + _bm.upper() if _bm != 'off' else '🔴 OFF'}"
                  f"  (>{bulkgate.preview_max()} reqs needs fresh estimate+test; SPENDGUARD_ENFORCE=off|warn|block)")
        except Exception:
            pass
        print(f"  flag file : {FLAG}  ({'present → off' if os.path.exists(FLAG) else 'absent'})")
        print(f"  env       : GATE_DISABLE={os.getenv('GATE_DISABLE','')!r}  GATE_ALLOW={os.getenv('GATE_ALLOW','')!r}  GATE_CAP={os.getenv('GATE_CAP') or '(default 75)'}")
        try:
            from openai.resources import files as of
            oai = getattr(of.Files.create, "_spend_gated", False)
        except Exception:
            oai = "n/a (SDK absent)"
        try:
            from anthropic.resources.messages import batches as ab
            ant = getattr(ab.Batches.create, "_spend_gated", False)
        except Exception:
            ant = "n/a (SDK absent)"
        print(f"  patched   : openai={oai} anthropic={ant}")
        # API keys — a proper setup check (this is what would have CAUGHT the repo-move break: cwd-relative .env
        # silently lost the keys, so reconcile/report saw no provider data). Show found + where from.
        try:
            from . import config
            for prov, name in (("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")):
                k = config.api_key(name)
                print(f"  key {prov:<9}: {'🟢 resolved' if k else '🔴 MISSING — reconcile/report will see NO ' + prov + ' spend (add to ~/.spendguard/.env)'}")
        except Exception:
            pass
        if cmd == "doctor":
            # SaaS push readiness — confirm THIS repo will independently push to the aggregation server.
            try:
                from . import saas as _saas
                c = _saas.conn()
                ok, reason = _saas.ready()
                if c.get("enabled"):
                    print(f"  saas      : {'🟢 ' + reason if ok else '🔴 ' + reason}  url={c.get('url') or '(unset)'}")
                    cok, cwhy = _saas.contributor_ok()
                    print(f"  push-as   : project={c.get('project') or '(git repo name)'}  "
                          f"contributor={'🟢' if cok else '🔴'} {_saas.contributor() or '(unresolved)'}  "
                          f"visibility={c.get('visibility', 'private')}")
                    if not cok:
                        print(f"    ⚠ {cwhy}")
                else:
                    print("  saas      : ⚪ off (set up a per-repo .spendguard.json to push this repo to the server)")
            except Exception:
                pass
            try:                                   # ungoverned spend (bypass detection) — CACHED verdict.
                # A health check must be FAST: the live leak computation pulls ~30 days of provider billing
                # (measured 3.5 min) and already runs in the daily report/reconcile, which persist it as a
                # byproduct. Read that, show its AGE; no cache = honest UNKNOWN, never a silent skip.
                # `spendguard doctor --live` forces the full pull.
                from . import ledger_sync
                if live:
                    line = ledger_sync.leak_line()
                    print(f"  ledger    : {line}" if line else "  ledger    : (nothing to compare yet)")
                else:
                    cached = ledger_sync.cached_leak_line()
                    if cached is None:
                        print("  ledger    : leak status UNKNOWN — run `spendguard reconcile` (free) "
                              "or `spendguard doctor --live`")
                    else:
                        cline, age = cached
                        age_s = f"{age/3600:.1f}h" if age >= 3600 else f"{age/60:.0f}m"
                        print(f"  ledger    : {cline or 'nothing to compare'}  (as of {age_s} ago — "
                              f"`spendguard reconcile` refreshes)")
            except Exception:
                print("  ledger    : leak status UNKNOWN — check could not run")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1] if len(sys.argv) > 1 else "status",
                  live="--live" in sys.argv[2:]))
