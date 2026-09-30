"""Measure each OpenAI-compatible embedding provider's REAL per-request batch ceiling — the SSOT input for
`model_catalog` capabilities.embed_max_batch. Exceeding a ceiling 400s the WHOLE chunk (every input in it fails), so
the ceiling must be MEASURED, not assumed (a 5,768-text gemini run left 5,760 unembedded under one global default).

Method (cheap by construction — the 400s cost nothing):
  1. send a deliberately OVERSIZED batch; if it 400s, PARSE the provider's STATED limit from the error (Gemini/OpenAI/
     Voyage all state it, e.g. "at most 100 requests"). A parse of a provider-stated number is mechanical extraction,
     not a meaning judgement.
  2. CONFIRM: embed exactly `ceiling` inputs (must succeed) and `ceiling+1` (must 400). The only billed calls are the
     successful small ones; every over-limit call 400s for $0.
  3. if the oversized 400 states no number, COARSE-bisect in [1, OVERSIZE] (success/fail only, no parse).

Inputs are distinct tiny strings ("probe-<i>", ~2 tokens) so the COUNT limit dominates (never a token-budget 400) and
the total spend stays at pennies. Estimate-first: prints the plan + a bound before any call. Gated + require()d.

  python scripts/reliability/embed_ceiling_probe.py            # measure (metered, ~$0.001)
  python scripts/reliability/embed_ceiling_probe.py --estimate # print the plan + cost bound, spend $0
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import spendguard                                                  # noqa: E402
spendguard.require()                                              # fail closed — never probe ungated
from spendguard import adapters, pricing, calls                   # noqa: E402

PROBE_INTENT = "embed-ceiling-probe"                              # attribute this probe's tiny spend, never '(none)'

# (provider:model, expected ceiling to confirm first). gemini is already measured at 100 (bisection 2026-09-30);
# it is listed so the probe re-confirms it end-to-end. The "expected" only orders the confirm step — the OVERSIZE
# probe + parse is what actually establishes the number, so a wrong expectation costs at most one extra 400 ($0).
TARGETS = [
    ("openai:text-embedding-3-small", 2048),
    ("voyage:voyage-3.5", 1000),
    ("gemini:gemini-embedding-001", 100),
]
OVERSIZE = 4096            # above any plausible embeddings batch ceiling; if even this succeeds we record ">=OVERSIZE"


def _texts(n):
    return [f"probe-{i}" for i in range(n)]                        # distinct + tiny → count limit dominates, ~2 tok each


def _try(model, n):
    """Embed n tiny inputs as ONE RAW /embeddings request, BYPASSING adapters.embed()'s catalog clamp — this must
    measure the PROVIDER's true ceiling, and the clamp would otherwise re-chunk under it so a 400 never surfaces
    (the clamp is exactly the fix this probe's numbers feed). The gate still meters the call (it patches the SDK
    class). Returns (ok, err, cost): ok iff the provider returned a vector for every input."""
    prov, raw = model.split(":", 1)
    with calls.context(intent=PROBE_INTENT):
        try:
            client = adapters._oai_compat_client(prov)
            r = client.embeddings.create(model=raw, input=_texts(n))
        except Exception as e:
            return False, str(e)[:300], 0.0                       # a 400 (over the cap) billed nothing
    ok = bool(getattr(r, "data", None)) and len(r.data) == n
    cost = 0.0
    if ok:
        try:
            cost = pricing.realtime_cost(model, in_tokens=2 * n, out_tokens=0)  # ~2 tok/input, success billed only
        except Exception:
            cost = 0.0
    return ok, None, cost


def _measure(model):
    """The largest batch the provider ACCEPTS, by pure BISECTION in [1, OVERSIZE] — send N, did it work? The
    provider's accept/reject is the only oracle, so no error prose is ever parsed (the same discipline embed() uses).
    Returns {model, ceiling, basis, spent, evidence}. Cheap: the over-cap probes 400 for $0; only accepted sizes bill."""
    spent, last_err = 0.0, None
    lo, hi, last_ok = 1, OVERSIZE, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        ok, err, c = _try(model, mid)
        spent += c
        if ok:
            last_ok = mid
            lo = mid + 1
        else:
            last_err = err
            hi = mid - 1
    ceiling = f">={OVERSIZE}" if last_ok >= OVERSIZE else last_ok
    ev = f"largest accepted batch = {last_ok}" + (f"; first-over 400: {last_err}" if last_ok < OVERSIZE and last_err else "")
    return {"model": model, "ceiling": ceiling, "basis": "bisection", "spent": spent, "evidence": ev}


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    print("embed-ceiling probe — per-provider batch limit (the over-limit calls 400 for $0; only successes bill)")
    # a worst-case cost BOUND: confirm-steps embed ~Σ expected tiny inputs; bisection adds ~log2(OVERSIZE) successes.
    bound = 0.0
    for model, exp in TARGETS:
        try:
            bound += pricing.realtime_cost(model, in_tokens=2 * (exp + OVERSIZE), out_tokens=0)
        except Exception:
            pass
    print(f"  plan: {len(TARGETS)} providers, oversize={OVERSIZE}; worst-case cost bound ~${bound:.4f} "
          f"(typical far less — 400s are free).")
    if "--estimate" in argv:
        print("  --estimate: no calls made ($0).")
        return 0
    rows = []
    total = 0.0
    for model, exp in TARGETS:
        try:
            k = adapters.config.api_key(adapters.PROVIDERS[model.split(":", 1)[0]]["key_env"])
        except Exception:
            k = None
        if not k:
            print(f"  ✗ {model}: no key ({adapters.PROVIDERS[model.split(':',1)[0]]['key_env']}) — SKIPPED (cannot verify)")
            rows.append({"model": model, "ceiling": None, "basis": "no key", "spent": 0.0, "evidence": "key missing"})
            continue
        res = _measure(model)
        total += res["spent"]
        rows.append(res)
        print(f"  ✓ {model:<40} ceiling = {str(res['ceiling']):<8} [{res['basis']}]  spent ${res['spent']:.5f}")
        print(f"      evidence: {res['evidence']}")
    print("\nmeasured ceilings: " + ", ".join(f"{r['model'].split(':',1)[0]}={r['ceiling']}" for r in rows))
    print(f"real $ this probe: ${total:.5f} API + $0 subs + $0 remote  ::  est sub value $0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
