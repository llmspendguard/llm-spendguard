"""spendguard experiment — the efficiency A/B/n LAB at the chokepoint.

review DETECTS the opportunity → experiment PROVES the fix (cost ↓ AND the output stays the same) →
apply to the script → report/calls VERIFIES the realized drop. Takes real (prompt, reference-output)
samples for an intent and runs VARIANTS on the SAME inputs through the gate (caged spendguard:experiment,
estimate-first). For each variant it measures cost and OUTPUT-EQUIVALENCE vs the production reference, and
recommends the cheapest variant that preserves the result. A variant only "wins" if it's cheaper AND keeps
the output — never trade quality for cost.

Variants (compose freely, iterate to converge):
  - output-format: append an instruction to make the output terser (fewer output tokens — usually the win).
  - model:         swap to a cheaper model (equivalence check exposes quality loss, e.g. nano).
  - cacheability:  (reported by cache-audit/cache-test) restructure so a ≥min-token static prefix is reused.

The baseline cost is computed from the stored sample (no spend); only the variants make calls.
CLI: `spendguard experiment --intent X [--model M]... [--instruction "..."]... [--n 20] [--run]`.
"""
import json, re, difflib
from . import callio, calls, config, pricing
from .submit import _count_tokens

META = "spendguard"
KILL_PILOT = 5        # samples in the cheap first stage
KILL_THRESH = 0.5     # kill a variant at pilot if mean output-match is below this (clear loser → stop spending)
_TERSE = ("\n\nOutput ONLY the minimal result (e.g. the JSON / the codes) with NO prose, "
          "explanation, or restated input. Be as terse as possible while keeping the same answer.")


def _base_model(intent):
    """The reference model with the most usable (prompt+output) samples — the baseline to compare against."""
    with callio._lock:
        r = callio._db().execute(
            "SELECT model, COUNT(*) FROM call_io WHERE COALESCE(intent,'(none)')=? AND prompt!='' "
            "AND output!='' GROUP BY model ORDER BY 2 DESC LIMIT 1", (intent,)).fetchone()
    return r[0] if r else None


def _samples(intent, base_model, k):
    with callio._lock:
        rows = callio._db().execute(
            "SELECT prompt, output, model FROM call_io WHERE COALESCE(intent,'(none)')=? AND model=? "
            "AND prompt!='' AND output!='' LIMIT ?", (intent, base_model, k)).fetchall()
    return rows


def _norm_json(s):
    if not s:
        return None
    for a, b in (("{", "}"), ("[", "]")):
        i, j = s.find(a), s.rfind(b)
        if 0 <= i < j:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                pass
    return None


def _flatten(x):
    """Scalars in document order — so equivalence is GRADED (fraction of fields that match), not the
    all-or-nothing exact match that scores 0% on any rich nested output (even a model vs itself)."""
    if isinstance(x, list):
        for e in x:
            yield from _flatten(e)
    elif isinstance(x, dict):
        for k in sorted(x):
            yield from _flatten(x[k])
    else:
        yield x


def _match(a, b):
    fa, fb = list(_flatten(a)), list(_flatten(b))
    n = max(len(fa), len(fb))
    if not n:
        return 1.0
    return sum(1 for i in range(n) if i < len(fa) and i < len(fb) and fa[i] == fb[i]) / n


def _equiv(ref, out):
    a, b = _norm_json(ref), _norm_json(out)
    if a is not None and b is not None:
        return _match(a, b)
    return difflib.SequenceMatcher(None, (ref or "").strip(), (out or "").strip()).ratio()


def _call(model, prompt, max_out=400):
    """One realtime call; returns (cost, in_tok, out_tok, text). models.apply_call_params handles each
    model's quirks (gpt-5 → reasoning='minimal' + max_completion_tokens) so we can't forget them."""
    from . import adapters, models
    prov = adapters.provider_for(model)
    key = config.api_key(adapters.PROVIDERS[prov]["key_env"])
    kw = models.apply_call_params(model, {"model": model, "max_tokens": max_out,
                                          "messages": [{"role": "user", "content": prompt}]})
    if prov == "anthropic":
        import anthropic
        m = anthropic.Anthropic(api_key=key).messages.create(**kw)
        text = "".join(b.text for b in m.content if getattr(b, "type", None) == "text")
        it, ot = m.usage.input_tokens, m.usage.output_tokens
    else:
        oc = OpenAI(api_key=key, base_url=adapters.PROVIDERS[prov]["base_url"])
        try:
            m = oc.chat.completions.create(**kw)
        except Exception as e:                          # self-heal a wrong reasoning_effort literal, then retry once
            if models.heal_reasoning(model, kw, e):
                m = oc.chat.completions.create(**kw)
            else:
                raise
        text = m.choices[0].message.content or ""
        it, ot = m.usage.prompt_tokens, m.usage.completion_tokens
    return pricing.realtime_cost(model, it, ot), it, ot, text


def _variants(intent, base_model, models, instructions):
    """Default variant set if none specified: terse-output + (a cheaper model if we can name one)."""
    vs = []
    for ins in (instructions or []):
        vs.append(dict(label=f"instr:{ins[:18]}", model=base_model, instr="\n\n" + ins))
    for m in (models or []):
        vs.append(dict(label=f"model:{m}", model=m, instr=""))
    if not vs:
        vs.append(dict(label="terse-output", model=base_model, instr=_TERSE))
    return vs


def experiment(intent, models=None, instructions=None, n=20, run=False):
    base_model = _base_model(intent)
    samples = _samples(intent, base_model, n) if base_model else []
    if not samples:
        print(f"experiment — no call_io samples for intent '{intent}'. Run `spendguard fetch-io` first.")
        return dict(ok=False)
    variants = _variants(intent, base_model, models, instructions)

    # baseline cost from the stored samples (no spend)
    base_cost = 0.0
    for prompt, ref, _m in samples:
        base_cost += pricing.realtime_cost(base_model, _count_tokens(prompt, base_model), _count_tokens(ref, base_model))
    base_per = base_cost / len(samples)

    print(f"experiment — intent '{intent}'  baseline {base_model}  ({len(samples)} samples), "
          f"caged {META}:experiment")
    print(f"  baseline ~${base_per:.5f}/call (computed from stored I/O; no spend)")
    # estimate variant cost (in tok known; out ≈ reference length, terse ≈ half)
    est = 0.0
    for v in variants:
        for prompt, ref, _m in samples:
            it = _count_tokens(prompt + v["instr"], v["model"])
            ot = _count_tokens(ref, v["model"]) // (2 if "terse" in v["label"] else 1)
            est += pricing.realtime_cost(v["model"], it, ot)
    print(f"  {len(variants)} variant(s) × {len(samples)} samples  ESTIMATE: ~${est:.4f}  "
          f"(meta ${config.meta_cap():.0f}/day · spent ${_meta():.4f})")
    if not run:
        print("  variants: " + ", ".join(v["label"] for v in variants))
        print("  estimate-only. Re-run with --run to execute (gate caps it).")
        return dict(ok=True, est=est)

    # GRADUATED: pilot small → kill clear losers cheap → expand survivors → report match ± stderr.
    # Guards against the law of small numbers: you SEE the uncertainty, losers waste only the pilot, and
    # a survivor's calls are real output (not lost if you then keep them — see --keep idea in docs).
    pilot = min(KILL_PILOT, len(samples))
    print(f"\n  graduated: pilot {pilot}, then expand survivors to {len(samples)} "
          f"(kill if match < {int(KILL_THRESH*100)}% at pilot)\n")
    print(f"  {'variant':<22}{'$/call':>10}{'cost vs base':>13}{'match±stderr':>16}  verdict")
    print(f"  {'baseline(' + base_model[:12] + ')':<22}{('$%.5f' % base_per):>10}{'—':>13}{'—':>16}  reference")
    results = []

    def _measure(v, subset):
        costs, scores = [], []
        for prompt, ref, _m in subset:
            try:
                mo = max(1500, int(_count_tokens(ref, v["model"]) * 2) + 800)
                cost, _it, _ot, text = _call(v["model"], prompt + v["instr"], max_out=mo)
                costs.append(cost); scores.append(_equiv(ref, text))
            except Exception:
                pass
        return costs, scores

    with calls.context(intent=f"{META}:experiment"):
        for v in variants:
            costs, scores = _measure(v, samples[:pilot])
            killed = scores and (sum(scores) / len(scores)) < KILL_THRESH
            if not killed:                                  # expand survivors to the full sample
                c2, s2 = _measure(v, samples[pilot:])
                costs += c2; scores += s2
            if not scores:
                print(f"  {v['label'][:21]:<22}{'ERR':>10}")
                continue
            n = len(scores)
            per = sum(costs) / n
            match = sum(scores) / n
            var = sum((s - match) ** 2 for s in scores) / n
            stderr = (var ** 0.5) / (n ** 0.5)
            drop = (base_per - per) / base_per * 100 if base_per else 0
            if killed:
                verdict = f"✗ killed at pilot (N={n})"
            elif per >= base_per:
                verdict = "✗ not cheaper"
            elif match >= 0.97:
                verdict = "✅ adopt" + (" (N small)" if n < 10 else "")
            else:
                verdict = "⚠️ cheaper, output differs"
            print(f"  {v['label'][:21]:<22}{('$%.5f' % per):>10}{('%+.0f%%' % -drop):>13}"
                  f"{('%.0f%% ±%.0f%% (N=%d)' % (100*match, 100*stderr, n)):>16}  {verdict}")
            results.append(dict(label=v["label"], per=per, drop=drop, match=match, stderr=stderr, n=n, killed=killed))
    print("\n  adopt only variants that are cheaper AND keep output (match ≥ 97%). Wide ±stderr or small N "
          "= inconclusive — run more samples (`--n`) before trusting it. Losers were killed at the pilot to cap waste.")
    return dict(ok=True, results=results)


def _meta():
    from . import budget
    try:
        return budget.meta_spent_today()
    except Exception:
        return 0.0


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard experiment")
    ap.add_argument("--intent", required=True)
    ap.add_argument("--model", action="append", help="a cheaper model variant to test (repeatable)")
    ap.add_argument("--instruction", action="append", help="an output-format instruction variant (repeatable)")
    ap.add_argument("--n", type=int, default=20, help="samples to test per variant")
    ap.add_argument("--run", action="store_true", help="actually call (default: estimate). Caged by caps.meta.")
    a = ap.parse_args(argv)
    experiment(a.intent, models=a.model, instructions=a.instruction, n=a.n, run=a.run)
    return 0
