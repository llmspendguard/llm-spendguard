"""The two attribution gaps a metered_only refute run surfaced — closed and GUARDED so neither can regress.

GAP 1 — a billed METERED row recorded executor=None. gate._record_rt is the ONE recorder for every realtime/metered
        call (the SDK gate + http_capture, whose PROVIDER_HOSTS are only the metered openai/anthropic/google hosts). A
        subscription LANE never reaches it: lanes run as subprocess / raw-urllib CLIs and are recorded separately as
        kind='subscription' with their lane name. So a realtime row is BY CONSTRUCTION the metered provider API and
        must record executor='api' — the SAME value the metered result dict carries (adapters._call_once). Before the
        fix the row's executor was None, so the receipt's lane view / served_by could not tell a metered call from an
        untagged one, and a billed row had no path recorded — an attribution hole in spendguard's core mission.

GAP 2 — spendguard's OWN effort-discovery probe (vendor_call.discover_efforts, reached from models.resolve_effort
        during a batch build / pinned-matrix / metered_only run) fired a tiny PAID call with NO intent, so it landed
        in '(none)' and tripped the "PAID call with NO intent" warning. Like every other internal spendguard call it
        must run under a spendguard:* META intent (gate._meta_intent → the segregated meta ledger), NOT workload.

Offline + hermetic — no network, no LLM. GAP 1 drives gate._record_rt with an AUTHORITATIVE cost (so it skips
pricing) and reads the row back out of the calls ledger. GAP 2 stubs adapters.call to CAPTURE the ambient intent at
probe time, and also proves the tag NESTS (the caller's intent is restored on exit).
"""
import os
import sys
import sqlite3
import tempfile

import atexit as _atexit   # noqa: E402
import shutil as _shutil   # noqa: E402
_SG_HOME = tempfile.mkdtemp(prefix="sg-attr-")
os.environ["SPENDGUARD_HOME"] = _SG_HOME
_atexit.register(_shutil.rmtree, _SG_HOME, ignore_errors=True)   # clean up the temp HOME — don't leak it into $TMPDIR
os.environ["SPENDGUARD_CALLS"] = "1"                       # enable call logging (it FAILS CLOSED without this)
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import gate, calls, config, vendor_call, adapters   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


def _last_row():
    """The most-recently recorded (kind, executor, intent, effort) row from the calls ledger."""
    con = sqlite3.connect(config.db_path())
    try:
        return con.execute("SELECT kind, executor, intent, effort FROM calls ORDER BY rowid DESC LIMIT 1").fetchone()
    finally:
        con.close()


fails = []

print("-- GAP 1a: a WORKLOAD metered/realtime row records executor='api' (never None) --")
# authoritative cost → _record_rt skips pricing; a plain workload intent → the non-meta branch (what a real metered
# call takes). The row must carry executor='api' AND the reasoning_effort actually sent.
with calls.context(intent="attr-test-workload"):
    gate._record_rt("gpt-5.6-luna", {"model": "gpt-5.6-luna", "reasoning_effort": "none"},
                    in_tok=100, out_tok=50, latency=0.2, output="ok", finish="stop", cost=0.01, provider="openai")
_row = _last_row()
fails += report_check("a realtime row was recorded", bool(_row) and _row[0] == "realtime")
fails += report_check("executor == 'api' (the metered path is NAMED, not None)", bool(_row) and _row[1] == "api")
fails += report_check("the workload intent rode the row (attribution intact)", bool(_row) and _row[2] == "attr-test-workload")
fails += report_check("the applied reasoning_effort rode the row too ('none')", bool(_row) and _row[3] == "none")

print("\n-- GAP 1b: the META branch of _record_rt ALSO records executor='api' (both twins fixed) --")
with calls.context(intent="spendguard:attr-probe-test"):
    gate._record_rt("gpt-5.6-sol", {"model": "gpt-5.6-sol"},
                    in_tok=10, out_tok=5, latency=0.1, output="ok", finish="stop", cost=0.02, provider="openai")
_mrow = _last_row()
fails += report_check("the meta row records executor='api' too", bool(_mrow) and _mrow[1] == "api")
fails += report_check("the meta row kept its spendguard:* intent", bool(_mrow) and _mrow[2] == "spendguard:attr-probe-test")

print("\n-- GAP 2: discover_efforts runs its probe under a spendguard:* META intent (never workload '(none)') --")
probe_calls = []
_orig_call = adapters.call


def _capture_call(*a, **kw):
    """Stand in for a real probe call: RECORD the ambient intent + the metered_only flag at call time, return a
    clean 'accepted' result (no network)."""
    probe_calls.append({"intent": (calls.current() or {}).get("intent"), "metered_only": kw.get("metered_only")})
    return {"error": None, "dropped": [], "text": "OK"}


adapters.call = _capture_call
before = (calls.current() or {}).get("intent")               # the baseline the whole stack must unwind back to
try:
    with calls.context(intent="honestreview:refute"):        # the CALLER's workload intent wrapping the probe
        vendor_call.discover_efforts("openai", "probe-model-x", refresh=True)
        inner_after = (calls.current() or {}).get("intent")
    outer_after = (calls.current() or {}).get("intent")
finally:
    adapters.call = _orig_call

fails += report_check("the probe fired at least once", len(probe_calls) > 0)
fails += report_check("EVERY probe call ran under intent 'spendguard:effort-probe' (meta, not '(none)')",
                      bool(probe_calls) and all(c["intent"] == "spendguard:effort-probe" for c in probe_calls))
fails += report_check("the tag is spendguard:* so gate._meta_intent routes it to the META ledger",
                      bool(probe_calls) and all((c["intent"] or "").startswith("spendguard:") for c in probe_calls))
fails += report_check("ROBUSTNESS: every probe forces the METERED path via metered_only=True (a per-call, thread-safe "
                      "flag — no process-global env race; a lane can't mask the API's real capability)",
                      bool(probe_calls) and all(c["metered_only"] is True for c in probe_calls))
fails += report_check("NESTING: the caller's intent is restored inside its own block after the probe returns",
                      inner_after == "honestreview:refute")
fails += report_check("NESTING: the whole stack unwinds back to the baseline after the caller's block exits",
                      outer_after == before)

print("\n-- GAP 3: adapters.call(sig=X) TAGS the ledger row — sig sets the thread-local intent for the dispatch --")
# gate._record_rt reads the THREAD-LOCAL intent; sig= alone used NOT to set it, so a metered call passing only sig=
# landed in '(none)' despite the tag. adapters.call now sets it for the dispatch (and restores it), so the row is
# attributed. Stub the dispatch to CAPTURE the ambient intent at call time (no network).
seen_dispatch = {}
_orig_guarded = adapters._call_guarded


def _capture_guarded(*a, **kw):
    seen_dispatch["intent"] = (calls.current() or {}).get("intent")
    return {"text": "ok", "cost": 0.0, "executor": "api", "error": None, "in_tok": 1, "out_tok": 1}


base_intent = (calls.current() or {}).get("intent")           # no ambient context here
adapters._call_guarded = _capture_guarded
try:
    adapters.call("gpt-5.6-luna", "hi", sig="loinc-typing")
    sig_seen = seen_dispatch.get("intent")                    # what the dispatch saw for a bare sig= (no context)
    after_call = (calls.current() or {}).get("intent")
    with calls.context(intent="ambient-intent"):              # an EXPLICIT ambient context must WIN over sig
        adapters.call("gpt-5.6-luna", "hi", sig="a-different-sig")
        ambient_seen = seen_dispatch.get("intent")
finally:
    adapters._call_guarded = _orig_guarded

fails += report_check("the dispatch saw sig as the thread-local intent (so gate._record_rt tags the row, not '(none)')",
                      sig_seen == "loinc-typing")
fails += report_check("the context is RESTORED to baseline after the call (no leak past the dispatch)",
                      after_call == base_intent)
fails += report_check("an explicit ambient calls.context intent WINS over sig (never overridden)",
                      ambient_seen == "ambient-intent")

print(f"\n{'[FAIL]' if fails else 'OK'} test_metered_row_attribution: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
