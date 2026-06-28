"""Gate invariants (property/fuzz) — the gate sits in the call path of EVERY LLM call, so it must uphold two
properties no matter what:

  1. PASSTHROUGH — the gate returns EXACTLY the underlying call's result (same object for non-stream; the same
     chunks, in order, for a stream). It never substitutes or mutates the result.
  2. FAIL-OPEN — only a DELIBERATE enforcement decision (SpendGateRefused / GateBlocked) may raise into the caller.
     ANY other error inside the gate — a bug in the estimator, the precheck, accounting, the stream proxy — is
     swallowed and the call proceeds. A gate bug must never break a legitimate LLM call.

Hypothesis drives random kwargs, random injected helper failures, and random stream shapes through the real
wrappers (`_wrap_rt` realtime + `_wrap` batch), sync and async. Offline, mocked, zero spend."""
import os, sys, tempfile, asyncio

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-prop-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from hypothesis import given, strategies as st, settings, HealthCheck
from spendguard import gate
from spendguard.gate import SpendGateRefused
from spendguard import bulkgate

S = settings(max_examples=60, deadline=None, suppress_health_check=list(HealthCheck))

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# Keep the properties about CONTROL FLOW (passthrough + fail-open), not ledger writes: no-op the recorder by default
# so Hypothesis stays fast + hermetic. Recording correctness is covered by test_stream_capture.py.
gate._record_rt = lambda *a, **k: None

def est_ok(kw):
    return (kw.get("model") or "m", 10, 20)
def act_ok(_r):
    return (10, 20)
def boom(*a, **k):
    raise ValueError("injected gate bug")

class Chunk:
    def __init__(self, i): self.i = i; self.choices = []; self.usage = None
class Stream:                       # a minimal sync stream + extra attr (delegation)
    def __init__(self, n): self._c = [Chunk(i) for i in range(n)]; self.response = "RESP"
    def __iter__(self): return iter(self._c)
class AStream:
    def __init__(self, n): self._c = [Chunk(i) for i in range(n)]; self.response = "RESP"
    def __aiter__(self):
        async def g():
            for c in self._c: yield c
        return g()

def run(w, is_async, **kw):
    return asyncio.run(w(None, **kw)) if is_async else w(None, **kw)

def kw_strat(stream=False):
    return st.fixed_dictionaries({
        "model": st.sampled_from(["gpt-5.5", "claude-opus-4-8", "", "weird/model:1"]),
        "messages": st.just([{"role": "user", "content": "x"}]),
        "max_tokens": st.integers(min_value=0, max_value=4000),
        "stream": st.just(stream),
    })

# ── 1. PASSTHROUGH: non-stream returns the EXACT underlying object (sync + async) ──
@given(kw=kw_strat(), is_async=st.booleans())
@S
def p_passthrough(kw, is_async):
    sentinel = object()
    orig = (lambda self, *a, **k: sentinel)
    if is_async:
        async def orig(self, *a, **k): return sentinel  # noqa: F811
    w = gate._wrap_rt(orig, est_ok, act_ok, is_async)
    assert run(w, is_async, **dict(kw)) is sentinel, "gate altered the non-stream result"

# ── 2. FAIL-OPEN pre-call: a bug in est_fn / precheck must NOT break the call ──
@given(kw=kw_strat(), is_async=st.booleans(), where=st.sampled_from(["est", "precheck"]))
@S
def p_failopen_precall(kw, is_async, where):
    sentinel = object()
    if is_async:
        async def orig(self, *a, **k): return sentinel
    else:
        orig = lambda self, *a, **k: sentinel
    est = boom if where == "est" else est_ok
    saved = gate._rt_precheck
    try:
        if where == "precheck":
            gate._rt_precheck = boom            # an unintended bug in precheck (NOT an enforcement decision)
        w = gate._wrap_rt(orig, est, act_ok, is_async)
        assert run(w, is_async, **dict(kw)) is sentinel, "a pre-call gate bug broke the call"
    finally:
        gate._rt_precheck = saved

# ── 3. FAIL-OPEN post-call: a bug in accounting must NOT break the call ──
@given(kw=kw_strat(), is_async=st.booleans(), where=st.sampled_from(["account", "record"]))
@S
def p_failopen_postcall(kw, is_async, where):
    sentinel = object()
    if is_async:
        async def orig(self, *a, **k): return sentinel
    else:
        orig = lambda self, *a, **k: sentinel
    saved_acc, saved_rec = gate._rt_account, gate._record_rt
    try:
        if where == "account":
            gate._rt_account = boom
        else:
            gate._record_rt = boom
        w = gate._wrap_rt(orig, est_ok, act_ok, is_async)
        assert run(w, is_async, **dict(kw)) is sentinel, "a post-call gate bug broke the call"
    finally:
        gate._rt_account, gate._record_rt = saved_acc, saved_rec

# ── 4. ENFORCEMENT still propagates: a deliberate block raises into the caller ──
@given(is_async=st.booleans(), exc=st.sampled_from([SpendGateRefused, bulkgate.GateBlocked]))
@S
def p_enforcement_propagates(is_async, exc):
    sentinel = object()
    if is_async:
        async def orig(self, *a, **k): return sentinel
    else:
        orig = lambda self, *a, **k: sentinel
    def block(*a, **k): raise exc("blocked")
    saved = gate._rt_precheck
    try:
        gate._rt_precheck = block
        w = gate._wrap_rt(orig, est_ok, act_ok, is_async)
        raised = False
        try:
            run(w, is_async, model="gpt-5.5", messages=[], max_tokens=10, stream=False)
        except exc:
            raised = True
        assert raised, "a deliberate enforcement block did NOT propagate"
    finally:
        gate._rt_precheck = saved

# ── 5. STREAM passthrough: every chunk passes through, in order (sync + async) ──
@given(n=st.integers(min_value=0, max_value=25), is_async=st.booleans())
@S
def p_stream_passthrough(n, is_async):
    if is_async:
        async def orig(self, *a, **k): return AStream(n)
        async def drain(w):
            return [c async for c in await w(None, model="gpt-5.5", messages=[], stream=True)]
        got = asyncio.run(drain(gate._wrap_rt(orig, est_ok, act_ok, True)))
    else:
        orig = lambda self, *a, **k: Stream(n)
        got = list(gate._wrap_rt(orig, est_ok, act_ok, False)(None, model="gpt-5.5", messages=[], stream=True))
    assert [c.i for c in got] == list(range(n)), "stream chunks were dropped/reordered/altered"

# ── 6. STREAM fail-open: a bug in usage capture must NOT drop chunks or raise ──
@given(n=st.integers(min_value=1, max_value=25), where=st.sampled_from(["pull", "record"]))
@S
def p_stream_failopen(n, where):
    orig = lambda self, *a, **k: Stream(n)
    saved_pull, saved_rec = gate._pull_usage, gate._record_rt
    try:
        if where == "pull":
            gate._pull_usage = boom
        else:
            gate._record_rt = boom
        got = list(gate._wrap_rt(orig, est_ok, act_ok, False)(None, model="gpt-5.5", messages=[], stream=True))
        assert [c.i for c in got] == list(range(n)), "a usage-capture bug dropped stream chunks"
    finally:
        gate._pull_usage, gate._record_rt = saved_pull, saved_rec

# ── 7. BATCH path (_wrap): gate_fn bug fails open; a deliberate refusal propagates ──
@given(is_async=st.booleans(), mode=st.sampled_from(["bug", "refuse", "ok"]))
@S
def p_batch(is_async, mode):
    sentinel = object()
    if is_async:
        async def orig(self, *a, **k): return sentinel
    else:
        orig = lambda self, *a, **k: sentinel
    def gate_fn(kw, a):
        if mode == "bug": raise ValueError("injected batch gate bug")
        if mode == "refuse": raise SpendGateRefused("blocked")
    w = gate._wrap(orig, gate_fn, is_async)
    if mode == "refuse":
        raised = False
        try:
            run(w, is_async)
        except SpendGateRefused:
            raised = True
        assert raised, "batch enforcement did not propagate"
    else:
        assert run(w, is_async) is sentinel, "batch gate %s broke the call" % mode


for name, fn in list(globals().items()):
    if name.startswith("p_"):
        try:
            fn()
            ck(name, True)
        except Exception as e:
            print(f"    falsifying/err: {e}")
            ck(name, False)

print(("[OK]" if not fails else "[FAIL]") + " gate-properties: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
