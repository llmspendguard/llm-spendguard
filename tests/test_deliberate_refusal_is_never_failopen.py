"""A DELIBERATE gate refusal must NEVER be caught by the handler meant for gate MALFUNCTIONS.

WHY THIS EXISTS. `gate._guard` is the single chokepoint every gated SDK call passes through. It re-raises a
deliberate refusal and fail-OPENs everything else (`database is locked`, a buggy register()'d fn) so a real job
is never broken by an accident. The refusal arm was written as `except SpendGateRefused` — an ENUMERATION — while
`bulkgate.GateBlocked` (the estimate+test-first lifecycle block) subclassed `Exception`, OUTSIDE that hierarchy.
So `_guard` caught GateBlocked as a malfunction and let the batch through: observed live 2026-08-28, a 712- and a
106-request `warden:describe` batch both printed "[bulkgate] BLOCKED …" and then ran anyway. The lifecycle eval
gate had been advisory-only at the SDK boundary since it was armed.

THE FIX, which this guards: every deliberate refusal shares ONE base (SpendGateRefused is the root; GateBlocked
inherits it), so the fail-open handlers name the CONCEPT, not a list. A NEW refusal type then blocks BY
CONSTRUCTION. The rogue-subclass case below is the real guard: it never mentions GateBlocked, yet must propagate.

Offline, isolated home, zero spend, no network."""
import os, sys, tempfile, asyncio

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-refusal-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

os.environ.pop("GATE_DISABLE", None)                 # the wrappers only run the guard when NOT disabled

from spendguard import gate as G
from spendguard import bulkgate
from spendguard.gate import SpendGateRefused

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

def raises_out(fn, exc):
    """True iff calling fn() lets `exc` (or a subclass) propagate OUT — i.e. the gate BLOCKED."""
    try:
        fn()
        return False
    except exc:
        return True

SENTINEL = object()

# ── 1. STRUCTURAL: every deliberate refusal shares the SpendGateRefused base (the concept, not a list) ──
ck("GateBlocked IS-A SpendGateRefused (shared refusal base, not bare Exception)",
   issubclass(bulkgate.GateBlocked, SpendGateRefused))
ck("SpendGateRefused is still a RuntimeError (base MRO preserved for `except RuntimeError` callers)",
   issubclass(SpendGateRefused, RuntimeError))

# ── 2. _guard: a DELIBERATE GateBlocked PROPAGATES (the exact live bug — it must not fail open) ──
def _blk(kw, a):
    raise bulkgate.GateBlocked("[bulkgate] BLOCKED … needs estimate+test+eval FIRST")
ck("_guard PROPAGATES bulkgate.GateBlocked (lifecycle block stops the batch, not fail-open)",
   raises_out(lambda: G._guard(_blk, {}, ()), bulkgate.GateBlocked))

# ── 3. _guard: a genuine MALFUNCTION still fails OPEN (a real job is never broken) ──
def _boom(kw, a):
    raise RuntimeError("database is locked")             # a gate hiccup, NOT a decision
def _guard_boom_returns():
    G._guard(_boom, {}, ()); return "survived"
ck("_guard FAILS OPEN on a malfunction (RuntimeError 'database is locked' is swallowed, call proceeds)",
   _guard_boom_returns() == "survived" and not raises_out(lambda: G._guard(_boom, {}, ()), Exception))

# ── 4. THE ROGUE: a brand-new refusal type blocks BY CONSTRUCTION — _guard never enumerated it ──
class _RogueRefusal(SpendGateRefused):
    """A refusal type invented AFTER _guard was written. If _guard named a list instead of the concept, this
    would fail open and nothing would notice — which is the whole class of bug."""
def _rogue(kw, a):
    raise _RogueRefusal("a future refusal nobody added to an except-tuple")
ck("_guard PROPAGATES a NEW SpendGateRefused subclass it was never told about (concept, not enumeration)",
   raises_out(lambda: G._guard(_rogue, {}, ()), _RogueRefusal))
# and the malfunction/refusal line is drawn by TYPE, not by identity: a plain Exception subclass still fails open
class _NotARefusal(Exception):
    pass
def _plain(kw, a):
    raise _NotARefusal("looks scary, is a bug")
ck("_guard FAILS OPEN on a non-refusal Exception subclass (only the refusal CONCEPT propagates)",
   not raises_out(lambda: G._guard(_plain, {}, ()), Exception))

# ── 5. THE REAL SDK BOUNDARY (_gate_wrap, sync + async): the path the live batch actually took ──
def orig_sync(self, *a, **k):
    return SENTINEL
async def orig_async(self, *a, **k):
    return SENTINEL

for is_async in (False, True):
    tag = "async" if is_async else "sync"
    orig = orig_async if is_async else orig_sync

    def run(w):
        return asyncio.new_event_loop().run_until_complete(w(None)) if is_async else w(None)

    # a GateBlocked from the gate_fn must propagate THROUGH the wrapper (mirrors warden:describe running anyway)
    w_blk = G._gate_wrap(orig, _blk, is_async)
    ck(f"[{tag}] _gate_wrap PROPAGATES GateBlocked out of the wrapped SDK call (batch is stopped)",
       raises_out(lambda: run(w_blk), bulkgate.GateBlocked))

    # a bug in the gate_fn must fail open: the wrapped call still returns its real result
    def _bug(kw, a):
        raise ValueError("injected gate bug")
    w_bug = G._gate_wrap(orig, _bug, is_async)
    got = None
    try:
        got = run(w_bug)
    except Exception as e:
        fails.append(f"[{tag}] _gate_wrap should fail open on a gate bug, but raised {e!r}")
    ck(f"[{tag}] _gate_wrap FAILS OPEN on a gate_fn bug (call returns its real result, unbroken)",
       got is SENTINEL)

print(("\n[OK] " if not fails else "\n[FAIL] ") + f"deliberate_refusal_is_never_failopen: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
