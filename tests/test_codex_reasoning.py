"""Codex lane honours a selectable reasoning effort — mapped to the Codex plan model's OWN scale
(none|low|medium|high|xhigh|max), which has NO 'minimal' (MEASURED: 'minimal' is a hard 400 on that model). So the
standard ordinal 'minimal' → 'none'; the rest pass through. run_prompt threads it as `-c model_reasoning_effort=<v>`.
Offline: subprocess + the codex binary are stubbed, so no real (slow) codex call runs.
"""
import os
import sys
import tempfile

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-cdxr-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import codex_exec                                                       # noqa: E402


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


class _R:
    returncode = 0
    stdout = "{}"
    stderr = ""


seen = {}


def _fake_run(cmd, **kw):
    seen["cmd"] = list(cmd)
    try:                                                # write the out-file so run_prompt reads a body, not an error
        i = cmd.index("--output-last-message")
        open(cmd[i + 1], "w").write("ok")
    except Exception:
        pass
    return _R()


print("\n-- run_prompt threads the effort as `-c model_reasoning_effort=<v>`; none set → no flag --")
_obin, _orun = codex_exec._bin, codex_exec.subprocess.run
try:
    codex_exec._bin = lambda: "/fake/codex"
    codex_exec.subprocess.run = _fake_run
    codex_exec.run_prompt("hi", reasoning="minimal", model="openai:gpt-5.5")
    cmd = seen.get("cmd", [])
    fails += ck("reasoning='minimal' → cmd carries `-c model_reasoning_effort=none`",
                "-c" in cmd and "model_reasoning_effort=none" in cmd)
    seen.clear()
    codex_exec.run_prompt("hi", reasoning="high")
    fails += ck("reasoning='high' → `model_reasoning_effort=high`", "model_reasoning_effort=high" in seen.get("cmd", []))
    seen.clear()
    codex_exec.run_prompt("hi")
    fails += ck("no reasoning → NO model_reasoning_effort flag (codex default untouched)",
                not any("model_reasoning_effort" in str(x) for x in seen.get("cmd", [])))
finally:
    codex_exec._bin, codex_exec.subprocess.run = _obin, _orun

print(f"\n{'[FAIL]' if fails else 'OK'} test_codex_reasoning: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
