"""Does a four-vendor review panel answer, every time, inside a budget? Run this before trusting one.

WHAT IT PROVES, AND WHY EACH PART IS THERE
  * ROUNDS, not one call. "It works" is a RATE. A single green call cannot tell a vendor that answers always
    from one that answers 4 times in 5, and the second is the one that ruins a batch at item 400.
  * MEASURED budgets. Each vendor gets time_budget(), sized from its own recorded p99. A global deadline is
    generous and marginal at the same time: measured p99s here span 49s (anthropic) to 446s (moonshot), and
    a hardcoded 180s silently converted the two slow vendors' normal tail into `deadline_exceeded`.
  * CONCURRENT. fan_out now runs vendors in parallel, so the panel costs the slowest vendor, not the sum.
  * first_ok alongside it, because the two answer different questions: fan_out asks "did everyone agree to
    speak", first_ok asks "do I have an answer yet". A classification job wants the second and should not
    pay the straggler's latency for it.

READ THE FAILURES BY KIND — they have different remedies and must never be merged into one "error" bucket:
    transport_error     transient; retried inside call()
    deadline_exceeded   the BUDGET was wrong (or the vendor is down) — never retry it at the same budget
    empty / truncated   the CAP was wrong; gate.autotune raises it from measurement
    refused             the model declined; a different prompt, not a different vendor
"""
import argparse, json, os, statistics, sys, time

import spendguard                                   # noqa: F401
spendguard.require()
from spendguard import bulkgate, config, vendor_call as vc   # noqa: E402

PANEL = [("anthropic", "claude-opus-4-8"), ("openai", "gpt-5.5"),
         ("moonshot", "kimi-k3"), ("zai", "glm-5.2")]

# A real review-shaped task: a short diff and a contract to answer against. Kept small on purpose — this
# probe measures whether the PANEL answers, not whether the models are good reviewers.
DIFF = """def apply_discount(price, pct):
    if pct > 100:
        pct = 100
    return price - price * pct / 100
"""
PROMPT = ("Review this function and list every correctness issue you find, one per line, "
          "each with the line number. If there are none, say NONE.\n\n" + DIFF)
SYSTEM = "You are a code reviewer. Be specific and brief."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="panels to run; reliability is a rate over these")
    ap.add_argument("--mode", choices=("fan_out", "first_ok", "both"), default="both")
    ap.add_argument("--need", type=int, default=2, help="first_ok: how many answers are enough")
    ap.add_argument("--fallback-s", type=float, default=180.0,
                    help="budget used only where NOTHING is measured yet; measured always wins")
    ap.add_argument("--estimate", action="store_true")
    a = ap.parse_args()
    os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = "api"      # measure the API path, not a subscription lane

    print("measured time budgets (each vendor's own p99 x slack, floored and ceiled):")
    for v, m in PANEL:
        b, basis = vc.time_budget(v, m, default_s=a.fallback_s)
        d = bulkgate.latency(model=m) or {}
        print(f"  {v:<11}{m:<20}{(f'{b:.0f}s' if b else '—'):>8}   {basis:<24}"
              f"p50/p90/p99 {d.get('p50')}/{d.get('p90')}/{d.get('p99')}s")

    if a.estimate:
        from spendguard import pricing, expected_output
        tot = 0.0
        for v, m in PANEL:
            pred, _ = expected_output.expect(m)
            tot += pricing.realtime_cost(m, len(PROMPT) // 4, pred) * a.rounds
        print(f"\nZERO-SPEND ESTIMATE — {len(PANEL) * a.rounds} calls per mode, ~${tot:,.3f}")
        return 0

    out_path = os.path.join(str(config.HOME), "panel_reliability.jsonl")
    stats, rows = {}, []
    modes = ("fan_out", "first_ok") if a.mode == "both" else (a.mode,)
    for mode in modes:
        print(f"\n── {mode} × {a.rounds} rounds " + ("(waits for ALL)" if mode == "fan_out"
                                                    else f"(returns at {a.need} answers)"))
        waits = []
        for i in range(a.rounds):
            t0 = time.time()
            if mode == "fan_out":
                fan = vc.fan_out(PANEL, PROMPT, deadline_s=a.fallback_s, purpose="panel:review",
                                 system=SYSTEM)
            else:
                fan = vc.first_ok(PANEL, PROMPT, deadline_s=a.fallback_s, need=a.need,
                                  purpose="panel:review", system=SYSTEM)
            el = time.time() - t0
            waits.append(el)
            for r in fan["results"]:
                k = (mode, r.vendor)
                st = stats.setdefault(k, {"n": 0, "ok": 0, "kinds": {}})
                st["n"] += 1
                st["ok"] += 1 if r.ok else 0
                st["kinds"][r.kind] = st["kinds"].get(r.kind, 0) + 1
            bad = [f"{r.vendor}={r.kind}" for r in fan["results"] if not r.ok]
            print(f"  round {i + 1}: {fan['n_ok']}/{fan['n']} answered in {el:>6.1f}s   "
                  f"complete={fan['complete']}   {' '.join(bad) or ''}")
            rows.append({"mode": mode, "round": i + 1, "n": fan["n"], "n_ok": fan["n_ok"],
                         "complete": fan["complete"], "wall_s": round(el, 1),
                         "kinds": {r.vendor: r.kind for r in fan["results"]},
                         "latencies": {r.vendor: round(r.latency, 1) for r in fan["results"]}})
        print(f"  wall-clock p50 {statistics.median(waits):.1f}s   worst {max(waits):.1f}s")

    with open(out_path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    print(f"\n  {'mode':<10}{'vendor':<11}{'answered':>10}   kinds")
    for (mode, v), st in sorted(stats.items()):
        ks = " ".join(f"{k}:{n}" for k, n in sorted(st["kinds"].items()))
        print(f"  {mode:<10}{v:<11}{st['ok']}/{st['n']:<8}   {ks}")
    worst = [f"{v} {st['ok']}/{st['n']}" for (mode, v), st in stats.items() if st["ok"] < st["n"]]
    print(f"\n  {'ALL VENDORS ANSWERED EVERY ROUND' if not worst else 'NOT 100%: ' + ', '.join(worst)}")
    print(f"  rows -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
