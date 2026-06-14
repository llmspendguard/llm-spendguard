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

    prompt = a.prompt or (open(a.prompt_file).read() if a.prompt_file else None)
    if not prompt:
        print("need --prompt or --prompt-file")
        return 1
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    print(f"comparing {len(models)} models on one prompt (real calls, metered by the gate)…\n")

    rows = [(m, adapters.call(m, prompt, a.max_tokens, a.system)) for m in models]

    print(f"{'model':<24}{'provider':<11}{'lat(s)':>7}{'in':>8}{'out':>8}{'$cost':>11}  {'$/1k out':>9}")
    for m, r in rows:
        if r["error"]:
            print(f"{m:<24}{r['provider']:<11}{'—':>7}{'—':>8}{'—':>8}{'ERR':>11}  {r['error']}")
            continue
        cost = f"${r['cost']:.5f}" if r["cost"] is not None else "n/a"
        per1k = f"${r['cost']/max(r['out_tok'],1)*1000:.4f}" if r["cost"] is not None else "—"
        print(f"{m:<24}{r['provider']:<11}{r['latency']:>7.2f}{r['in_tok']:>8}{r['out_tok']:>8}{cost:>11}  {per1k:>9}")

    ok = [r for _, r in rows if not r["error"] and r["cost"] is not None]
    if ok:
        cheapest = min(ok, key=lambda r: r["cost"])
        fastest = min(ok, key=lambda r: r["latency"])
        print(f"\ncheapest: {cheapest['model']} (${cheapest['cost']:.5f})   fastest: {fastest['model']} ({fastest['latency']:.2f}s)")
        print("(quality is yours to judge — use --show to read the outputs)")

    if a.show:
        for m, r in rows:
            print(f"\n──────── {m} ────────")
            print(r["text"] if r["text"] else f"ERROR: {r['error']}")
    return 0
