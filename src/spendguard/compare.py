"""spendguard compare — run the same prompt across models and table cost + latency + output.

spendguard's angle is COST-PER-RESULT (deep evals are promptfoo's job). Makes REAL paid calls,
metered by the gate. Opt-in.

  spendguard compare --prompt "Explain X in 3 bullets" \\
      --models gpt-5.5,claude-opus-4-8,gemini-2.5-flash,deepseek-chat,qwen-max
  spendguard compare --prompt-file p.txt --models ... --max-tokens 800 --show
"""
import argparse
from . import adapters


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt")
    ap.add_argument("--prompt-file")
    ap.add_argument("--models", required=True, help="comma-separated; 'provider:model' to force a provider")
    ap.add_argument("--max-tokens", type=int, default=512)
    ap.add_argument("--system")
    ap.add_argument("--show", action="store_true", help="print each model's full output")
    a = ap.parse_args(argv)

    # A MISSING --prompt-file IS A USAGE ERROR, NOT A TRACEBACK. And the handle is closed.
    prompt = a.prompt
    if not prompt and a.prompt_file:
        try:
            with open(a.prompt_file) as _fh:
                prompt = _fh.read()
        except OSError as e:
            print(f"compare: cannot read --prompt-file {a.prompt_file!r} ({type(e).__name__}: {e})")
            return 1
    if not prompt:
        print("need --prompt or --prompt-file")
        return 1
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    print(f"comparing {len(models)} models on one prompt (real calls, metered by the gate)…\n")

    # A RESULT THAT IS NOT A DICT IS NOT A RESULT. r["error"] is indexed twice below, so an adapter that
    # returned None (or a shape without that key) raised TypeError/KeyError mid-table — after every model
    # in the list had already been called and paid for. Normalising here keeps the comparison printable.
    def _norm(m, r):
        if isinstance(r, dict) and "error" in r:
            return r
        return {"provider": "", "error": f"adapter returned {type(r).__name__} with no result shape",
                "cost": None, "latency": None, "in_tok": None, "out_tok": None, "text": None, "model": m}
    rows = [(m, _norm(m, adapters.call(m, prompt, a.max_tokens, a.system))) for m in models]

    print(f"{'model':<24}{'provider':<11}{'lat(s)':>7}{'in':>8}{'out':>8}{'$cost':>11}  {'$/1k out':>9}")
    for m, r in rows:
        if r["error"]:
            print(f"{m:<24}{r['provider']:<11}{'—':>7}{'—':>8}{'—':>8}{'ERR':>11}  {r['error']}")
            continue
        cost = f"${r['cost']:.5f}" if r["cost"] is not None else "n/a"
        per1k = f"${r['cost']/max(r['out_tok'],1)*1000:.4f}" if r["cost"] is not None else "—"
        # A row can be error-free and still be MISSING numbers — an adapter that returned no usage leaves
        # latency/in_tok/out_tok as None, and :>7.2f on None raises mid-table, after real calls were paid
        # for. Absent renders as '—' rather than taking the comparison down.
        _lat = f"{r['latency']:>7.2f}" if isinstance(r.get("latency"), (int, float)) else f"{'—':>7}"
        _in = f"{r['in_tok']:>8}" if isinstance(r.get("in_tok"), int) else f"{'—':>8}"
        _out = f"{r['out_tok']:>8}" if isinstance(r.get("out_tok"), int) else f"{'—':>8}"
        print(f"{m:<24}{r['provider']:<11}{_lat}{_in}{_out}{cost:>11}  {per1k:>9}")

    ok = [r for _, r in rows if not r["error"] and r["cost"] is not None]
    if ok:
        cheapest = min(ok, key=lambda r: r["cost"])
        # `ok` is filtered on cost, not latency, so a row with cost and no latency reached min() and
        # compared None against a float. Fastest is drawn from the rows that actually HAVE a latency.
        _timed = [r for r in ok if isinstance(r.get("latency"), (int, float))]
        fastest = min(_timed, key=lambda r: r["latency"]) if _timed else None
        print(f"\ncheapest: {cheapest['model']} (${cheapest['cost']:.5f})"
              + (f"   fastest: {fastest['model']} ({fastest['latency']:.2f}s)" if fastest
                 else "   fastest: — (no model reported a latency)"))
        print("(quality is yours to judge — use --show to read the outputs)")

    if a.show:
        for m, r in rows:
            print(f"\n──────── {m} ────────")
            # `is not None`: a model that legitimately returned an EMPTY completion is not an error, and
            # printing "ERROR: None" for it invents a failure that did not happen.
            print(r["text"] if r.get("text") is not None else f"ERROR: {r['error']}")
    return 0
