"""A lane reply is only a "$0 plan success" if it has real CONTENT and (when a shape was asked for) SATISFIES it.
An EMPTY, WHITESPACE-only, or OFF-SHAPE reply must FAIL OVER to the metered API — never be recorded as a success and
handed back as a blank/garbage chunk. (This is the gap Ash hit: "the chunks that worked were the ones on the API.")
Offline: a fake lane whose reply we control + a stubbed API failover; no real subprocess, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanefb-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, lane_balance, calls, output_contract                  # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
_X = {"text": ""}          # the fake lane's reply, set per case


class _FakeLane:
    TIMEOUT_S = 300

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, **kw):
        return {"text": _X["text"], "error": None, "in_tok": 1, "out_tok": 1, "latency": 0.0}


def _run(text, schema=None):
    _X["text"] = text
    with calls.context(intent="chunk-extract"):
        return adapters._call_once("openai:gpt-5-mini", "hi", max_tokens=100, schema=schema)


_orig = (adapters._lane_for, adapters.call, lane_balance.route_decision,
         adapters._input_fits, adapters.json_schema_request, output_contract.check_item)
try:
    adapters._lane_for = lambda prov: ("codex", _FakeLane) if prov == "openai" else None
    # the API failover path: the reactive substitute (route_decision reactive) routed through call() — a distinct,
    # unmistakable answer so we can tell "fell back" from "kept the lane's reply"
    adapters.call = lambda model, prompt, **kw: {"text": "FALLBACK", "error": None, "in_tok": 1, "out_tok": 1,
                                                 "latency": 0.0, "cost": 0.0, "model": model}
    # _lane_cool no longer stubbed: cooldowns now write to the per-test-isolated resource_state store, so real
    # cooling is a harmless side effect here (this test asserts on the reply text, not on cooldowns).
    adapters._input_fits = lambda *a, **k: (True, "stub")
    lane_balance.route_decision = lambda intent, model, reactive=False: (
        ("openai:gpt-5.5-sub", "primary failed") if reactive else (None, ""))
    adapters.json_schema_request = lambda kind, schema, **k: {"_schema_prompt": "emit JSON"}

    print("-- EMPTY / WHITESPACE lane replies fail over to the API (not a $0 'success') --")
    fails += ck("empty text → fails over", _run("").get("text") == "FALLBACK")
    fails += ck("whitespace-only text → fails over", _run("   \n  ").get("text") == "FALLBACK")

    print("\n-- a REAL answer (no schema) stays on the lane ($0) — no needless failover --")
    r = _run("the real answer")
    fails += ck("valid text → lane KEPT (executor=codex, $0)", r.get("text") == "the real answer" and r.get("executor") == "codex")

    print("\n-- with a schema: OFF-shape output fails over; ON-shape stays on the lane --")
    output_contract.check_item = lambda item, contract: (False, False, "off shape")
    fails += ck("non-empty but OFF-shape reply → fails over", _run("garbage", schema={"type": "object"}).get("text") == "FALLBACK")
    output_contract.check_item = lambda item, contract: (True, False, "")
    r2 = _run('{"ok":1}', schema={"type": "object"})
    fails += ck("non-empty and ON-shape reply → lane KEPT", r2.get("text") == '{"ok":1}' and r2.get("executor") == "codex")
finally:
    (adapters._lane_for, adapters.call, lane_balance.route_decision,
     adapters._input_fits, adapters.json_schema_request, output_contract.check_item) = _orig

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_empty_fallback: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
