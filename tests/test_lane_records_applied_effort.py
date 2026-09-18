"""A lane records the effort it ACTUALLY APPLIED, not the requested tier.

The codex CLI maps a requested 'minimal' to its own floor 'none' (the OpenAI API scale differs from the Codex
scale). Recording the REQUEST ('minimal') would mislabel what RAN and make a lane call look like it diverged from
its metered twin — when it did not: for gpt-5.6-sol@minimal BOTH channels apply reasoning_effort='none'
(models.normalize_reasoning and codex_exec._codex_effort agree). This guards adapters._call_once's lane success
path: the executor reports the effort it applied (`s['effort']`), and that APPLIED value is what record_call books
and what the result row carries — so a consistency-sensitive caller can VERIFY the lane matched its metered fallback.

Offline: the lane executor and calls.record_call are stubbed — no CLI, no network, no LLM.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-eff-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, calls, codex_exec, models   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []

print("-- both channels apply the SAME effort for gpt-5.6-sol@minimal → the lane never under-reasoned vs metered --")
# gpt-5.6-sol rejects 'minimal' on the metered API (400) → its VERIFIED floor is 'none' (a per-model fact, present in
# the real home). Seed it here so the isolated home reflects that reality; without it gpt-5.6-sol falls to the gpt-5
# FAMILY default ('minimal') and the metered send would heal minimal→none on the 400 instead of pre-normalizing.
models.add_fact("gpt-5.6-sol", "reasoning", "none", source="test")
fails += report_check("codex lane _codex_effort('minimal') == 'none' (its floor)", codex_exec._codex_effort("minimal") == "none")
fails += report_check("codex lane _codex_effort('low') == 'low' (pass-through)", codex_exec._codex_effort("low") == "low")
fails += report_check("metered normalize_reasoning('gpt-5.6-sol','minimal') == 'none' (SAME as the lane, via the verified fact)",
                      models.normalize_reasoning("gpt-5.6-sol", "minimal") == "none")


class _LaneExec:
    """A fake subscription-lane executor that REPORTS the effort it applied (as codex_exec now does): a requested
    'minimal' ran as 'none'."""
    TIMEOUT_S = 30
    MIN_TIMEOUT_S = 1

    @staticmethod
    def run_prompt(prompt, system=None, model=None, timeout=None, reasoning=None, max_tokens=None):
        # TEST DOUBLE (not production): a fake subscription-lane executor standing in for codex/claude/agy. It
        # fabricates no real inference — it returns a FIXED envelope so the test can assert _call_once books the
        # APPLIED effort ('none') the executor reports. max_tokens is accepted to match the real run_prompt protocol.
        return {"text": "LANE-ANSWER", "in_tok": 1, "out_tok": 1, "latency": 0.1, "effort": "none"}


adapters._lane_for = lambda prov: ("codexfake", _LaneExec)     # every provider has a live fake lane
adapters._lane_too_big = lambda lane, prompt: False
adapters._lane_model_cooling = lambda lane, raw: False


class _RecordCapture:
    """Captures calls.record_call into INSTANCE state (self.rows), so the test can assert what was booked without a
    module-level container."""

    def __init__(self):
        self.rows = []

    def __call__(self, prov, model, kind, cost, **kw):
        self.rows.append({"kind": kind, "effort": kw.get("effort"), "executor": kw.get("executor")})


_cap = _RecordCapture()
calls.record_call = _cap

print("\n-- adapters._call_once lane path books + returns the APPLIED effort ('none'), NOT the requested ('minimal') --")
r = adapters._call_once("openai:gpt-5.6-sol", "refute X", max_tokens=64, reasoning="minimal", _skip_lane=False)
fails += report_check("the lane served it (executor=codexfake, $0)", r.get("executor") == "codexfake" and r.get("cost") == 0.0)
fails += report_check("the RESULT ROW carries the APPLIED effort 'none' (not the requested 'minimal')", r.get("effort") == "none")
fails += report_check("record_call BOOKED effort='none' (applied), executor=codexfake, kind=subscription",
                      any(row["effort"] == "none" and row["executor"] == "codexfake" and row["kind"] == "subscription"
                          for row in _cap.rows))

print(f"\n{'[FAIL]' if fails else 'OK'} test_lane_records_applied_effort: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
