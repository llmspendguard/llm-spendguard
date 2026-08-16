"""Scoped agentic re-adjudication of the lane-protocol names that gained a new implementation.

When a subscription lane is ADDED (here: the gemini/antigravity lane), the bare method names every lane exposes
— `_bin`, `available`, `run_prompt` — gain a definition, and tests/test_names_stay_unique.py fails because the
frozen verdict in docs/NAME_REGISTRY.json was reached by reading the ORIGINAL bodies. The doctrine is explicit:
that verdict is a JUDGEMENT about meaning (protocol vs duplication vs collision) and must be ADJUDICATED by a
model, never asserted. This does exactly that, scoped to the names that actually changed rather than re-paying to
re-sort all 87 groups.

It reads the WHOLE lane modules + the polymorphic dispatcher (lanes.py) via spendguard.llm_files — complete
bodies, never excerpts — so the model judges from the real contract, not a starved window. Gated; prefers the $0
subscription lane (executor=pool) with the metered API as fallback. Prints the verdicts as JSON for recording in
docs/NAME_REGISTRY.json.

  SPENDGUARD_ADVISOR_EXECUTOR=pool .venv/bin/python scripts/probe/adjudicate_lane_protocol_names.py
"""
import json
import os

import spendguard
spendguard.require()                      # fail closed: refuse to run ungated (this makes paid calls)

from spendguard import adapters, llm_files

MODEL = os.environ.get("SPENDGUARD_ADJUDICATE_MODEL", "claude-opus-4-8")   # config, not a literal buried in code

_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(_HERE, "..", "..", "src", "spendguard"))
# Complete evidence for the judgement: every module that DEFINES one of the three names, plus lanes.py — the
# caller that dispatches `mod._bin()` / `mod.run_prompt(...)` polymorphically across the lane modules.
CONTEXT = [os.path.join(SRC, m) for m in
           ("subscription_exec.py", "codex_exec.py", "zai_exec.py", "antigravity_exec.py", "lanes.py")]
NAMES = ["_bin", "available", "run_prompt"]

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["PROTOCOL", "DUPLICATION", "COLLISION"]},
                    "why": {"type": "string", "nonempty": True},
                },
                "required": ["name", "verdict", "why"],
            },
        },
    },
    "required": ["verdicts"],
}

SYSTEM = (
    "You are adjudicating shared FUNCTION NAMES in a Python package, using these definitions verbatim:\n"
    "  • PROTOCOL   — one uniform contract dispatched by name across interchangeable implementations "
    "(e.g. every lane module exposes run_prompt with the same signature and return shape, and a caller "
    "invokes mod.run_prompt(...) polymorphically). This is CORRECT and must be preserved.\n"
    "  • DUPLICATION — two copies of ONE job that have drifted or should be merged into a single definition.\n"
    "  • COLLISION  — different jobs that merely happen to share a name; a grep/import/re-export can silently "
    "pick the wrong one. This is a BUG and the fix is to RENAME.\n"
    "Judge from the ACTUAL bodies and how the name is dispatched — not from the name alone. Answer only via the "
    "required JSON shape, with a concise, specific `why` grounded in the code you were given."
)

_prompt_body = (
    f"Sort each of these bare names into protocol / duplication / collision: {', '.join(NAMES)}.\n\n"
    "Context — every module that defines one of these names, plus lanes.py (the dispatcher). Each file is stamped "
    "COMPLETE:\n\n"
)
block, manifests = llm_files.attach_many(CONTEXT)
prompt = _prompt_body + block

if __name__ == "__main__":
    print(f"model={MODEL}  executor={adapters._executor()}  files={[m['path'].split('/')[-1] for m in manifests]}")
    r = adapters.call(MODEL, prompt, system=SYSTEM, schema=VERDICT_SCHEMA, sig="lane-protocol-name-adjudication")
    if r.get("error"):
        print(f"ADJUDICATION FAILED: {r['error']}")
        raise SystemExit(1)
    print(f"executor={r.get('executor') or 'api'}  in_tok={r.get('in_tok')}  out_tok={r.get('out_tok')}  "
          f"cost=${(r.get('cost') or 0):.4f}  latency={(r.get('latency') or 0):.1f}s")
    try:
        verdicts = json.loads(r["text"])["verdicts"]
    except Exception as e:
        print(f"could not parse verdicts as JSON ({type(e).__name__}): {(r.get('text') or '')[:400]}")
        raise SystemExit(1)
    print("\n=== VERDICTS ===")
    print(json.dumps(verdicts, indent=2))
