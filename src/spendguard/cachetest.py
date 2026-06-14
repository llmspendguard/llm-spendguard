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
    from .cacheaudit import _SYS_ASSIGN
    txt = open(path, errors="ignore").read()
    best = ""
    for m in _SYS_ASSIGN.finditer(txt):
        q = m.group(1)
        end = txt.find(q, m.end())
        if end < 0:
            continue
        body = txt[m.end():end].strip()
        if body[:1].isalpha() and len(body) > len(best):
            best = body
    return best


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


def cache_test(system, users, model=None, run=False):
    from . import adapters
    model = model or config.advisor_judge_model()
    prov = adapters.provider_for(model)
    sys_tok = _count_tokens(system, model)
    if not system or sys_tok < 200:
        print("cache-test — need a system block ≥200 tokens to be worth caching (give --script or --from-intent).")
        return dict(ok=False)
    users = users or ["Item A.", "Item B.", "Item C."]
    n = len(users)
    p = pricing.price(model)
    base, read = p["in_"], p.get("cached_in", p["in_"])

    print(f"cache-test — {model} ({prov}) · system block {sys_tok:,} tokens · {n} calls "
          f"(1 cold + {n-1} warm), caged {META}:cache-test")
    in_tok = sum(_count_tokens(system + u, model) for u in users)
    est = pricing.realtime_cost(model, in_tok, 16 * n)
    print(f"  ESTIMATE (zero paid calls): ~{in_tok:,} in tok -> ~${est:.4f}  (meta ${config.meta_cap():.0f}/day)")
    if prov == "openai" and sys_tok < 1024:
        print("  ⚠️ OpenAI auto-caches only prefixes ≥1024 tokens — this block is too short to cache there.")
    if not run:
        print("  estimate-only. Re-run with --run to actually test caching (gate caps it).")
        return dict(ok=True, est=est)

    calls_out = []
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
                r = c.chat.completions.create(model=model, max_tokens=16,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": u}])
                d = getattr(r.usage, "prompt_tokens_details", None)
                calls_out.append(dict(in_=r.usage.prompt_tokens,
                                      write=0, read=(getattr(d, "cached_tokens", 0) or 0) if d else 0))

    print("\n  call    input   cache_write   cache_read")
    for i, co in enumerate(calls_out):
        tag = "cold" if i == 0 else "warm"
        print(f"  {i+1:>2} {tag}  {co['in_']:>7}   {co['write']:>10}   {co['read']:>10}")
    warm = calls_out[1:] or calls_out
    engaged = any(co["read"] > 0 for co in warm)
    avg_read = sum(co["read"] for co in warm) / max(1, len(warm))

    print()
    if engaged:
        per_call_saving = avg_read * (base - read) / 1_000_000
        write_extra = sys_tok * (base * _ANTHROPIC_WRITE_MULT - base) / 1_000_000 if prov == "anthropic" else 0
        breakeven = (write_extra / per_call_saving) if per_call_saving else 0
        print(f"  ✓ caching ENGAGED — {avg_read:,.0f} tokens read from cache on warm calls.")
        print(f"  ✓ warm-call input saving: ~${per_call_saving:.5f}/call ({100*(base-read)/base:.0f}% off the cached block).")
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
    system, users = "", None
    if a.script:
        system = _system_from_script(a.script)
    elif a.from_intent:
        system, users = _system_and_users_from_intent(a.from_intent, a.n)
    else:
        print("give --script PATH or --from-intent INTENT (the prefix to test for caching).")
        return 1
    if users and len(users) < a.n:
        users = (users * a.n)[:a.n]
    cache_test(system, users or ["Item A.", "Item B.", "Item C."][:a.n], model=a.model, run=a.run)
    return 0
