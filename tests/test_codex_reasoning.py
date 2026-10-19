"""Codex lane honours a selectable reasoning effort — mapped to the Codex plan model's OWN scale
(none|low|medium|high|xhigh|max), which has NO 'minimal' (MEASURED: 'minimal' is a hard 400 on that model). So the
standard ordinal 'minimal' → 'none'; the rest pass through. run_prompt forwards reasoning to warm admission
and reports the applied effort. Offline: warm admission is stubbed, so no codex process runs.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-cdxr-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec, codex_daemon                                         # noqa: E402


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


fails = []
ce = codex_exec._codex_effort

print("-- the ordinal maps to Codex's OWN scale (no 'minimal'; floor is 'none') --")
fails += ck("minimal → none (Codex model rejects 'minimal' with a 400)", ce("minimal") == "none")
fails += ck("low → low", ce("low") == "low")
fails += ck("high → high", ce("high") == "high")
fails += ck("xhigh passes through (Codex-specific tier)", ce("xhigh") == "xhigh")
fails += ck("empty / None → None (leave codex's own default)", ce("") is None and ce(None) is None)


seen = {}


def _fake_run(prompt, **kw):
    seen.update(kw)
    return {"text": prompt, "error": None}


print("\n-- run_prompt forwards reasoning through warm admission and reports the applied effort --")
_obin, _orun = codex_exec._daemon_enabled, codex_daemon.run_warm
try:
    codex_exec._daemon_enabled = lambda: True
    codex_daemon.run_warm = _fake_run
    result = codex_exec.run_prompt("hi", reasoning="minimal", model="openai:gpt-5.5")
    fails += ck("reasoning='minimal' is forwarded and reports applied effort none",
                seen.get("reasoning") == "minimal" and result.get("effort") == "none")
    seen.clear()
    result = codex_exec.run_prompt("hi", reasoning="high")
    fails += ck("reasoning='high' is forwarded and reports high",
                seen.get("reasoning") == "high" and result.get("effort") == "high")
    seen.clear()
    result = codex_exec.run_prompt("hi")
    fails += ck("no reasoning leaves the warm model default untouched",
                seen.get("reasoning") is None and result.get("effort") is None)
finally:
    codex_exec._daemon_enabled, codex_daemon.run_warm = _obin, _orun

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_reasoning: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
