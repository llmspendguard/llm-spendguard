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
import os, sys, json, argparse, datetime

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


def estimate_jsonl_cost(jsonl_path, model, batch=True, avg_out_tokens=None):
    """Project cost of an OpenAI /v1/chat/completions batch .jsonl. No paid calls."""
    n = 0
    in_tok = 0
    out_ceiling = 0
    measured = avg_out_tokens is not None
    used_heuristic = False
    try:
        import tiktoken  # noqa
    except Exception:
        used_heuristic = True
    for line in open(jsonl_path, errors="ignore"):
        line = line.strip()
        if not line:
            continue
        n += 1
        body = json.loads(line).get("body", {})
        text = ""
        for m in body.get("messages", []):
            c = m.get("content", "")
            text += c if isinstance(c, str) else json.dumps(c)
        in_tok += _count_tokens(text, model)
        out_ceiling += body.get("max_tokens", body.get("max_completion_tokens", 0) or 0)
    out_tok = int(avg_out_tokens * n) if measured else out_ceiling
    cost_fn = batch_cost if batch else realtime_cost
    cost = cost_fn(model, in_tok, out_tok)
    return dict(requests=n, in_tok=in_tok, out_tok=out_tok, cost=cost,
                out_basis=("measured avg" if measured else "max_tokens ceiling (conservative)"),
                token_basis=("char/4 heuristic — install tiktoken for accuracy" if used_heuristic else "tiktoken"),
                model=normalize(model), mode=("batch" if batch else "realtime"))


def guarded_submit(jsonl_path, model, cap_dollars, batch=True, avg_out_tokens=None,
                   expected_cost=None, submit=True, request_cap=25000):
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
    if expected_cost is not None and est["cost"] > expected_cost * 1.2:
        raise RuntimeError(f"REFUSED: projected ${est['cost']:,.2f} is >20% over your expected "
                           f"${expected_cost:,.2f} — re-check token assumptions before submitting.")

    os.makedirs(AUDIT_DIR, exist_ok=True)
    rec = dict(est); rec["jsonl"] = jsonl_path; rec["cap"] = cap_dollars; rec["expected"] = expected_cost
    audit_path = os.path.join(AUDIT_DIR, f"{os.path.basename(jsonl_path)}.gate.json")
    json.dump(rec, open(audit_path, "w"), indent=2)

    if not submit:
        print(f"[submit_gate] PASS (estimate only, submit=False). audit: {audit_path}")
        return None

    # passed the gate — submit via OpenAI
    from openai import OpenAI
    client = OpenAI(api_key=_api_key("OPENAI_API_KEY"))
    f = client.files.create(file=open(jsonl_path, "rb"), purpose="batch")
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
    if a.cap and est["cost"] > a.cap:
        print(f"\nWOULD REFUSE: ${est['cost']:,.2f} > cap ${a.cap:,.2f}")
        sys.exit(2)
    print(f"\nWOULD PASS (cap ${a.cap if a.cap else 'none'}).")


if __name__ == "__main__":
    main()
