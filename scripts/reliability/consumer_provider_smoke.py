#!/usr/bin/env python3
"""A of the reliability program — the CONSUMER x PROVIDER SEAM smoke matrix, MEASURE-THEN-PROJECT.

The sweep (`spendguard reliability`) proves every lane + metered key is REACHABLE with a one-word ping; this proves the
next thing the ping can't: that each provider DELIVERS THE SHAPE a real consumer asks for — a strict JSON schema, a
plain one-shot, or an embedding vector — or FAILS LOUD, never a silent-wrong / silent-lost. That seam is where this
session's real bugs lived (whole_job's prompt_for; healiom's union-type schema; the with_raw_response usage artifact and
the gemini/voyage embed crash this very harness surfaced), and it is the layer the offline suite cannot reach because it
stubs the seams.

SEAMS, NOT one row per named consumer: two consumers with the same call shape share the same seam. The matrix is
{distinct call shape} x {provider that shape rides}:
  · strict  — a required/nonempty/enum/UNION JSON schema on the METERED path (honestreview panel, healiom, warden).
  · lenient — a plain one-shot that RIDES this provider's $0 lane (comprehend/describe); metered fallback if down.
  · embed   — an embeddings call (7thsense) on each embedding-capable provider.
EVERY target is catalog-derived, NEVER hardcoded: chat providers/lanes from `reliability.plan()`; embedding providers +
models from `model_catalog.embedding_models()` (the embed-model SSOT). An empty slate FAILS LOUD.

COST IS MEASURED, NEVER INVENTED (the estimate-literals doctrine — the defect that quoted this run at $0.0065 when the
with_raw_response usage bug inflated the recorded figure to $2.77). No estimate is built from assumed token counts:
  1. `--measure` runs ONE probe per distinct chat model + per embed model and writes each one's ACTUAL tokens to the
     sample file (chat: real in/out from the reply — spendguard's output_budget owns the cap, so this is cheap; embed:
     input tokens counted from the probe text by the same tokenizer pricing uses, output is 0 by construction).
  2. default (no flag) PROJECTS the full-matrix $ from that measured sample (measured tokens x canonical pricing) — $0,
     no provider call, no invented literal. FAILS LOUD if the sample is missing OR does not cover every priced cell.
  3. `--run` executes the full matrix once and prints the delivered | loud-failure | SILENT-WRONG scorecard. A
     SILENT-WRONG is the finding this matrix exists to surface.

Run under the gated venv (`spendguard doctor` == ENFORCING HERE: YES).
"""
import argparse
import json
import os
import sys

STRICT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["label", "score"], "nonempty": ["label"],
    "properties": {"label": {"type": "string", "enum": ["yes", "no", "unknown"]},
                   "score": {"type": ["integer", "null"], "minimum": 0, "maximum": 10}},
}
STRICT_PROMPT = ('Reply with ONLY a JSON object {"label": one of yes|no|unknown, "score": integer 0-10 or null}. '
                 'Is 2+2 equal to 4? Answer label=yes, score=10.')
LENIENT_PROMPT = "Reply with ONE word only: the color of a clear daytime sky."
EMBED_TEXT = "spendguard consumer-provider seam smoke probe"
INTENT = "spendguard:consumer-provider-smoke"


def _sample_path(cli_value):
    """Where the measured sample lives. Default: under spendguard HOME (a spendguard artifact, not source, not /tmp)."""
    if cli_value:
        return cli_value
    from spendguard import config
    return os.path.join(str(config.HOME), "consumer_provider_smoke_sample.json")


def _embed_cells():
    """Embedding cells from the catalog SSOT (model_catalog.embedding_models — no hardcoded ids), one per embedding-
    capable provider that is KEYED (so a cell is not added for a provider the user cannot reach), cheapest model first.
    Returns [(provider, 'provider:model')]."""
    from spendguard import model_catalog, adapters, config
    out, seen = [], set()
    for prov, mid in model_catalog.embedding_models():
        if prov in seen:
            continue
        spec = adapters.PROVIDERS.get(prov) or {}
        key_env = spec.get("key_env")
        if key_env and config.api_key(key_env):
            seen.add(prov)
            out.append((prov, mid))          # BARE model id, uniform with chat cells; the 'provider:' prefix is added
            #                                   only at the adapters.embed / pricing call sites (never stored prefixed)
    return out


def _slate():
    """(cells, plan) from catalog-derived authorities — never re-derived here. cells is [(shape, provider, lane_or_none,
    model)]: strict+lenient per metered provider + lane per lane (reliability.plan), and embed per keyed embedding
    provider (model_catalog.embedding_models). RAISES if the whole slate is empty (a slate that tested nothing must fail
    loud, not report a green matrix over zero cells)."""
    from spendguard import reliability, adapters
    pl = reliability.plan()
    lane_provider = {lane: prov for prov, (lane, _m) in adapters._LANES.items()}
    cells = []
    for prov, model in pl["metered"]:
        cells.append(("strict", prov, None, model))
        cells.append(("lenient", prov, None, model))
    for lane, model in pl["lanes"]:
        cells.append(("lane", lane_provider.get(lane), lane, model))
    for prov, model in _embed_cells():
        cells.append(("embed", prov, None, model))
    if not cells:
        raise RuntimeError("no lanes, no metered providers, and no keyed embedding models — the catalog/config is "
                           "unavailable or unkeyed; refusing to report a matrix over zero cells (fail loud, not open)")
    return cells, pl


def _priced_cells(cells):
    """The cells whose cost must be MEASURED before a projection: chat (strict/lenient — keyed by chat:<prov>:<model>,
    one measurement per model covers both shapes) and embed (embed:<prov>:<model>). Lane cells are $0. Returns
    [(sample_key, shape_kind, provider, model)] with shape_kind in {'chat','embed'}."""
    seen, out = set(), []
    for shape, prov, _lane, model in cells:
        kind = "embed" if shape == "embed" else ("chat" if shape in ("strict", "lenient") else None)
        if not kind:
            continue
        key = f"{kind}:{prov}:{model}"
        if key in seen:
            continue
        seen.add(key)
        out.append((key, kind, prov, model))
    return out


def _embed_in_tokens(text):
    """Input tokens for an embedding call, COUNTED from the text by the same tokenizer pricing uses — a measurement of
    the text, not an invented literal. Embeddings have no output, so out is 0 by construction."""
    from spendguard import gate
    return int(gate._content_tokens(text, provider="openai"))


def _is_label(text):
    """EXACT enum-conformance of the strict reply (the parsed object's `label` IS one of the closed set) — a format
    check on a fixed shape, not a meaning judgement (parsing, not a decision)."""
    try:
        obj = json.loads(text) if isinstance(text, str) else text
    except Exception:
        return False
    return isinstance(obj, dict) and obj.get("label") in ("yes", "no", "unknown")


def measure(cells, sample_path):
    """MEASURE step — one probe per distinct priced cell, writing each one's ACTUAL tokens to `sample_path`. Chat probes
    make a small real call (output is tiny by nature and spendguard's output_budget owns the cap, so the pass is cheap
    — this harness never decides a max_tokens, that is output_budget's home); embed probes make a real embeddings call
    to confirm delivery and record the tokenizer-counted input (output 0). The ONLY place the harness learns token
    counts — measured, never invented. Prints what it measured; returns 0."""
    from spendguard import adapters
    sample = {}
    for key, kind, prov, model in _priced_cells(cells):
        if kind == "chat":
            # Measure the STRICT shape (schema + JSON reply) — the MORE expensive of the two chat shapes this model
            # rides — so projecting both strict and lenient cells from it errs HIGH (a conservative ceiling that never
            # surprises above), never low. Measured tokens either way; the shape choice sets the direction of the slack.
            r = adapters.call(f"{prov}:{model}", STRICT_PROMPT, schema=STRICT_SCHEMA, intent=INTENT,
                              metered_only=True, no_substitution=True, timeout_s=90)
            sample[key] = {"provider": prov, "model": model, "kind": kind,
                           "in": int(r.get("in_tok") or 0), "out": int(r.get("out_tok") or 0),
                           "error": (str(r.get("error"))[:80] if r.get("error") else None)}
            print(f"  measured chat  {prov:<10} {model:<24} in={r.get('in_tok')} out={r.get('out_tok')}"
                  f"{'  ERROR ' + str(r.get('error'))[:40] if r.get('error') else ''}")
        else:  # embed — input tokens are counted from the text; the call confirms the seam works
            r = adapters.embed([EMBED_TEXT], model=f"{prov}:{model}")
            sample[key] = {"provider": prov, "model": model, "kind": kind,
                           "in": _embed_in_tokens(EMBED_TEXT), "out": 0,
                           "error": (str(r.get("error"))[:80] if r.get("error") else None)}
            print(f"  measured embed {prov:<10} {model:<24} in={_embed_in_tokens(EMBED_TEXT)} out=0"
                  f"{'  ERROR ' + str(r.get('error'))[:40] if r.get('error') else '  dims=' + str(r.get('dims'))}")
    with open(sample_path, "w") as fh:
        json.dump(sample, fh, indent=2)
    print(f"\nwrote measured sample → {sample_path}\nnow project the full matrix cost ($0):  "
          f"{os.path.basename(sys.argv[0])}    (then --run)")
    return 0


def project(cells, sample_path):
    """PROJECT step — the full-matrix $ from the MEASURED sample, $0 and no provider call. Each priced cell's cost is
    pricing.realtime_cost(model, MEASURED_in, MEASURED_out) — token counts from the sample file, never a literal (an
    embed cell's out is a real 0: no output). Lane cells are $0 (metered fallback only if the lane is down). FAILS LOUD
    (returns 3) if the sample is missing OR does not cover every priced cell — a projection over an incomplete
    measurement is the invented-number defect in another form (a partial total that reads as valid), so it names the
    uncovered cells rather than under-reporting the total and exiting 0."""
    from spendguard import pricing
    if not os.path.exists(sample_path):
        print(json.dumps({"phase": "project", "error": "no measured sample — run --measure first (the spend protocol: "
                          "measure real tokens, THEN project)", "sample_path": sample_path}, indent=2))
        return 3
    with open(sample_path) as fh:
        sample = json.load(fh)
    priced = {key: (prov, model) for key, _kind, prov, model in _priced_cells(cells)}
    missing = [key for key in priced if key not in sample]
    if missing:
        print(json.dumps({"phase": "project", "error": "sample does not cover every priced cell — re-run --measure",
                          "uncovered": sorted(missing), "sample_path": sample_path}, indent=2))
        return 3
    rows, total = [], 0.0
    for shape, prov, lane, model in cells:
        if shape == "lane":
            rows.append({"shape": shape, "provider": prov, "lane": lane, "usd": 0.0,
                         "note": "$0 while the lane is up; tiny metered fallback only if down"})
            continue
        key = f"{'embed' if shape == 'embed' else 'chat'}:{prov}:{model}"
        s = sample[key]
        c = pricing.realtime_cost(f"{prov}:{model}", s["in"], s["out"])   # MEASURED tokens from the sample, not literals
        total += (c or 0.0)
        rows.append({"shape": shape, "provider": prov, "model": model, "in": s["in"], "out": s["out"],
                     "usd": (round(c, 6) if c else None)})
    print(json.dumps({"phase": "project", "cells": len(cells), "projected_total_usd": round(total, 4),
                      "basis": "MEASURED tokens x canonical pricing (no invented literals)",
                      "note": "approve, then --run for the live delivered-or-loud scorecard", "rows": rows}, indent=2))
    return 0


# The three terminal verdict CATEGORIES a cell can land in — a STRUCTURED value the run carries and counts, never
# re-derived from the diagnostic string. A prefix match on a self-generated message is a decision a format change can
# silently break: leading whitespace before "SILENT-WRONG" would make startswith miss it and A_PROVEN wrongly read true.
DELIVERED, LOUD, SILENT_WRONG = "delivered", "loud", "silent_wrong"


def _run_strict(prov, model):
    """(category, detail, usd): DELIVERED (an in-contract label), LOUD (a typed error — acceptable, not silent), or
    SILENT_WRONG (no error yet not an in-contract label — the outcome this matrix exists to catch)."""
    from spendguard import adapters
    r = adapters.call(f"{prov}:{model}", STRICT_PROMPT, schema=STRICT_SCHEMA, intent=INTENT,
                      metered_only=True, no_substitution=True, timeout_s=60)
    usd = float(r.get("cost") or 0.0)
    if r.get("error"):
        return LOUD, f"loud-failure ({str(r.get('error'))[:60]})", usd
    if _is_label(r.get("text")):
        return DELIVERED, "delivered", usd
    return SILENT_WRONG, f"no error, reply not an in-contract label: {str(r.get('text'))[:50]!r}", usd


def _run_text(model, *, metered_only, no_substitution):
    """(category, detail, usd) for a plain one-shot: DELIVERED (nonempty text), LOUD (typed error), SILENT_WRONG
    (empty with no error)."""
    from spendguard import adapters
    r = adapters.call(model, LENIENT_PROMPT, intent=INTENT, metered_only=metered_only,
                      no_substitution=no_substitution, timeout_s=90)
    usd = float(r.get("cost") or 0.0)
    if (r.get("text") or "").strip():
        return DELIVERED, "delivered", usd
    if r.get("error"):
        return LOUD, f"loud-failure ({str(r.get('error'))[:60]})", usd
    return SILENT_WRONG, "empty, no error", usd


def _run_embed(prov, model):
    """(category, detail, usd) for an embedding call: DELIVERED (a non-empty vector), LOUD (error / failed item),
    SILENT_WRONG (no vector, no error)."""
    from spendguard import adapters, pricing
    spec = f"{prov}:{model}"
    r = adapters.embed([EMBED_TEXT], model=spec)
    usd = pricing.realtime_cost(spec, _embed_in_tokens(EMBED_TEXT), 0) or 0.0     # embeddings: input-only, out is a real 0
    vecs = r.get("vectors") or []
    if r.get("error"):
        return LOUD, f"loud-failure ({str(r.get('error'))[:60]})", usd
    if vecs and vecs[0]:
        return DELIVERED, "delivered", usd
    if r.get("failed"):
        return LOUD, f"loud-failure (embed failed: {str(r['failed'][:1])[:60]})", usd
    return SILENT_WRONG, "no vector, no error", usd


def run_live(cells):
    """Execute the live matrix — INCURS METERED SPEND for strict/lenient/embed cells (lane cells $0 unless a lane is
    down). Each cell yields a STRUCTURED category (DELIVERED | LOUD | SILENT_WRONG; an unexpected exception counts LOUD),
    which is what silent_wrong is counted from — never a re-parse of the diagnostic string. Prints per-cell progress +
    the ACTUAL measured cost and a final JSON summary. RETURNS 0 when NO cell was SILENT_WRONG (A proven) and 4
    otherwise. `cells` is _slate()'s [(shape, provider, lane, model)]."""
    scorecard, silent_wrong, total = [], 0, 0.0
    for shape, prov, lane, model in cells:
        target = lane or prov
        try:
            if shape == "strict":
                cat, detail, usd = _run_strict(prov, model)
            elif shape == "lenient":
                cat, detail, usd = _run_text(f"{prov}:{model}", metered_only=True, no_substitution=True)
            elif shape == "lane":
                cat, detail, usd = _run_text(f"{prov}:{model}", metered_only=False, no_substitution=True)
            else:  # embed
                cat, detail, usd = _run_embed(prov, model)
        except Exception as e:
            cat, detail, usd = LOUD, f"loud-failure (raised {type(e).__name__}: {str(e)[:50]})", 0.0
        if cat == SILENT_WRONG:
            silent_wrong += 1
        total += usd
        mark = "OK " if cat in (DELIVERED, LOUD) else "XX "
        verdict = detail if cat in (DELIVERED, LOUD) else f"SILENT-WRONG ({detail})"
        print(f"  [{mark}] {shape:8} {target:14} ${usd:<9.5f} {verdict}")
        scorecard.append({"shape": shape, "target": target, "model": model, "usd": round(usd, 6),
                          "category": cat, "verdict": verdict})
    proven = silent_wrong == 0
    print(json.dumps({"phase": "run", "cells": len(cells), "silent_wrong": silent_wrong, "A_PROVEN": proven,
                      "actual_total_usd": round(total, 4),
                      "note": ("every seam DELIVERED or FAILED LOUD across its providers — no silent-wrong/lost"
                               if proven else f"{silent_wrong} SILENT-WRONG cell(s) — a seam returned a non-answer with "
                               "no error; inspect the scorecard above"),
                      "scorecard": scorecard}, indent=2))
    return 0 if proven else 4


def main():
    import spendguard
    spendguard.require()                                   # fail closed: halt unless the gate is ENFORCING here
    ap = argparse.ArgumentParser(description="A — consumer x provider SEAM smoke matrix (measure-then-project)")
    ap.add_argument("--measure", action="store_true", help="run probes to MEASURE real per-model tokens → sample file")
    ap.add_argument("--run", action="store_true", help="execute the live matrix; delivered-or-loud scorecard")
    ap.add_argument("--sample", help="sample file path (default: under spendguard HOME)")
    a = ap.parse_args()
    cells, _pl = _slate()
    sample_path = _sample_path(a.sample)
    if a.measure:
        return measure(cells, sample_path)
    if a.run:
        return run_live(cells)
    return project(cells, sample_path)                      # default: $0 projection from the measured sample


if __name__ == "__main__":
    sys.exit(main())
