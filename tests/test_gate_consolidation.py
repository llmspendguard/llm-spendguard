"""GATE CONSOLIDATION GUARD — three drift-prone shapes in the gate (the path of EVERY LLM call) were each folded
into ONE shared brain. This pins BOTH halves of each fold: (a) the delegators keep ROUTING THROUGH the shared
brain — re-inlining the logic re-introduces the drift and FAILS this test — and (b) the shared brain keeps the
invariant it owns.

  1. fire-once    — _StreamProxy._fire and _AsyncStreamProxy._fire both route through _fire_once, which fires the
                    done-callback EXACTLY once (idempotent across stream exhaustion + context-manager exit).
  2. patch-method — _apply / _apply_rt / _apply_units / _apply_stream all route through _patch_method; it resolves
                    module.Class.method, is idempotent (already-gated → skip), and honors optional (absent surface
                    → skip, vs a REQUIRED surface → raise).
  3. wrap-gated   — _gate_wrap / _wrap_rt / _wrap_rt_units all route through _wrap_gated; each upholds passthrough,
                    the _spend_gated marker, deliberate-refusal-propagates, and fail-open on a non-refusal pre-hook
                    bug — INCLUDING _wrap_rt_units, which tests/test_gate_properties.py did not previously cover.

Hermetic: recorder/precheck/accounting no-op'd (control flow, not the ledger), a fake SDK module in sys.modules;
no network, zero spend."""
import os
import sys
import tempfile
import asyncio

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-gateconsol-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import types                                   # noqa: E402
from spendguard import gate                    # noqa: E402
from spendguard.gate import SpendGateRefused   # noqa: E402

# Keep the properties about CONTROL FLOW (route + passthrough + fail-open), not ledger writes: no-op every recorder
# and precheck so a $0 estimate can't refuse and passthrough is about the wrapper, exactly as test_gate_properties.
_noop = lambda *a, **k: None                   # noqa: E731
gate._record_rt = _noop
gate._rt_account = _noop
gate._rt_precheck = _noop
gate._rt_precheck_usd = _noop

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def run(w, is_async, **kw):
    return asyncio.run(w(None, **kw)) if is_async else w(None, **kw)


# ───────────────────────────── 1. fire-once ─────────────────────────────
print("-- fire-once: both stream proxies route through _fire_once, which fires exactly once --")
_seen_fire = []
_saved = gate._fire_once
gate._fire_once = lambda sg: _seen_fire.append(sg)
try:
    for cls in (gate._StreamProxy, gate._AsyncStreamProxy):
        cls(iter([]), {}, lambda: None)._fire()
    ck("_StreamProxy._fire AND _AsyncStreamProxy._fire delegate to _fire_once", len(_seen_fire) == 2)
finally:
    gate._fire_once = _saved

_fired = []
_sg = [None, None, lambda: _fired.append(1), False]
gate._fire_once(_sg)
gate._fire_once(_sg)
gate._fire_once(_sg)
ck("_fire_once fires the done-callback EXACTLY once across repeated calls", _fired == [1])


# ─────────────────────────── 2. patch-method ────────────────────────────
print("\n-- patch-method: all four _apply* route through _patch_method; idempotency + optional --")
_fake = types.ModuleType("fake_gated_sdk")


class FakeClient:
    def create(self, **kw):
        return "R"


_fake.FakeClient = FakeClient
sys.modules["fake_gated_sdk"] = _fake

_seen_patch = []
_saved = gate._patch_method
gate._patch_method = lambda mp, cn, m, mk, **kw: _seen_patch.append(kw.get("optional", False))
try:
    gate._apply("fake_gated_sdk", "FakeClient", "create", lambda kw, a: None, False)
    gate._apply_rt("fake_gated_sdk", "FakeClient", "create", lambda kw: ("m", 1, 2), lambda kw, r: (1, 2), False)
    gate._apply_units("fake_gated_sdk", "FakeClient", "create", lambda kw: ("m", 0.0), lambda kw, r: None, False)
    gate._apply_stream("fake_gated_sdk", "FakeClient", "create", "resp",
                       lambda kw: ("m", 1, 2), lambda kw, r: (1, 2), False)
    ck("all FOUR _apply* delegate to _patch_method", len(_seen_patch) == 4)
    ck("only _apply_stream passes optional=True (the others are required)", _seen_patch == [False, False, False, True])
finally:
    gate._patch_method = _saved


def _mk(cur):                                  # a trivial gated wrapper builder
    def w(self, *a, **k):
        return cur(self, *a, **k)
    w._spend_gated = True
    return w


FakeClient.create = lambda self, **kw: "R"      # start clean (unwrapped)
gate._patch_method("fake_gated_sdk", "FakeClient", "create", _mk)
ck("a fresh method gets wrapped (marker set)", getattr(FakeClient.create, "_spend_gated", False))
_w1 = FakeClient.create
gate._patch_method("fake_gated_sdk", "FakeClient", "create", _mk)
ck("an already-gated method is NOT re-wrapped (idempotent)", FakeClient.create is _w1)

_raised = False
try:
    gate._patch_method("fake_gated_sdk", "FakeClient", "nope_missing", _mk)              # required + absent
except AttributeError:
    _raised = True
ck("a REQUIRED but absent method raises (the surface must exist)", _raised)

_ok = True
try:
    gate._patch_method("fake_gated_sdk", "FakeClient", "nope_missing", _mk, optional=True)  # optional + absent
except Exception:
    _ok = False
ck("an OPTIONAL absent method is skipped, not an error (older SDKs)", _ok)


# ──────────────────────────── 3. wrap-gated ─────────────────────────────
print("\n-- wrap-gated: all three wrappers route through _wrap_gated + keep the invariants --")
_seen_wrap = []
_saved = gate._wrap_gated


def _spy(orig, is_async, pre=None, post=None):
    _seen_wrap.append(is_async)
    return _saved(orig, is_async, pre=pre, post=post)


gate._wrap_gated = _spy
try:
    _o = lambda self, *a, **k: "R"             # noqa: E731
    gate._gate_wrap(_o, lambda kw, a: None, False)
    gate._wrap_rt(_o, lambda kw: ("m", 1, 2), lambda kw, r: (1, 2), False)
    gate._wrap_rt_units(_o, lambda kw: ("m", 0.0), lambda kw, r: None, False)
    ck("all THREE wrapper builders delegate to _wrap_gated", len(_seen_wrap) == 3)
finally:
    gate._wrap_gated = _saved

# behavioral invariants across all three REAL wrappers (incl. _wrap_rt_units, previously uncovered), sync + async
sentinel = object()
_sync = lambda self, *a, **k: sentinel          # noqa: E731


async def _async(self, *a, **k):
    return sentinel


def _build(name, is_async):
    o = _async if is_async else _sync
    if name == "gate":
        return gate._gate_wrap(o, lambda kw, a: None, is_async)
    if name == "rt":
        return gate._wrap_rt(o, lambda kw: (kw.get("model"), 1, 2), lambda kw, r: (1, 2), is_async)
    return gate._wrap_rt_units(o, lambda kw: (kw.get("model"), 0.0), lambda kw, r: None, is_async)


for name in ("gate", "rt", "units"):
    for is_async in (False, True):
        w = _build(name, is_async)
        ck(f"{name}[async={is_async}]: sets the _spend_gated marker", getattr(w, "_spend_gated", False))
        ck(f"{name}[async={is_async}]: passthrough returns the EXACT underlying result",
           run(w, is_async, model="m") is sentinel)


def _refuse(*a, **k):
    raise SpendGateRefused("blocked")


def _boom(*a, **k):
    raise ValueError("injected gate bug")


print("\n-- deliberate refusal in the pre-hook PROPAGATES; a non-refusal pre-hook bug FAILS OPEN (all three) --")
gate._rt_precheck = _refuse                     # drives _wrap_rt's pre-hook (_rt_precheck_guard) to refuse
for name, w in [
    ("gate", gate._gate_wrap(_sync, lambda kw, a: _refuse(), False)),
    ("rt", gate._wrap_rt(_sync, lambda kw: ("m", 1, 2), lambda kw, r: (1, 2), False)),
    ("units", gate._wrap_rt_units(_sync, _refuse, lambda kw, r: None, False)),
]:
    raised = False
    try:
        w(None, model="m")
    except SpendGateRefused:
        raised = True
    ck(f"{name}: a DELIBERATE refusal in the pre-hook propagates (never swallowed)", raised)
gate._rt_precheck = _noop

gate._rt_precheck = _boom                        # an unintended pre-hook BUG (not an enforcement decision)
for name, w in [
    ("gate", gate._gate_wrap(_sync, lambda kw, a: _boom(), False)),
    ("rt", gate._wrap_rt(_sync, lambda kw: ("m", 1, 2), lambda kw, r: (1, 2), False)),
    ("units", gate._wrap_rt_units(_sync, _boom, lambda kw, r: None, False)),
]:
    ck(f"{name}: a non-refusal pre-hook bug fails OPEN (call still returns the result)",
       w(None, model="m") is sentinel)
gate._rt_precheck = _noop

print(f"\n{'[FAIL]' if fails else 'OK'} test_gate_consolidation: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
