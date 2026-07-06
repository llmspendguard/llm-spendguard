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
import json
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


from . import equivalence
_flatten = equivalence._flatten          # kept for back-compat (tests)


def _equiv(ref, out, mode="auto", model=None):
    """Score 0..1 from the graded equivalence ladder (see equivalence.grade)."""
    return equivalence.grade(ref, out, mode=mode, model=model)[0]


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
        from openai import OpenAI
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


def experiment(intent, models=None, instructions=None, n=20, run=False, reconsider=False, mode="auto"):
    from . import models as M
    base_model = _base_model(intent)
    samples = _samples(intent, base_model, n) if base_model else []
    if not samples:
        print(f"experiment — no call_io samples for intent '{intent}'. Run `spendguard fetch-io` first.")
        return dict(ok=False)
    # soft denylist: skip models already known-ineffective for this intent (unless --reconsider)
    keep_models, skipped = [], []
    for m in (models or []):
        bad = M.ineffective(m, intent)
        if bad and not reconsider:
            skipped.append((m, bad))
        else:
            keep_models.append(m)
    for m, (reason, conf, ts) in skipped:
        print(f"  ⊘ skipping {m} — known ineffective for '{intent}' ({reason}; {(ts or '')[:10]}). "
              f"--reconsider to retest (it may have improved).")
    variants = _variants(intent, base_model, keep_models, instructions)

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
        from . import ui; ui.estimate_only(action="run the A/B experiment", cost=est)
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
        costs, scores, tiers, struct = [], [], [], []
        for prompt, ref, _m in subset:
            try:
                mo = max(1500, int(_count_tokens(ref, v["model"]) * 2) + 800)
                cost, _it, _ot, text = _call(v["model"], prompt + v["instr"], max_out=mo)
                sc, tier = equivalence.grade(ref, text, mode=mode, model=v["model"])
                st = equivalence.structural(ref, text)
                costs.append(cost); scores.append(sc); tiers.append(tier)
                if st is not None:
                    struct.append(st)
            except Exception:
                pass
        return costs, scores, tiers, struct

    with calls.context(intent=f"{META}:experiment"):
        for v in variants:
            costs, scores, tiers, struct = _measure(v, samples[:pilot])
            killed = scores and (sum(scores) / len(scores)) < KILL_THRESH
            if not killed:                                  # expand survivors to the full sample
                c2, s2, t2, st2 = _measure(v, samples[pilot:])
                costs += c2; scores += s2; tiers += t2; struct += st2
            if not scores:
                print(f"  {v['label'][:21]:<22}{'ERR':>10}")
                continue
            n = len(scores)
            per = sum(costs) / n
            match = sum(scores) / n
            tier = max(set(tiers), key=tiers.count) if tiers else "?"
            fmt = (100 * sum(struct) / len(struct)) if struct else None
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
            fmt_s = f" · format {fmt:.0f}%" if fmt is not None else ""
            print(f"  {v['label'][:21]:<22}{('$%.5f' % per):>10}{('%+.0f%%' % -drop):>13}"
                  f"{('%.0f%% ±%.0f%% (N=%d)' % (100*match, 100*stderr, n)):>16}  {verdict}")
            print(f"      via {tier} equivalence{fmt_s}")
            results.append(dict(label=v["label"], per=per, drop=drop, match=match, stderr=stderr,
                                n=n, killed=killed, tier=tier, format=fmt))
            # self-learning denylist: a killed MODEL-swap is remembered as ineffective for this intent
            if killed and v["label"].startswith("model:"):
                M.mark_ineffective(v["model"], intent, f"{int(100*match)}% output-match at pilot (N={n})")
                print(f"      ↳ marked {v['model']} ineffective for '{intent}' — auto-skipped next time.")
    print("\n  adopt only variants that are cheaper AND keep output (match ≥ 97%). Wide ±stderr or small N "
          "= inconclusive — run more samples (`--n`) before trusting it. Losers were killed at the pilot to cap waste.")
    return dict(ok=True, results=results)


def _meta():
    from . import budget
    try:
        return budget.meta_spent_today()
    except Exception:
        return 0.0


def _read_inputs(path):
    """Prompts from a chunk file: a batch .jsonl (OpenAI body.messages / Anthropic params.messages),
    {"prompt":...} per line, or a plain text file (one prompt per line). Returns [(custom_id, prompt)]."""
    items = []
    for ln in open(path, errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            items.append((None, ln)); continue
        cid = o.get("custom_id")
        body = o.get("body") or o.get("params") or o
        prompt = None
        for m in (body.get("messages") or []):
            if (m.get("role") if isinstance(m, dict) else None) == "user":
                c = m.get("content"); prompt = c if isinstance(c, str) else json.dumps(c)
        prompt = prompt if prompt is not None else (o.get("prompt") or "")
        items.append((cid, prompt))
    return items


def _promote_batch(intent, model, instr, items, run):
    """Promote at SCALE via the Batch API (50% off, async) — the real 25K-chunk path. Per-model params
    auto-applied; estimate+cap+submit reuse the guarded submit / gate. WORKLOAD spend (real intent)."""
    from . import adapters, models as M
    import tempfile, os
    prov = adapters.provider_for(model)
    if len(items) > 25000:
        print(f"  ⚠ {len(items):,} requests > 25K batch limit — chunk it (submitting first 25K).")
        items = items[:25000]
    reqs_body = [(cid or f"i{idx}",
                  M.apply_call_params(model, {"model": model, "max_tokens": 1500,
                                              "messages": [{"role": "user", "content": p + instr}]}))
                 for idx, (cid, p) in enumerate(items)]
    print(f"promote (BATCH) — winner {model} ({prov}) on {len(reqs_body):,} requests for '{intent}', "
          f"KEEP output as production")
    if prov == "openai":
        fd, path = tempfile.mkstemp(suffix=".jsonl", prefix=f"promote_{intent.replace('/', '_')}_")
        with os.fdopen(fd, "w") as f:
            for cid, body in reqs_body:
                f.write(json.dumps({"custom_id": cid, "method": "POST",
                                    "url": "/v1/chat/completions", "body": body}) + "\n")
        from .submit import guarded_submit
        with calls.context(intent=intent):            # WORKLOAD spend (real intent → your caps)
            bid = guarded_submit(path, model=model, cap_dollars=config.cap(), submit=run)
        if run:
            print(f"  jsonl: {path}\n  submitted batch {bid}; retrieve results with `spendguard fetch-io` "
                  f"or the batch output file when complete.")
        else:
            print(f"  jsonl: {path}")
            from . import ui; ui.estimate_only(action="submit the batch", note="WORKLOAD caps apply")
        return dict(ok=True, provider=prov, jsonl=path, batch=bid if run else None)
    else:  # anthropic — gate estimates + caps on create; submit under workload context
        reqs = [{"custom_id": cid, "params": body} for cid, body in reqs_body]
        if not run:
            from . import gate
            est = gate._estimate_anthropic_requests(reqs)
            print(f"  ESTIMATE: {est['requests']:,} req · in~{est['in_tok']:,} out≤{est['out_tok']:,} "
                  f"-> ~${est['cost']:.2f} (batch).")
            from . import ui; ui.estimate_only(action="submit the batch", cost=est["cost"], note="WORKLOAD caps apply")
            return dict(ok=True, provider=prov, est=est["cost"])
        import anthropic
        with calls.context(intent=intent):            # WORKLOAD; gate meters + caps on create
            b = anthropic.Anthropic(api_key=config.api_key("ANTHROPIC_API_KEY")).messages.batches.create(requests=reqs)
        print(f"  submitted Anthropic batch {b.id}; retrieve results when complete.")
        return dict(ok=True, provider=prov, batch=b.id)


def promote(intent, model, instruction="", input=None, out=None, n=None, run=False, batch=False):
    """Run a WINNING config on real inputs and KEEP the output — a successful test IS production work,
    not wasted spend. Runs under the REAL intent → WORKLOAD caps (not the meta cage). Estimate-first.
    --batch uses the Batch API (50% off, async) for true large chunks; else realtime for slices."""
    instr = ("\n\n" + instruction) if instruction else ""
    if input:
        items = _read_inputs(input)
    else:                                              # fall back to the slice we already have for the intent
        base = _base_model(intent)
        items = [(None, p) for p, _r, _m in _samples(intent, base, n or 50)]
    if n:
        items = items[:n]
    if not items:
        print(f"promote — no inputs (give --input <chunk.jsonl> or fetch-io samples for '{intent}').")
        return dict(ok=False)
    if batch:
        return _promote_batch(intent, model, instr, items, run)
    est = sum(pricing.realtime_cost(model, _count_tokens(p + instr, model), max(50, _count_tokens(p, model) // 2))
              for _c, p in items)
    print(f"promote — run winner {model} on {len(items)} inputs for '{intent}', KEEP output as production")
    print(f"  ⚠ this is WORKLOAD spend (real intent → your normal caps, NOT the meta cage). est ~${est:.4f}")
    if not run:
        from . import ui; ui.estimate_only(action="produce + keep the output", cost=est, note="WORKLOAD spend")
        return dict(ok=True, est=est, items=len(items))
    out = out or f"promote_{intent.replace('/', '_')}.jsonl"
    kept, tot = 0, 0.0
    with calls.context(intent=intent):                 # REAL intent → workload caps (this is production)
        with open(out, "w") as f:
            for cid, p in items:
                try:
                    cost, _it, _ot, text = _call(model, p + instr, max_out=1500)
                    f.write(json.dumps({"custom_id": cid, "output": text}) + "\n")
                    kept += 1; tot += cost
                except Exception:
                    pass
    print(f"  kept {kept}/{len(items)} outputs → {out}  (${tot:.4f} — production work, not wasted).")
    print("  (for a full 25K-request chunk use the Batch API path; this realtime promote suits meaningful slices.)")
    return dict(ok=True, kept=kept, out=out, cost=tot)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard experiment")
    ap.add_argument("--intent", required=True)
    ap.add_argument("--model", action="append", help="a cheaper model variant to test (repeatable)")
    ap.add_argument("--instruction", action="append", help="an output-format instruction variant (repeatable)")
    ap.add_argument("--n", type=int, default=20, help="samples to test per variant")
    ap.add_argument("--reconsider", action="store_true", help="also test models previously marked ineffective")
    ap.add_argument("--semantic", help="judge tier: embed | rubric | custom:<module.fn> (your own callable "
                    "(ref,out)->0..1 — wrap promptfoo assertions or any domain check). For PROSE outputs: a caged semantic "
                    "equivalence tier (embed=embedding cosine, rubric=LLM judge) — costs extra, meta-capped")
    ap.add_argument("--run", action="store_true", help="actually call (default: estimate). Caged by caps.meta.")
    a = ap.parse_args(argv)
    experiment(a.intent, models=a.model, instructions=a.instruction, n=a.n, run=a.run,
               reconsider=a.reconsider, mode=a.semantic or "auto")
    return 0


def promote_main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard promote")
    ap.add_argument("--intent", required=True)
    ap.add_argument("--model", required=True, help="the winning model")
    ap.add_argument("--instruction", help="the winning output-format instruction (optional)")
    ap.add_argument("--input", help="chunk to process (batch .jsonl / {prompt} jsonl / plain text)")
    ap.add_argument("--out", help="where to write kept outputs (jsonl) — realtime mode")
    ap.add_argument("--n", type=int, help="cap inputs")
    ap.add_argument("--batch", action="store_true", help="use the Batch API (50%% off, async) for large chunks")
    ap.add_argument("--run", action="store_true", help="actually produce+keep (default: estimate). WORKLOAD spend.")
    a = ap.parse_args(argv)
    promote(a.intent, a.model, instruction=a.instruction or "", input=a.input, out=a.out, n=a.n,
            run=a.run, batch=a.batch)
    return 0
