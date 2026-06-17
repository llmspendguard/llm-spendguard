"""Fail-closed / fail-open behavior of the gate — NO network, NO spend. Stubs the SDK create methods before
install() so the gate wraps the stub. Complements test_gate.py (batch caps) by covering:
  * require() — the FAIL-CLOSED primitive: refuses when the gate is disabled / not enforcing.
  * the REAL-TIME precheck — refuses over GATE_RT_BUDGET, honors GATE_ALLOW, and GATE_DISABLE passes through.
"""
import os
import sys
import tempfile

# Isolate SPENDGUARD_HOME before the venv sitecustomize loads the gate (re-exec once; the runner sets the flag).
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)


class _Usage:
    prompt_tokens = 12
    completion_tokens = 8


class _Resp:                       # minimal stand-in for a chat completion (no network)
    usage = _Usage()
    choices = []


# stub the real-time create BEFORE install so the gate wraps the stub
from openai.resources.chat import completions as oc_chat            # noqa: E402
oc_chat.Completions.create = lambda self, *a, **k: _Resp()

import spendguard                                                    # noqa: E402
from spendguard import gate as spend_gate                           # noqa: E402

failures = 0


def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def refuses(fn):
    try:
        fn()
        return False
    except spend_gate.SpendGateRefused:
        return True


# ── require(): the fail-closed guard ───────────────────────────────────────────────────────────────────────
os.environ["GATE_DISABLE"] = "1"
check("require() RAISES when GATE_DISABLE=1 (won't spend ungated)", refuses(spendguard.require))
os.environ.pop("GATE_DISABLE", None)

check("require() returns when the gate IS enforcing (SDK importable + patched)", not refuses(spendguard.require))

# ── real-time precheck ─────────────────────────────────────────────────────────────────────────────────────
spend_gate.install()
check("chat.completions.create is gated after install()", getattr(oc_chat.Completions.create, "_spend_gated", False))

from openai import OpenAI                                            # noqa: E402
oc = OpenAI(api_key="sk-test")
msgs = [{"role": "user", "content": "summarize this support ticket in two sentences " * 5}]


def call():
    return oc.chat.completions.create(model="gpt-5.5", messages=msgs, max_tokens=200)


os.environ["GATE_RT_BUDGET"] = "0.0001"
os.environ.pop("GATE_ALLOW", None)
check("real-time call over tiny GATE_RT_BUDGET, non-interactive -> REFUSE", refuses(call))

os.environ["GATE_ALLOW"] = "1"
check("GATE_ALLOW=1 -> real-time passes despite the tiny budget", not refuses(call))
os.environ.pop("GATE_ALLOW", None)

os.environ["GATE_RT_BUDGET"] = "1000"
check("real-time under a high GATE_RT_BUDGET -> passes", not refuses(call))

os.environ["GATE_RT_BUDGET"] = "0.0001"
os.environ["GATE_DISABLE"] = "1"
check("GATE_DISABLE=1 -> real-time passes (kill switch, fail-open)", not refuses(call))
os.environ.pop("GATE_DISABLE", None)

print(f"\n{'[FAIL]' if failures else 'OK'} gate fail-closed/open: {failures} failure(s)")
sys.exit(1 if failures else 0)
