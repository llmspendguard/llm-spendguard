"""cache-test — empirically PROVE prompt caching works for a candidate, before re-architecting.

cache-audit finds where a large prefix is reused; this tests it for real: send the same big prefix a
few times and read the ACTUAL usage (Anthropic cache_creation/cache_read_input_tokens; OpenAI
prompt_tokens_details.cached_tokens). Confirms caching ENGAGED, measures cold-vs-warm cost, the one-time
write overhead, the break-even (reuses to pay it back), and projects savings at your volume.

Caching doesn't change outputs (pure billing optimization), so there's no quality risk to test — only
"does it engage and how much does it save." Estimate-first; the test calls are caged (spendguard:cache-test
→ caps.meta). The process: cache-audit (detect, free) → cache-test (prove, cents) → adopt → cache-audit /
report (verify the realized hit rate climbed).

CLI: `spendguard cache-test [--script P | --from-intent X] [--model M] [--n 3] [--run]`.
"""
import os, re
from . import config, calls, pricing
from .submit import _count_tokens

META = "spendguard"
_ANTHROPIC_WRITE_MULT = 1.25   # ephemeral (5-min) cache write premium over base input


def _system_from_script(path):
    """The largest system-prompt constant in a script — via cacheaudit's AST extractor.

    This was a second, weaker implementation of the same extraction: a raw regex whose `txt.find(quote, ...)`
    stops at the FIRST matching quote without checking for a preceding backslash, so a prompt containing an
    escaped quote was silently truncated at that point. That is not a cosmetic difference here — the
    truncated length feeds the ≥200-token worth-caching check and the ≥1024/2048-token provider thresholds,
    so a real caching candidate could be reported as too short to bother with. Its prose guard was weaker
    too (`body[:1].isalpha()` alone), which let code-shaped constants through as 'the system prompt'."""
    from . import cacheaudit
    txt = open(path, errors="ignore").read()
    cands = [v for _name, v in cacheaudit._sys_assignments_ast(txt)]   # [(name, value)] pairs
    return max(cands, key=len) if cands else ""


def _model_from_script(path):
    """Test the model the script ACTUALLY uses (an explicit model= wins; else infer from the filename)."""
    txt = open(path, errors="ignore").read()
    m = re.search(r"""model\s*=\s*["']([\w.\-]+)["']""", txt)
    if m:
        return m.group(1)
    base = os.path.basename(path).lower()
    for key, mdl in [("gpt5mini", "gpt-5-mini"), ("gpt5nano", "gpt-5-nano"), ("gpt55", "gpt-5.5"),
                     ("gpt5", "gpt-5"), ("opus", "claude-opus-4-8"), ("haiku", "claude-haiku-4-5"),
                     ("sonnet", "claude-sonnet-4-5")]:
        if key in base:
            return mdl
    return None


def _system_and_users_from_intent(intent, n):
    from . import callio
    from .cacheaudit import _common_prefix
    with callio._lock:
        prompts = [r[0] for r in callio._db().execute(
            "SELECT prompt FROM call_io WHERE COALESCE(intent,'(none)')=? AND prompt!='' LIMIT ?",
            (intent, max(n, 8))).fetchall()]
    if len(prompts) < 2:
        return "", []
    pref = _common_prefix(prompts)
    users = [p[len(pref):][:400] or "(item)" for p in prompts[:n]]
    return pref, users


def _run_calls(calls_out, system, users, model, prov):
    """Make the test calls, APPENDING each result as it lands.

    Extracted so a failure part-way through leaves the caller holding what already completed. The loop was
    inline and unguarded, so one exception discarded every call before it — calls that were made, billed,
    and whose usage figures were the entire point of running this."""
    from . import adapters                                             # noqa: F401  (import parity)
    with calls.context(intent=f"{META}:cache-test"):
        if prov == "anthropic":
            import anthropic
            c = anthropic.Anthropic(api_key=config.api_key("ANTHROPIC_API_KEY"))
            sysblock = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            for u in users:
                m = c.messages.create(model=model, max_tokens=16, system=sysblock,
                                      messages=[{"role": "user", "content": u}])
                usg = m.usage
                calls_out.append(dict(in_=getattr(usg, "input_tokens", 0),
                                      write=getattr(usg, "cache_creation_input_tokens", 0) or 0,
                                      read=getattr(usg, "cache_read_input_tokens", 0) or 0))
        else:
            from openai import OpenAI
            c = OpenAI(api_key=config.api_key("OPENAI_API_KEY"))
            for u in users:
                # gpt-5 family requires max_completion_tokens (max_tokens is rejected)
                r = c.chat.completions.create(model=model, max_completion_tokens=16,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": u}])
                d = getattr(r.usage, "prompt_tokens_details", None)
                calls_out.append(dict(in_=r.usage.prompt_tokens,
                                      write=0, read=(getattr(d, "cached_tokens", 0) or 0) if d else 0))


def cache_test(system, users, model=None, run=False):
    from . import adapters
    model = model or config.advisor_judge_model()
    prov = adapters.provider_for(model)
    sys_tok = _count_tokens(system, model)
    if not system or sys_tok < 200:
        print("cache-test — need a system block ≥200 tokens to be worth caching (give --script or --from-intent).")
        return dict(ok=False)
    # `is None`, not truthiness: an explicitly empty list is the caller saying "no user turns", and
    # `or` overrode that with three invented ones — a test that silently measured something else.
    users = ["Item A.", "Item B.", "Item C."] if users is None else users
    n = len(users)
    # AN UNPRICED MODEL CANNOT BE COSTED, AND SAYING SO BEATS A KeyError. price() raises or returns a card
    # without in_ for a model not in the table, and this indexed it directly — after the user had asked for
    # a cache test on exactly the model they most likely need to price.
    try:
        p = pricing.price(model) or {}
    except Exception as e:
        print(f"cache-test — no price for {model!r} ({type(e).__name__}), so the saving cannot be computed. "
              f"Add it: `spendguard price {model} --in <$/1M> --out <$/1M> --source '<url>'`")
        return dict(ok=False, why="unpriced model")
    if p.get("in_") is None:
        print(f"cache-test — the price card for {model!r} has no input rate; the saving cannot be computed.")
        return dict(ok=False, why="no input rate")
    base = p["in_"]
    read = p.get("cached_in") if p.get("cached_in") is not None else base

    print(f"cache-test — {model} ({prov}) · system block {sys_tok:,} tokens · {n} calls "
          f"(1 cold + {n-1} warm), caged {META}:cache-test")
    in_tok = sum(_count_tokens(system + u, model) for u in users)
    est = pricing.realtime_cost(model, in_tok, 16 * n)
    print(f"  ESTIMATE (zero paid calls): ~{in_tok:,} in tok -> ~${est:.4f}  (meta ${config.meta_cap():.0f}/day)")
    if prov == "openai" and sys_tok < 1024:
        print("  ⚠️ OpenAI auto-caches only prefixes ≥1024 tokens — this block is too short to cache there.")
    if prov == "anthropic" and "haiku" in str(model) and sys_tok < 2200:
        print("  ⚠️ Anthropic Haiku needs ≥2048 tokens to cache (Opus/Sonnet ≥1024); this block may be too short.")
    elif prov == "anthropic" and "haiku" not in str(model) and sys_tok < 1024:
        # the Haiku-only warning left Opus/Sonnet runs with a 200–1023-token block proceeding to paid calls that
        # can NEVER cache (Opus/Sonnet need ≥1024), despite the Haiku message itself naming that threshold.
        print("  ⚠️ Anthropic Opus/Sonnet need ≥1024 tokens to cache — this block is too short to cache there.")
    if not run:
        from . import ui; ui.estimate_only(action="run the live caching test", cost=est)
        return dict(ok=True, est=est)

    # PARTIAL RESULTS ARE STILL RESULTS. An exception mid-loop propagated and threw away every call
    # already made and BILLED — the one situation where reporting what you have matters most.
    calls_out = []
    try:
        _run_calls(calls_out, system, users, model, prov)
    except Exception as e:
        print(f"\n  ⚠ the run stopped after {len(calls_out)} of {len(users)} call(s) "
              f"({type(e).__name__}: {str(e)[:80]}). Those calls were BILLED; what follows is measured "
              f"from them alone and is not a complete test.")
    print("\n  call    input   cache_write   cache_read")
    for i, co in enumerate(calls_out):
        tag = "cold" if i == 0 else "warm"
        print(f"  {i+1:>2} {tag}  {co['in_']:>7}   {co['write']:>10}   {co['read']:>10}")
    # NO FALLBACK TO THE COLD CALL. `calls_out[1:] or calls_out` meant a single-call run measured cache
    # ENGAGEMENT using the very call that populates the cache — the one call guaranteed to read 0 from
    # it. That is not a weaker measurement, it is a measurement of the opposite thing.
    warm = calls_out[1:]
    if not warm:
        # NOT MEASURED IS NOT NOT-ENGAGED. With no warm call there is no evidence either way, and letting
        # this fall through would print "caching did NOT engage" — a confident negative finding produced by
        # a run that could not have produced any finding. `engaged` is None so a caller can tell the three
        # states apart: engaged, not engaged, and never tested.
        print("\n  ? cache engagement NOT MEASURED — a single call only populates the cache; it cannot read "
              "from it. Re-run with --n 2 or more.")
        return dict(ok=True, engaged=None, calls=calls_out,
                    why="only one call: no warm call to observe a cache read on")
    engaged = any(co["read"] > 0 for co in warm)
    avg_read = sum(co["read"] for co in warm) / len(warm)

    print()
    if engaged:
        per_call_saving = avg_read * (base - read) / 1_000_000
        # THE BILLED NUMBER, NOT OUR ESTIMATE OF IT. sys_tok is _count_tokens()'s guess at the block size;
        # calls_out[0]["write"] is what the provider actually charged cache-creation on, and it is sitting
        # right there in the response this function just read. Using the estimate in the one place the
        # truth is available is the exact substitution this whole tool exists to stop — and it feeds the
        # BREAK-EVEN, so an over-estimated write overhead tells you caching is not worth adopting when it is.
        _billed_write = (calls_out[0].get("write") or 0) if calls_out else 0
        write_extra = (_billed_write * (base * _ANTHROPIC_WRITE_MULT - base) / 1_000_000
                       if prov == "anthropic" else 0)
        breakeven = (write_extra / per_call_saving) if per_call_saving else 0
        print(f"  ✓ caching ENGAGED — {avg_read:,.0f} tokens read from cache on warm calls.")
        # A $0.00 BASE RATE IS A REAL CARD (embeddings publish one), and dividing by it raised
        # ZeroDivisionError after the test calls had already been paid for. No base means no percentage to
        # state, not a crash.
        _pct = f"{100 * (base - read) / base:.0f}% off the cached block" if base else "base rate is $0.00"
        print(f"  ✓ warm-call input saving: ~${per_call_saving:.5f}/call ({_pct}).")
        if write_extra:
            print(f"  • one-time write overhead ~${write_extra:.5f}; break-even after ~{breakeven:.1f} reuse(s).")
        for vol in (100_000, 1_000_000):
            print(f"  → at {vol:,} reuses: ~${per_call_saving*vol:,.2f} saved on this prefix alone.")
    else:
        print("  ✗ caching did NOT engage (cache_read=0). Check: prefix identical across calls, "
              "≥1024 tokens (OpenAI), static content FIRST, within the 5-min TTL (Anthropic).")
    return dict(ok=True, engaged=engaged, calls=calls_out)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard cache-test")
    ap.add_argument("--script", help="extract the big system prompt from this .py to test")
    ap.add_argument("--from-intent", help="use the common prefix of this intent's recovered prompts")
    ap.add_argument("--model", help="model to test (default: advisor_judge_model)")
    ap.add_argument("--n", type=int, default=3, help="calls to run (1 cold + n-1 warm)")
    ap.add_argument("--run", action="store_true", help="actually call (default: estimate). Caged by caps.meta.")
    a = ap.parse_args(argv)
    # --n IS HONOURED IN EVERY BRANCH. users stayed None through the --script path, so the three-item
    # fallback list capped that run at 3 calls however large --n was: `--n 10` measured 1 cold + 2 warm and
    # reported it as if ten had been made. The list is built from --n here, once, for all branches.
    system = ""
    users = [f"Item {i + 1}." for i in range(max(1, int(a.n)))]
    if a.script:
        system = _system_from_script(a.script)
        if not a.model:                              # test the model the script actually uses
            a.model = _model_from_script(a.script)
    elif a.from_intent:
        system, users = _system_and_users_from_intent(a.from_intent, a.n)
    else:
        print("give --script PATH or --from-intent INTENT (the prefix to test for caching).")
        return 1
    if users and len(users) < a.n:
        users = (users * a.n)[:a.n]
    cache_test(system, users or ["Item A.", "Item B.", "Item C."][:a.n], model=a.model, run=a.run)
    return 0
