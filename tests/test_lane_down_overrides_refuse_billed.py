"""A lane that is DOWN (its executor returned an error — login/token expired, CLI crash, rejected model) still fails
over THROUGH the ladder even under the $0-only contract (no_metered_fallback). Infrastructure failure is not a task
being too hard, so work is never silently lost — the empty-and-skipped a logged-out codex lane produced (Ash
2026-09-26: "a down lane still fails over"). A TASK miss (empty / off-shape) under the SAME contract still stays a $0
miss (no surprise metered charge). The split is STRUCTURAL (which branch set _lane_reason), never a parse of the error
prose — that (auth vs other) is the agentic remediation's job.

Offline, isolated home: a fake lane executor returns an error (down) or empty (task miss); the metered API fails fast
with no key, so we assert WHICH path was taken (refused-$0 short-circuit vs ladder-attempted), never a live success.
"""
import os
import sys
import tempfile
import types

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-lanedown-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, resource_state   # noqa: E402


def ck(results, label, cond, extra=""):
    """Record + print one check. `results` is passed IN (the accumulator is the caller's, not module-global state)."""
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _run(lane_name, exec_module, no_metered_fallback):
    """Drive _call_once with a fake lane and NO metered key (so the metered leg fails fast — we read the PATH taken,
    not a live success). _no_sub=True pins the vendor (the describe/comprehend confinement case) so the ladder goes
    straight to the metered twin rather than a substitute lane."""
    _real_lane_for, _real_key = adapters._lane_for, adapters.config.api_key
    adapters._lane_for = lambda prov: (lane_name, exec_module) if prov == "anthropic" else None
    adapters.config.api_key = lambda name: None
    try:
        return adapters._call_once("anthropic:claude-opus-4-8", "describe this", max_tokens=500, timeout_s=5,
                                   no_metered_fallback=no_metered_fallback, _no_sub=True)
    finally:
        adapters._lane_for, adapters.config.api_key = _real_lane_for, _real_key


def main():
    results = []

    # ── a DOWN lane (executor error) under no_metered_fallback → applies the ladder, does NOT short-circuit to $0 ──
    resource_state._reset()
    adapters._lane_announce._at.clear()
    down_exec = types.SimpleNamespace(
        TIMEOUT_S=60,
        run_prompt=lambda prompt, system=None, model=None, timeout=None, **_kw: {
            "error": "access token could not be refreshed — please log out", "text": None})
    rA = _run("codex-down", down_exec, no_metered_fallback=True)
    ck(results, "a DOWN lane under no_metered_fallback does NOT return the $0 'refused' miss (it applies the ladder)",
       not str(rA.get("error") or "").startswith("refused"), extra=str(rA.get("error"))[:80])
    ck(results, "...the outage is SURFACED once (never a silent empty)",
       "codex-down" in adapters._lane_announce._at)
    ck(results, "...and the metered leg failed too (no key) so the lane was cooled (down)",
       adapters._lane_cooling("codex-down"))

    # ── a TASK miss (empty text, executor set NO error) under no_metered_fallback → stays a $0 'refused' miss ──
    resource_state._reset()
    adapters._lane_announce._at.clear()
    empty_exec = types.SimpleNamespace(
        TIMEOUT_S=60,
        run_prompt=lambda prompt, system=None, model=None, timeout=None, **_kw: {"text": "", "error": None})
    rB = _run("lane-empty", empty_exec, no_metered_fallback=True)
    ck(results, "a TASK miss (empty) under no_metered_fallback stays a $0 'refused' miss (no surprise charge)",
       str(rB.get("error") or "").startswith("refused"), extra=str(rB.get("error"))[:80])
    ck(results, "...a task miss is NOT surfaced as a lane-down (it is not infrastructure failure)",
       "lane-empty" not in adapters._lane_announce._at)

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_lane_down_overrides_refuse_billed: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
