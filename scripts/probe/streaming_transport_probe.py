"""D — does NON-STREAMING cause APIConnectionError on GLM/Kimi with long prompts?

A controlled probe, not a hypothesis. The prompt is held FIXED (~14k chars, the size that failed in the real
harness); the only thing that varies is `stream=True|False`. N per cell is a parameter. Every call is bounded
by its own hard timeout so a hang costs one cell, not the run.

--estimate is a SEPARATE zero-spend pass: it counts the work and prices it from pricing.py, and stops.
"""
import argparse, json, os, sys, time

import spendguard                                    # noqa: F401 — the gate
spendguard.require()
from spendguard import adapters, config, pricing     # noqa: E402

CELLS = [("moonshot", "kimi-k3"), ("zai", "glm-5.2")]
PROMPT_CHARS = 14_000
OUT_TOK = 2_000                                      # enough output to exercise a long generation
TIMEOUT_S = 180


def build_prompt():
    """~14k chars of real, varied text — a code-review-shaped ask, matching what actually failed."""
    body = ("def handle(rec):\n    # step\n    x = rec.get('a')\n    if x is None: return None\n"
            "    return {'k': x, 'n': len(str(x))}\n\n")
    src = body * (PROMPT_CHARS // len(body) + 1)
    return ("Review this Python and list every correctness issue you find, one per line, "
            "with the line number and a one-sentence consequence.\n\n" + src)[:PROMPT_CHARS]


def one(vendor, model, prompt, stream):
    from openai import OpenAI
    spec = adapters.PROVIDERS[vendor]
    c = OpenAI(api_key=config.api_key(spec["key_env"]), base_url=spec["base_url"], timeout=TIMEOUT_S,
               max_retries=0)
    t0 = time.time()
    try:
        if stream:
            txt, usage = [], None
            s = c.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                          max_tokens=OUT_TOK, stream=True,
                                          stream_options={"include_usage": True})
            for ch in s:
                if getattr(ch, "usage", None):
                    usage = ch.usage
                if ch.choices and ch.choices[0].delta and ch.choices[0].delta.content:
                    txt.append(ch.choices[0].delta.content)
            out = "".join(txt)
        else:
            r = c.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}],
                                          max_tokens=OUT_TOK)
            out, usage = (r.choices[0].message.content or ""), r.usage
        return {"ok": True, "chars": len(out), "s": round(time.time() - t0, 1),
                "in_tok": getattr(usage, "prompt_tokens", 0) or 0,
                "out_tok": getattr(usage, "completion_tokens", 0) or 0, "err": None}
    except Exception as e:
        return {"ok": False, "chars": 0, "s": round(time.time() - t0, 1), "in_tok": 0, "out_tok": 0,
                "err": type(e).__name__}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--estimate", action="store_true", help="zero-spend: count + price the work, then stop")
    a = ap.parse_args()
    prompt = build_prompt()
    in_tok_est = len(prompt) // 4
    if a.estimate:
        total = 0.0
        print(f"ZERO-SPEND ESTIMATE — prompt {len(prompt):,} chars (~{in_tok_est:,} tok), out cap {OUT_TOK:,}")
        for v, m in CELLS:
            n = a.n * 2                                          # streaming + non-streaming
            c = pricing.realtime_cost(m, in_tok_est * n, OUT_TOK * n)
            total += c
            print(f"  {v:<10} {m:<12} {n:>3} calls  ${c:,.3f}")
        print(f"  {'TOTAL':<24} {len(CELLS) * a.n * 2:>3} calls  ${total:,.3f}   (worst case: every call "
              f"runs to the {OUT_TOK:,}-token cap)")
        return 0
    rows = []
    for v, m in CELLS:
        for stream in (False, True):
            for i in range(a.n):
                r = one(v, m, prompt, stream)
                r.update(vendor=v, model=m, stream=stream, i=i)
                rows.append(r)
                print(f"  {v:<10} {m:<12} stream={str(stream):<5} #{i+1} "
                      f"{'ok' if r['ok'] else r['err']:<22} {r['s']:>6.1f}s  {r['chars']:>6} chars",
                      flush=True)
    out = os.path.join(str(config.HOME), "streaming_probe.json")
    json.dump(rows, open(out, "w"), indent=1)
    print(f"\nrows -> {out}")
    print(f"\n  {'cell':<26}{'ok':>4}{'fail':>6}{'median s':>10}{'errors':>28}")
    for v, m in CELLS:
        for stream in (False, True):
            c = [r for r in rows if r["vendor"] == v and r["stream"] == stream]
            ok = [r for r in c if r["ok"]]
            errs = sorted({r["err"] for r in c if not r["ok"]})
            med = sorted(r["s"] for r in c)[len(c) // 2] if c else 0
            print(f"  {v + '/' + ('stream' if stream else 'non-stream'):<26}{len(ok):>4}{len(c) - len(ok):>6}"
                  f"{med:>10.1f}{', '.join(errs) or '—':>28}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
