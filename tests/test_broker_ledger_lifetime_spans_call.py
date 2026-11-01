"""Regression: a COMPLETED codex delegation must not be reported failed by the 5s ledger read deadline.

Two distinct, proven defects of the request-lifetime ledger scope (both observed as
"Ledger read exceeded wall-clock budget 5.0 seconds" on a job whose work already landed):

  #1 THE JOB-FAILING RAISE (codex_daemon_broker / codex_daemon pool): the cancellation scope that wraps the
     warm turn was opened with the 5s ledger-read DEFAULT, but a warm/agentic turn legitimately runs for
     minutes. A serving-path ledger op (e.g. a pooled_ledger_conn open) after 5s then raised LedgerQueryDeadline
     NON-best-effort; the broker re-wraps any exception as "codex broker request refused: {exc}", failing a
     completed job. Fix: span the scope across the CALL's own budget (never shorter than the old default).

  #2 SILENTLY-LOST ACCOUNTING (delegate_router): the terminal success-booking (guard.record_decision /
     record_saving) is @best_effort_ledger_function, so under an expired ambient scope it SWALLOWS the deadline
     (warn-once) and LOSES the value-proof row — the job survives but its accounting vanishes. Fix: book it under
     ledger_evidence_persistence_scope() (a fresh, uncancellable budget), so the row records despite the expired
     ambient request scope.

Offline only; no provider/network/paid call; isolated tmp ledger. Prove-it-can-fail is explicit for BOTH.
"""
import os
import socket
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-broker-ledger-lifetime-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config, guard, delegate_router, codex_daemon_broker  # noqa: E402
from spendguard.ledger_read import ledger_request_scope, LedgerQueryDeadline  # noqa: E402

# A warm/agentic turn's legitimate window dwarfs the 5s read default; the tests simulate it with a short
# overrun against a short scope so no test actually sleeps for minutes.
SHORT_SCOPE_S = 0.4
OVERRUN_S = SHORT_SCOPE_S + 0.3
STRESS_ITERS = 25


class _CheckCollector:
    """Owns its own failure list, so recording a check never mutates module-level state."""
    def __init__(self):
        self.failed = []

    def record(self, label, condition):
        ok = bool(condition)
        if not ok:
            self.failed.append(label)
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")


def _noop_schema(db):
    return None


def _decisions_count():
    # Read under a fresh persistence budget so the read itself is never subject to a test's expired scope.
    from spendguard.ledger_read import ledger_evidence_persistence_scope
    with ledger_evidence_persistence_scope():
        return guard._decisions_db().execute("SELECT COUNT(*) FROM decisions").fetchone()[0]


def _open_pooled_conn():
    return config.pooled_ledger_conn("_broker_lifetime_probe", _noop_schema)


def _raises_deadline(fn):
    try:
        fn()
        return False
    except LedgerQueryDeadline:
        return True


# ───────────────────────── Defect #1: the broker/pool cancellation scope must span the call ─────────────────

def defect1_prove_it_can_fail(checks):
    """OLD behavior: a short (5s-default-sized) scope + a turn that outlasts it aborts a serving-path ledger op."""
    pair_a, pair_b = socket.socketpair()
    try:
        raised = 0
        with codex_daemon_broker._broker_request_ledger_lifetime(pair_a, deadline_s=SHORT_SCOPE_S):
            time.sleep(OVERRUN_S)
            for _ in range(STRESS_ITERS):
                if _raises_deadline(_open_pooled_conn):
                    raised += 1
        print(f"  short-scope (old 5s default) overrun failure rate = {raised}/{STRESS_ITERS}")
        checks.record("defect#1 prove-it-can-fail: a too-short scope DOES abort the completed turn's ledger op",
                      raised == STRESS_ITERS)
    finally:
        pair_a.close()
        pair_b.close()


def defect1_fix_spans_call(checks):
    """FIX: a scope sized to the call budget does NOT abort a serving-path ledger op after the old 5s window."""
    pair_a, pair_b = socket.socketpair()
    try:
        raised = 0
        long_budget = OVERRUN_S + 30.0  # mirrors "call_timeout + grace + startup" spanning the whole turn
        with codex_daemon_broker._broker_request_ledger_lifetime(pair_a, deadline_s=long_budget):
            time.sleep(OVERRUN_S)  # well past the old 5s-equivalent window, but within the call budget
            for _ in range(STRESS_ITERS):
                if _raises_deadline(_open_pooled_conn):
                    raised += 1
        print(f"  call-spanning scope overrun failure rate = {raised}/{STRESS_ITERS}")
        checks.record("defect#1 fix: a call-spanning scope does NOT abort the completed turn's ledger op",
                      raised == 0)
    finally:
        pair_a.close()
        pair_b.close()


def defect1_serve_one_sizes_scope_to_call(checks):
    """Un-regressable: _serve_one must size the ledger lifetime to the call budget, never the 5s read default.

    Capture the deadline_s _serve_one passes into _broker_request_ledger_lifetime for a long-timeout call and
    assert it spans that call (>> the 5s read default), so the spurious abort cannot return by a future edit.
    """
    captured = {}
    real_lifetime = codex_daemon_broker._broker_request_ledger_lifetime

    def capturing_lifetime(connection, *, deadline_s=None):
        captured["deadline_s"] = deadline_s
        return real_lifetime(connection, deadline_s=deadline_s)

    broker = codex_daemon_broker._CodexDaemonBroker.__new__(codex_daemon_broker._CodexDaemonBroker)

    class _LocalStub:
        CALL_TIMEOUT_S = 15.0
        STARTUP_TIMEOUT_S = 0.0

        def run_warm(self, **kwargs):
            return {"text": "ok", "thread": "t", "error": None}

    broker._local = _LocalStub()
    broker._handler_slots = _ReleasableSlots()
    call_timeout = 1800.0  # an agentic repo turn
    request = {"version": codex_daemon_broker._PROTOCOL_VERSION, "method": "run_warm",
               "kwargs": {"prompt": "x", "timeout": call_timeout}, "execution": {}}
    conn = _InMemoryFramedConnection(request)
    with patch.object(codex_daemon_broker, "_broker_request_ledger_lifetime", capturing_lifetime):
        broker._serve_one(conn)
    print(f"  _serve_one passed ledger deadline_s = {captured.get('deadline_s')} for a {call_timeout}s call")
    checks.record("defect#1 un-regressable: _serve_one sizes ledger scope to the call budget (>> 5s read default)",
                  captured.get("deadline_s") is not None
                  and captured["deadline_s"] >= call_timeout
                  and captured["deadline_s"] > config.LEDGER_QUERY_DEADLINE_S)


# ───────────────────────── Defect #2: terminal accounting records despite an expired ambient scope ─────────

def _assemble_stubbed(**overrides):
    execution = {"text": "done", "in_tok": 10, "out_tok": 20, "cost": 0.0, "execution_started": True}
    kwargs = dict(execution=execution, result={"status": "pending"},
                  classification={"kind": "oneshot", "why": "independent answer"},
                  estimate={"plan": "codex", "model": "test-model", "input_tokens": 10, "output_tokens": 20},
                  intent="spendguard:delegate-task", changed_files=[])
    kwargs.update(overrides)
    # Deliberately do NOT stub _measured_claude_overage_ratio: its ledger READ must run under the persistence
    # scope too, so the real read exercises the widened coverage (on the isolated empty ledger it returns None).
    with patch.object(delegate_router, "_finalize_accepted_delegate_workspace", lambda execution: None), \
         patch.object(delegate_router.lane_registry, "lane_spec",
                      lambda plan: {"provider": "codex", "exec": "codex_exec"}), \
         patch.object(delegate_router.pricing, "realtime_cost", lambda *a, **k: 0.0):
        return delegate_router._assemble_delegate_execution_result(**kwargs)


def defect2_prove_it_can_fail(checks):
    """OLD behavior: booking the decision directly (best-effort) under an expired ambient scope LOSES the row."""
    before = _decisions_count()
    with ledger_request_scope(deadline_s=SHORT_SCOPE_S):
        time.sleep(OVERRUN_S)
        guard.record_decision(intent="spendguard:delegate-task", requested_model="claude-overage",
                              chosen_model="test-model", counterfactual_usd=0.0, actual_usd=0.0,
                              saved_usd=0.0, basis="delegate", why="prove-it-can-fail")
    after = _decisions_count()
    checks.record("defect#2 prove-it-can-fail: best-effort booking under an expired scope LOSES the row",
                  after == before)


def defect2_fix_records_through_persistence_scope(checks):
    """FIX: _assemble_delegate_execution_result books under the persistence scope → row records; job executed.

    The persistence scope must cover the whole ledger-touching region, including the overage-ratio READ. Seed
    the real money ledger (spend_events) so that read exercises the fix instead of a bare missing-table error —
    in production the ledger is always initialized before any delegation reaches this path.
    """
    from spendguard.ledger import SpendLedger
    SpendLedger()  # _ensure_schema() creates spend_events; the real _measured_claude_overage_ratio read needs it
    before = _decisions_count()
    with ledger_request_scope(deadline_s=SHORT_SCOPE_S):
        time.sleep(OVERRUN_S)
        result = _assemble_stubbed()
    after = _decisions_count()
    checks.record("defect#2 fix: completed delegation returns status 'executed' despite the expired ambient scope",
                  isinstance(result, dict) and result.get("status") == "executed")
    checks.record("defect#2 fix: the value-proof decision row RECORDS through the persistence scope",
                  after == before + 1)


class _ReleasableSlots:
    def release(self):
        return None


class _InMemoryFramedConnection:
    """Minimal stand-in for _serve_one's socket: carries ONE request frame, captures the response, no real I/O.

    _serve_one only needs _receive_frame to return the request and _send_frame to accept a reply; it also calls
    settimeout/close. This keeps the un-regressable scope-sizing assertion free of real sockets and timing.
    """
    def __init__(self, request):
        self._request = request
        self.sent = []

    def settimeout(self, _):
        return None

    def close(self):
        return None


def _patched_frames():
    return (patch.object(codex_daemon_broker, "_receive_frame", lambda conn, *a, **k: conn._request),
            patch.object(codex_daemon_broker, "_send_frame", lambda conn, payload: conn.sent.append(payload)))


def main():
    checks = _CheckCollector()
    print("Defect #1 — broker/pool cancellation scope must span the call window:")
    defect1_prove_it_can_fail(checks)
    defect1_fix_spans_call(checks)
    frame_recv, frame_send = _patched_frames()
    with frame_recv, frame_send:
        defect1_serve_one_sizes_scope_to_call(checks)

    print("\nDefect #2 — terminal accounting must record despite an expired ambient scope:")
    defect2_prove_it_can_fail(checks)
    defect2_fix_records_through_persistence_scope(checks)

    print(f"\n{'ALL PASS' if not checks.failed else 'FAILURES: ' + ', '.join(checks.failed)}")
    sys.exit(1 if checks.failed else 0)


if __name__ == "__main__":
    main()
