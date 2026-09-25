"""Guard — the conformance PANEL REVIEW sizes its batch wall-clock deadline from MEASURED per-model latency, never a
hardcode. The panel once hardcoded deadline_s=180.0; a reasoning reviewer on the whole-suite payload lands in the slow
tail (measured kimi-k3 p95 ≈ 265s), so 180s tore it down MID-THOUGHT — which STILL BILLS (reasoning tokens, no output)
while the local ledger reads $0. Both kimi-k3 and glm-5.3 failed that way on a real run. The fix routes the deadline
through adapters.deadline_for (the ONE measured-latency home) and takes the MAX across the panel, because bulk_delegate
bounds the whole batch with a single deadline. This guard fails if the hardcode ever creeps back or the sizing stops
being measured. Offline: adapters.deadline_for is stubbed; build_payload only reads repo files; no network, no call."""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="spendguard-paneldl-"))
os.environ.pop("SPENDGUARD_CONFORMANCE_DEADLINE_S", None)   # a stray env override would mask the measured path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts", "integration", "conformance"))

import panel_review as PR  # noqa: E402
from spendguard import adapters  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


# ── the sizing is MEASURED, and the batch takes the SLOWEST reviewer ──
print("-- measured deadline (stubbed advisor) --")
_calls = []
def _fake_deadline_for(model, intent=None, in_chars=None, default_s=None):
    _calls.append((model, intent, in_chars))
    table = {"anthropic:claude-opus-4-8": (1200.0, "measured:model(n=228)"),
             "moonshot:kimi-k3": (1800.0, "measured:model(n=176)"),
             "zai:glm-5.3": (300.0, "measured:model(n=15)")}
    return table.get(model, (None, "unknown"))

_real = adapters.deadline_for
adapters.deadline_for = _fake_deadline_for
try:
    payload = PR.build_payload()
    ck("payload is non-empty (evidence read whole)", len(payload) > 1000)

    secs, basis = PR.resolve_panel_deadline(
        ["anthropic:claude-opus-4-8", "moonshot:kimi-k3", "zai:glm-5.3"], payload)
    ck("deadline is the MAX across the panel (slowest reviewer bounds the batch)", secs == 1800.0)
    ck("basis names the measured source + winning model", "measured" in basis and "kimi-k3" in basis)
    ck("the advisor was actually consulted per model (sizing is not invented here)",
       {c[0] for c in _calls} == {"anthropic:claude-opus-4-8", "moonshot:kimi-k3", "zai:glm-5.3"})
    ck("the advisor is asked with the real payload size (in_chars), not a guess",
       all(c[2] == len(payload) for c in _calls))

    # a model the advisor can't measure (unknown) must fall back to the NAMED default, never a silent 0/None
    _calls.clear()
    secs_unknown, basis_unknown = PR.resolve_panel_deadline(["no:such-model"], payload)
    ck("unmeasured model → the NAMED default fallback (not 0, not None)",
       secs_unknown == PR._PANEL_DEADLINE_DEFAULT_S and secs_unknown > 0)

    # explicit arg wins over the advisor
    secs_x, basis_x = PR.resolve_panel_deadline(["moonshot:kimi-k3"], payload, explicit=42.0)
    ck("explicit deadline_s overrides the advisor", secs_x == 42.0 and basis_x == "caller")

    # env override wins over the advisor (documented precedence, matches panel_models())
    os.environ["SPENDGUARD_CONFORMANCE_DEADLINE_S"] = "77"
    secs_e, basis_e = PR.resolve_panel_deadline(["moonshot:kimi-k3"], payload)
    ck("$SPENDGUARD_CONFORMANCE_DEADLINE_S overrides the advisor", secs_e == 77.0 and basis_e == "env")
    os.environ.pop("SPENDGUARD_CONFORMANCE_DEADLINE_S", None)
finally:
    adapters.deadline_for = _real

# ── anti-amnesia: the exact hardcode that caused the incident cannot return ──
print("-- no hardcoded deadline (the regression) --")
_src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "integration", "conformance",
                         "panel_review.py")).read()
ck("run_panel no longer hardcodes deadline_s=180", "deadline_s=180" not in _src.replace(" ", ""))
ck("the batch deadline is passed through the resolved variable, not a literal",
   "deadline_s=deadline_s" in _src.replace(" ", ""))
ck("the sizing goes through the single measured home (adapters.deadline_for)", "adapters.deadline_for" in _src)

print(f"\n{'[FAIL]' if _fails else 'OK'} test_panel_deadline_measured: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
