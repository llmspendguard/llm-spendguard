"""Ground truth for the Claude/Codex subscription lanes: are they available, do they answer a real prompt,
and — the actual reliability question — do they survive the concurrency a fan_out panel imposes?

The honestreview panel runs vendors concurrently across files, so if files-at-once=N the anthropic lane sees
N concurrent `claude -p` and the openai lane N concurrent `codex exec`. A lane that works single-shot but
429s / serializes / flaps under burst is exactly the "unreliable" the user reports — and a flap is worse than
a hard-down lane, because each failure cools the lane and silently diverts that vendor to the metered API.

Zero API spend: every call here is plan-billed ($0 on the billed axis, kind='subscription'). Tiny prompts.

    .venv.nosync/bin/python scripts/diag/probe_subscription_lanes.py [concurrency]   # default 4
"""
import concurrent.futures as cf
import sys
import time

import spendguard  # gate: fail closed if this interpreter is not enforcing
spendguard.require()

from spendguard import subscription_exec, codex_exec

CONCURRENCY = int(sys.argv[1]) if len(sys.argv) > 1 else 4
PROMPT = "Reply with exactly the two characters: OK"
LANES = [("claude-code (anthropic)", subscription_exec, "claude-opus-4-8"),
         ("codex (openai)", codex_exec, "gpt-5.5")]


def one_call(mod, model, timeout):
    t0 = time.time()
    try:
        r = mod.run_prompt(PROMPT, model=model, timeout=timeout)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "latency": round(time.time() - t0, 1)}
    r["latency"] = round(r.get("latency") or (time.time() - t0), 1)
    return r


for label, mod, model in LANES:
    print(f"\n===== {label} =====")
    print(f"  available(): {mod.available()}")
    if not mod.available():
        print("  → CLI not on PATH for this interpreter; lane cannot run here.")
        continue

    print("  -- single call --")
    r = one_call(mod, model, timeout=90)
    if r.get("error"):
        print(f"    [FAIL] {r['error']}  ({r['latency']}s)")
    else:
        print(f"    [OK] {r['latency']}s  in={r.get('in_tok')} out={r.get('out_tok')}  "
              f"text={ (r.get('text') or '')[:40]!r}")

    print(f"  -- {CONCURRENCY}x concurrent (the panel's actual stress) --")
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        rs = list(pool.map(lambda _: one_call(mod, model, timeout=120), range(CONCURRENCY)))
    ok = [x for x in rs if not x.get("error")]
    wall = round(time.time() - t0, 1)
    print(f"    {len(ok)}/{CONCURRENCY} succeeded in {wall}s wall  "
          f"(latencies: {sorted(x['latency'] for x in rs)})")
    for x in rs:
        if x.get("error"):
            print(f"      FAIL: {x['error'][:120]}")
    if len(ok) < CONCURRENCY:
        print(f"    ⚠ {CONCURRENCY - len(ok)} failed under concurrency — a lane that flaps here diverts that "
              f"vendor to the metered API mid-panel (cooldown), which is the reliability gap.")
    else:
        print(f"    ✓ all {CONCURRENCY} survived concurrency — the plan tolerates the panel's fan-out width.")


# ── the fix that makes a lane USABLE for the panel: does a caller's schema survive the lane path? ──
# Before the fix, adapters.call dropped `schema` on the lane branch, so a structured review came back as prose
# that output_contract rejected. This drives a REAL structured call through adapters.call with the executor
# forced onto each lane, and checks the returned text parses as the requested shape — the panel's actual path.
print("\n===== schema round-trip through the lane (adapters.call, the panel's real path) =====")
import json as _json
import os as _os
from spendguard import adapters, output_contract

SCHEMA = {"type": "object", "properties": {"colors": {"type": "array", "items": {"type": "string"}}},
          "required": ["colors"], "nonempty": ["colors"]}
LANE_ENV = [("claude-code", "anthropic:claude-haiku-4-5"), ("codex", "openai:gpt-5.5")]
for executor, model in LANE_ENV:
    _prev = _os.environ.get("SPENDGUARD_ADVISOR_EXECUTOR")
    _os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = executor
    try:
        r = adapters.call(model, "Name exactly two primary colors.", schema=SCHEMA, max_tokens=2000, timeout_s=90)
    finally:
        if _prev is None:
            _os.environ.pop("SPENDGUARD_ADVISOR_EXECUTOR", None)
        else:
            _os.environ["SPENDGUARD_ADVISOR_EXECUTOR"] = _prev
    err = r.get("error")
    executor_used = r.get("executor")
    text = r.get("text") or ""
    ok_shape = False
    if not err:
        ok_shape, _sal, _why = output_contract.check_item(text, SCHEMA)
    print(f"  [{executor}] executor_used={executor_used!r} cost={r.get('cost')} "
          f"{'schema-valid' if ok_shape else 'NO'} text={text[:60]!r}"
          + (f"  err={err[:80]}" if err else ""))
    if executor_used == executor and r.get("cost") == 0.0 and ok_shape:
        print(f"    ✓ schema rode the lane, ran on the plan ($0), and validated — panel reviews will work here")
    elif err:
        print(f"    ⚠ lane errored (likely fell back / auth) — see err above")
    else:
        print(f"    ⚠ ran on {executor_used} cost={r.get('cost')} shape_ok={ok_shape} — inspect")

print("\n(reminder: $0 billed — plan/subscription, kind='subscription')")
