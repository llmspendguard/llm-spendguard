"""Design item (P10, caller-intent): a COMPLETED stream's ACTUAL usage was discarded. _rt_account saw kw['stream']=True
and recorded the ESTIMATE first, throwing away the final message's real token counts that the stream proxy
(_GatedStreamManager.record) hands it. Now it records the provider's ACTUAL usage on the BILLED basis when act_fn yields
it (including a finished stream), and falls back to the projection ONLY when a stream reported no usable usage.

Offline ($0): peripheral extractors are stubbed so the test isolates the actual-vs-estimate DECISION (the fix).
Isolation: SPENDGUARD_HOME → mkdtemp before import.
"""
import os, sys, tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-stream-cost-")

from spendguard import gate   # noqa: E402


def main():
    fails = 0

    def ck(name, cond, extra=""):
        nonlocal fails
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra and not cond else ''}")

    seen = []
    _saved = {n: getattr(gate, n) for n in ("_record_rt", "_cached_in", "_output_text", "_finish", "_cache_creation",
                                            "_record_tool_fees", "_embed_per_item_max", "_stream_out_estimate")}
    gate._cached_in = lambda r: 0
    gate._output_text = lambda r: ""
    gate._finish = lambda r: None
    gate._cache_creation = lambda r: 0
    gate._record_tool_fees = lambda *a, **k: None
    gate._embed_per_item_max = lambda kw: None
    gate._stream_out_estimate = lambda model, kw, est_fn: (0, 42)     # the projection used only when there's no usage
    gate._record_rt = lambda model, kw, in_tok, out_tok, cached, latency, *a, **k: seen.append(
        {"in": in_tok, "out": out_tok, "basis": k.get("basis")})
    try:
        kw = {"stream": True, "model": "claude-opus-4-8"}

        # 1. a COMPLETED stream: act_fn yields the provider's REAL usage → recorded ACTUAL + BILLED, not the estimate
        seen.clear()
        gate._rt_account("claude-opus-4-8", kw, result={"final": "msg"},
                         est_fn=lambda _kw: (0, 999, 999), act_fn=lambda _r: (123, 456), latency=1.0)
        ck("a finished stream records the ACTUAL usage (123/456), not the estimate (999)",
           seen and seen[-1]["in"] == 123 and seen[-1]["out"] == 456, seen)
        ck("a finished stream is recorded on the BILLED basis (provider truth)",
           seen and seen[-1]["basis"] == gate.budget_basis_billed(), seen)

        # 2. a stream with NO usable usage (act_fn yields None) → the projection, on the ESTIMATE basis
        seen.clear()
        gate._rt_account("claude-opus-4-8", kw, result=None,
                         est_fn=lambda _kw: (0, 42, 7), act_fn=lambda _r: None, latency=1.0)
        ck("a stream with no usage falls back to the projection on the ESTIMATE basis",
           seen and seen[-1]["basis"] == gate.budget_basis_estimate(), seen)
    finally:
        for n, v in _saved.items():
            setattr(gate, n, v)

    print(f"\n{'[FAIL]' if fails else '[OK]'} test_stream_cost_actual_usage: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
