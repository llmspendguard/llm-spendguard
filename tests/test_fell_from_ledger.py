"""ITEM 4 — a metered call that fell over from a DOWN $0 lane is recorded on the DURABLE calls ledger with the lane it
fell FROM (`fell_from`), so `SUM(cost) WHERE fell_from=lane` is the $0 savings lost while that lane was down — the fact
an operator needs to know "re-login codex to stop paying the API." This asserts the two recording paths (explicit param
for the FAILURE recorder; thread-local context for the SUCCESS recorder gate._record_rt uses), the reader's per-lane
rollup, that a NORMAL call carries NULL (never polluting the rollup), and that fell_from_context nests without erasing
an outer scope. Offline + isolated home: we drive calls.record_call directly and read the ledger back — no live call.
"""
import os
import sys
import sqlite3
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-fellfrom-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["SPENDGUARD_CALLS"] = "1"                       # record content too (harmless here); the forensic row lands regardless
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calls, reliability, config   # noqa: E402


def ck(results, label, cond, extra=""):
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def _fell_from_of(call_id):
    """Read one row's fell_from straight from the ledger file (the durable truth, not the in-process return)."""
    con = sqlite3.connect(config.db_path())
    row = con.execute("SELECT fell_from FROM calls WHERE id=?", (call_id,)).fetchone()
    con.close()
    return row[0] if row else "<<no row>>"


def main():
    results = []

    # ── the FAILURE-recorder path: fell_from passed EXPLICITLY (adapters L1128 for a failed fallback) ──
    cid_codex_1 = calls.record_call("openai", "gpt-x", "realtime", 0.05, in_tok=100, out_tok=20,
                                    intent="fell-from-test", fell_from="codex")
    ck(results, "explicit fell_from is persisted on the ledger row", _fell_from_of(cid_codex_1) == "codex",
       extra=_fell_from_of(cid_codex_1))

    # ── the SUCCESS-recorder path: fell_from read from the thread-local CONTEXT (gate._record_rt passes no arg) ──
    with calls.fell_from_context("codex"):
        cid_codex_2 = calls.record_call("openai", "gpt-x", "realtime", 0.03, in_tok=50, out_tok=10,
                                        intent="fell-from-test")   # NO fell_from arg — must come from context
    ck(results, "fell_from read from context when the recorder passes no arg", _fell_from_of(cid_codex_2) == "codex",
       extra=_fell_from_of(cid_codex_2))

    # another lane, and a plain (no-fallback) call that must stay NULL
    with calls.fell_from_context("gemini"):
        calls.record_call("openai", "gpt-x", "realtime", 0.02, in_tok=40, out_tok=8, intent="fell-from-test")
    cid_plain = calls.record_call("openai", "gpt-x", "realtime", 0.01, in_tok=10, out_tok=4, intent="fell-from-test")
    ck(results, "a NORMAL call (no fallback) carries fell_from = NULL", _fell_from_of(cid_plain) is None,
       extra=str(_fell_from_of(cid_plain)))

    # ── the reader: per-lane rollup, ordered by $ DESC, and the plain call NEVER appears ──
    rows = reliability.lane_fallback_spend()
    by_lane = {r["lane"]: r for r in rows}
    ck(results, "rollup groups codex: 2 calls, $0.08", by_lane.get("codex", {}).get("n_calls") == 2
       and abs(by_lane.get("codex", {}).get("metered_usd", 0) - 0.08) < 1e-6, extra=str(by_lane.get("codex")))
    ck(results, "rollup groups gemini: 1 call, $0.02", by_lane.get("gemini", {}).get("n_calls") == 1
       and abs(by_lane.get("gemini", {}).get("metered_usd", 0) - 0.02) < 1e-6, extra=str(by_lane.get("gemini")))
    ck(results, "the plain call's provider is NOT a lane in the rollup (no NULL/empty key)",
       None not in by_lane and "" not in by_lane, extra=str(list(by_lane)))
    ck(results, "ordered by $ DESC — codex ($0.08) before gemini ($0.02)",
       [r["lane"] for r in rows][:2] == ["codex", "gemini"], extra=str([r["lane"] for r in rows]))

    # ── `since` filter is honored (a far-future lower bound excludes everything) ──
    ck(results, "since=far-future returns nothing (the ts filter is applied)",
       reliability.lane_fallback_spend(since="2999-01-01T00:00:00+00:00") == [], extra="expected []")

    # ── fell_from_context NESTS without erasing an outer scope, and fully restores on exit ──
    ck(results, "no ambient fell_from before any scope", calls.current().get("fell_from") is None)
    with calls.fell_from_context("outer"):
        inner_seen = None
        with calls.fell_from_context("inner"):
            inner_seen = calls.current().get("fell_from")
        ck(results, "inner scope sees 'inner'", inner_seen == "inner", extra=str(inner_seen))
        ck(results, "outer scope RESTORED after inner exits (not erased)",
           calls.current().get("fell_from") == "outer", extra=str(calls.current().get("fell_from")))
    ck(results, "context fully cleared after the outermost scope", calls.current().get("fell_from") is None,
       extra=str(calls.current().get("fell_from")))

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_fell_from_ledger: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
