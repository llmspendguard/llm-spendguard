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
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except Exception:
            enc = tiktoken.get_encoding("o200k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)  # heuristic fallback; flagged in result


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


def guarded_submit(jsonl_path, model, cap_dollars, batch=True, avg_out_tokens=None,
                   expected_cost=None, submit=True, request_cap=25000,
                   overrun_tolerance=DEFAULT_OVERRUN_TOLERANCE):
    """Estimate -> enforce cap -> log -> submit. Raises RuntimeError if it won't pass."""
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
    b = client.batches.create(input_file_id=f.id, endpoint="/v1/chat/completions", completion_window="24h")
    print(f"[submit_gate] SUBMITTED batch {b.id} (projected ${est['cost']:,.2f}). "
          f"Verify after: reconcile_openai_spend.py --estimate {est['cost']:.2f}")
    return b.id


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
