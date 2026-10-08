"""claudecode.residency_decomposition — the context-RESIDENCY source split (Review 1, ask #1). Offline: the
transcript walk is fed synthetic records, the ledger (_cc_conv_rows) + cache-read rate are stubbed. Zero spend.

Pins: a resident item costs tokens × turns-it-stayed × the cache-read rate; accumulated tool_output is attributed
PER TOOL (via tool_use_id→name) and dominates; the measured recurring cost is SPLIT by resident token-turn weight so
the decomposition RECONCILES with the measured total; and the instruction count is EXPLICIT + provenance-recorded,
never sourced ambiently from the host."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-resid-")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import claudecode as cc  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


def _asst(blocks):
    return {"type": "assistant", "sessionId": "C1",
            "message": {"role": "assistant", "id": "m%d" % id(blocks), "model": "claude-opus-4-8",
                        "usage": {"input_tokens": 1, "output_tokens": 1,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
                        "content": blocks}}


def _user(blocks):
    return {"type": "user", "sessionId": "C1", "message": {"role": "user", "content": blocks}}


_RECS = [
    _asst([{"type": "tool_use", "id": "tR", "name": "Read"}]),                  # turn 0 → 1
    _user([{"type": "tool_result", "tool_use_id": "tR", "content": "X" * 4000}]),   # big Read output, entry turn 1
    _asst([{"type": "tool_use", "id": "tB", "name": "Bash"}, {"type": "text", "text": "ok"}]),  # turn 1 → 2
    _user([{"type": "tool_result", "tool_use_id": "tB", "content": "Y" * 400}]),    # small Bash output, entry turn 2
    _user([{"type": "text", "text": "please do the thing"}]),                   # a real user prompt
    _user([{"type": "text", "text": "<system-reminder>ambient note</system-reminder>"}]),  # injected reminder
    _asst([{"type": "text", "text": "done"}]),                                 # turn 2 → 3
]
cc._conv_transcript_records = lambda conv_id: list(_RECS)
cc._cc_conv_rows = lambda min_day=None: {"C1": [("t1", "claude-opus-4-8", 100, 1000, 0),
                                                ("t2", "claude-opus-4-8", 100, 2000, 0),
                                                ("t3", "claude-opus-4-8", 100, 3000, 0)]}
cc._cache_read_rate = lambda model: 1e-6     # $1 / 1M cached tokens → measured recurring = (1000+2000+3000)*1e-6

d = cc.residency_decomposition("C1")          # no instruction paths → instructions unmeasured (honest 0)
ck("3 billed turns counted", d["turns"] == 3)
ck("measured recurring $ = Σ cache_read × rate", abs(d["measured_recurring_usd"] - 0.006) < 1e-9)
ck("tool_output is the dominant source", max(d["by_source_usd"], key=lambda k: d["by_source_usd"][k] or 0) == "tool_output")
ck("tool output attributed PER TOOL (Read and Bash both present)", set(d["by_tool_usd"]) == {"Read", "Bash"})
ck("the big, earlier Read output outweighs the small, later Bash output", d["by_tool_usd"]["Read"] > d["by_tool_usd"]["Bash"])
ck("an injected system_reminder is a distinct source", d["by_source_usd"]["system_reminder"] is not None and d["by_source_usd"]["system_reminder"] > 0)
ck("the split RECONCILES with the measured total (share is the finding, total is the size)",
   d["reconciles"] and abs(sum(v for v in d["by_source_usd"].values() if v) - d["measured_recurring_usd"]) < 1e-3)
ck("instructions are UNMEASURED (0) when no paths given — never ambiently sourced", d["instruction_tokens"] == 0 and d["instruction_source"] == [])

# declared instruction tokens → recorded as provenance, resident every turn
d2 = cc.residency_decomposition("C1", instruction_tokens=500)
ck("declared instruction tokens recorded with 'declared' provenance", d2["instruction_tokens"] == 500 and d2["instruction_source"] == "declared")
ck("instructions still reconcile into the split", d2["reconciles"])

# explicit instruction PATHS → measured from those files, provenance lists (path, tokens) — never the host cwd/home
_f = os.path.join(os.environ["SPENDGUARD_HOME"], "CLAUDE.md")
with open(_f, "w") as fh:
    fh.write("x " * 50)
d3 = cc.residency_decomposition("C1", instruction_paths=[_f])
ck("explicit instruction path is measured and recorded as provenance",
   d3["instruction_tokens"] > 0 and isinstance(d3["instruction_source"], list) and d3["instruction_source"][0][0] == _f)

print(f"\n{'[FAIL]' if fails else 'OK'} test_residency_decomposition: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
