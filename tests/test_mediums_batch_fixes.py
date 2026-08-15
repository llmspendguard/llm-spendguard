"""Verified-MEDIUM fixes (batches A/B/C from the 4-LLM review triage), pinned:

  A spend/trust: guard.record_saving warns (not silent) on a failed write; ledger_sync._warn_fetch surfaces a
    failed provider pull (not a silent $0); reconcile.completeness does NOT call a $0-truth source with a residual
    "reconciled"; tag.estimate_llm_retag keeps a small batch's estimate honestly nonzero.
  B lane/exec: subscription lane strips ANTHROPIC_API_KEY + AUTH_TOKEN + BASE_URL from the child; codex lane puts
    `--` before the prompt so a '-'-prefixed prompt is a positional, not a flag.
  C crash-guards: lambda_adapter._dph / sync._rate_or safe on non-numeric; resources.instances returns [] on an
    error payload (not the payload itself).

Offline, isolated home.
"""
import os
import sys
import tempfile
import types

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-medbatch-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import guard, ledger_sync, reconcile, tag, resources, config   # noqa: E402
from spendguard import lambda_adapter as la, sync                              # noqa: E402
from spendguard import subscription_exec as se, codex_exec as ce               # noqa: E402

fails = 0
warns = []


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


config.warn_once = lambda msg: warns.append(msg)

# ── A: spend/trust ───────────────────────────────────────────────────────────────────────────────────────────
def _boom():
    raise RuntimeError("db down")


guard._db = _boom
warns.clear()
guard.record_saving("cache", 5.0)                       # must NOT raise
ck("record_saving warns (not silently swallows) on a failed write", any("record_saving" in w for w in warns))

warns.clear()
ledger_sync._warn_fetch("openai", RuntimeError("net"))
ck("ledger_sync surfaces a FAILED provider fetch (not a silent $0)", any("openai" in w and "FAILED" in w for w in warns))

r0 = reconcile.completeness({"s": {"truth_total": 0.0, "residual": 10.0}})
ck("a $0-truth source with a $10 residual is NOT 'reconciled' (under)",
   r0["sources"]["s"]["status"] == "under" and r0["complete"] is False)
rz = reconcile.completeness({"s": {"truth_total": 0.0, "residual": 0.0}})
ck("a $0-truth source with a $0 residual IS reconciled", rz["sources"]["s"]["status"] == "reconciled")
rn = reconcile.completeness({"s": {"truth_total": 1000.0, "residual": 5.0}})
ck("a nonzero-truth source with a small residual (< threshold) is reconciled", rn["sources"]["s"]["status"] == "reconciled")

tag.ambiguous_count = lambda: 1
ck("a small batch's est_usd is honestly nonzero (not $0.00)", tag.estimate_llm_retag()["est_usd"] > 0)

# ── B: lane/executor ─────────────────────────────────────────────────────────────────────────────────────────
os.environ["ANTHROPIC_API_KEY"] = "k"
os.environ["ANTHROPIC_AUTH_TOKEN"] = "t"
os.environ["ANTHROPIC_BASE_URL"] = "http://x"
cap = {}
se._bin = lambda: "claude"
se.subprocess.run = lambda cmd, **kw: (cap.__setitem__("env", kw.get("env", {})),
                                       types.SimpleNamespace(returncode=0, stdout="{}", stderr=""))[1]
se.run_prompt("hello")
ck("subscription lane strips API_KEY + AUTH_TOKEN + BASE_URL from the child env",
   all(k not in cap.get("env", {}) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")),
   str([k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL") if k in cap.get("env", {})]))

cap2 = {}
ce._bin = lambda: "codex"
ce.subprocess.run = lambda cmd, **kw: (cap2.__setitem__("cmd", list(cmd)),
                                       types.SimpleNamespace(returncode=1, stdout="", stderr=""))[1]
ce.run_prompt("-weird prompt")
_cmd = cap2.get("cmd", [])
ck("codex cmd puts `--` immediately before the prompt (a '-'-prefixed prompt is a positional)",
   "--" in _cmd and _cmd[-2:] == ["--", "-weird prompt"], str(_cmd[-4:]))

# ── C: crash-guards ──────────────────────────────────────────────────────────────────────────────────────────
ck("_dph is safe on non-numeric / None / empty, correct on a number",
   la._dph("abc") is None and la._dph(None) is None and la._dph("") is None and abs(la._dph(150) - 1.5) < 1e-9)
ck("_rate_or is safe on non-numeric / None, correct on a number",
   sync._rate_or("abc", 5.0) == 5.0 and sync._rate_or(None, 5.0) == 5.0 and abs(sync._rate_or(1e-6, 5.0) - 1.0) < 1e-9)

resources._get = lambda path: {"error": "boom"}
ck("resources.instances() returns [] on an error payload (never iterates the payload as instances)",
   resources.instances() == [])
resources._get = lambda path: {"instances": [{"id": 1}]}
ck("resources.instances() returns the list when present", resources.instances() == [{"id": 1}])

print(f"\n{'[FAIL]' if fails else 'OK'} test_mediums_batch_fixes: {fails} failure(s)")
sys.exit(1 if fails else 0)
