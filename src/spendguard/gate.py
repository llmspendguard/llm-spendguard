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


def _decide(est):
    """Proceed (return) if under cap or allowed; raise SpendGateRefused to block."""
    cap = _cap()
    line = (f"[spend_gate] {est['provider']} {est.get('model')} · {est['requests']} req · "
            f"in~{est['in_tok']:,} out≤{est['out_tok']:,} -> ~${est['cost']:.2f} (cap ${cap:.0f})")
    if est["cost"] <= cap:
        _log({**est, "decision": "under_cap"}); print(line + "  OK", file=sys.stderr); return
    if _allow():
        _log({**est, "decision": "allowed_env"}); print(line + "  ALLOWED (GATE_ALLOW=1)", file=sys.stderr); return
    print(f"\n*** SPEND GATE: this single batch is projected at ${est['cost']:.2f}, over the ${cap:.0f} cap. ***\n"
          f"{line}\nBetter first: pack 25–40 items/request · trim max_tokens · use the cheaper executor "
          f"(opus-4.8 output < gpt-5.5) · split the scope. (raise GATE_CAP or GATE_ALLOW=1 to force.)", file=sys.stderr)
    if sys.stdin and sys.stdin.isatty():
        try:
            ans = input(f"Allow this ${est['cost']:.2f} submission anyway? type 'yes' to proceed: ").strip().lower()
        except Exception:
            ans = ""
        if ans in ("yes", "y"):
            _log({**est, "decision": "allowed_prompt"}); return
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


def _decide_and_account(est):
    if _meta_gate(est["cost"], est.get("model"), est.get("provider")):   # spendguard's own use → meta cap
        return
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
    _emit({"kind": "realtime", "provider": provider, "model": _m, "cost": cost, "decision": "recorded",
           "in_tok": in_tok, "out_tok": out_tok, "cached_in_tok": cached})


def _rt_precheck(provider, model, in_tok, est_out):
    global _rt_warned, _rt_bypass
    try:
        est = pricing.realtime_cost(model, in_tok, est_out) if model else 0.0
    except Exception:
        est = 0.0
    if _meta_intent():                                # spendguard's own use → separate meta cap, skip workload
        if not _allow():
            from . import budget, config
            ex = budget.meta_exceeded(est)
            if ex:
                _emit({"kind": "meta", "provider": provider, "model": model, "cost": est, "decision": "refused_meta"})
                raise SpendGateRefused(f"spendguard meta budget ${config.meta_cap():.0f}/day would be exceeded "
                                       f"(projected ${ex[2]:.2f}). Raise caps.meta or set GATE_ALLOW=1.")
        return
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
    d = getattr(u, "prompt_tokens_details", None)
    if d is not None:
        return getattr(d, "cached_tokens", 0) or 0
    return getattr(u, "cache_read_input_tokens", 0) or 0


def _rt_account(model, kw, result, est_fn, act_fn, latency=None):
    try:
        output = finish = None
        if kw.get("stream"):                       # can't read a stream's usage without consuming it
            _, in_tok, out_tok = est_fn(kw)
        else:
            act = act_fn(result)
            if act:
                in_tok, out_tok = act
            else:
                _, in_tok, out_tok = est_fn(kw)
            output = _output_text(result); finish = _finish(result)
        cached = 0 if kw.get("stream") else _cached_in(result)
        prov = "anthropic" if str(model).startswith("claude") else "openai"   # o3/embeddings are OpenAI, not "gpt"
        # normalize to OpenAI token semantics (input INCLUDES cached) before pricing: Anthropic's
        # input_tokens EXCLUDES cache_read, so add it back or _cost double-subtracts and under-bills ~2x.
        in_for_cost = (in_tok + cached) if prov == "anthropic" else in_tok
        cost = pricing.realtime_cost(model, in_for_cost, out_tok, cached) if model else 0.0
        if _meta_intent():                            # meta call → meta ledger only (not workload realtime)
            from . import budget
            budget.record_meta(prov, model, cost)
            if _calls.enabled():
                _calls.record(prov, model, "realtime", cost, in_tok=in_tok, out_tok=out_tok, latency=latency,
                              prompt=_prompt_text(kw), output=output, finish=finish)
            return
        _rt_record(prov, model, cost, in_tok=in_tok, out_tok=out_tok, cached=cached)
        if _calls.enabled():
            _calls.record(prov, model, "realtime", cost, in_tok=in_tok, out_tok=out_tok, latency=latency,
                          prompt=_prompt_text(kw), output=output, finish=finish)
    except Exception as e:
        print(f"[spend_gate] WARN real-time accounting failed ({e})", file=sys.stderr)


def _wrap_rt(orig, est_fn, act_fn, is_async):
    if is_async:
        @functools.wraps(orig)
        async def w(self, *a, **kw):
            if not _disabled():
                m, i, o = est_fn(kw); _rt_precheck(None, m, i, o)
            t0 = time.time()
            r = await orig(self, *a, **kw)
            if not _disabled():
                _rt_account(kw.get("model"), kw, r, est_fn, act_fn, time.time() - t0)
            return r
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            if not _disabled():
                m, i, o = est_fn(kw); _rt_precheck(None, m, i, o)
            t0 = time.time()
            r = orig(self, *a, **kw)
            if not _disabled():
                _rt_account(kw.get("model"), kw, r, est_fn, act_fn, time.time() - t0)
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
    ("anthropic.resources.messages", "Messages", "create", _est_anth_msg, _act_anth_msg, False),
    ("anthropic.resources.messages", "AsyncMessages", "create", _est_anth_msg, _act_anth_msg, True),
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
            return await orig(self, *a, **kw)
    else:
        @functools.wraps(orig)
        def w(self, *a, **kw):
            if not _disabled():
                _guard(gate_fn, kw, a)
            return orig(self, *a, **kw)
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


def _any_patched():
    """True iff at least one SDK surface is actually gated in THIS interpreter (enforcement is live here)."""
    import importlib
    for spec in INTERCEPTORS + _EXTRA + RT_INTERCEPTORS:
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


def _cli(cmd="status"):
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
            try:                                   # ungoverned spend (bypass detection)
                from . import ledger_sync
                line = ledger_sync.leak_line()
                if line:
                    print(f"  ledger    : {line}")
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1] if len(sys.argv) > 1 else "status"))
