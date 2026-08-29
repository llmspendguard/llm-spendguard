"""Five verified-high SPEND-INTEGRITY defects from the 4-LLM self-review, each pinned:

  budget._reset_after_fork  — a child of os.fork() must not reuse the parent's sqlite connection (_conn) or its
                              thread-local ledger; both are dropped so the child reconnects fresh.
  budget._record_spend_event— a failed ledger write is the SOLE money-of-record's loss; it is now DURABLY
                              captured to a dead-letter file, not just warned to stderr (a daemon discards stderr).
  backfill.backfill         — an unpriced (cost=None) batch row used to hit round(None,4) → TypeError, aborting
                              the whole ingest; None now stays None.
  trust.check               — a server-side drift (in_sync False) must ELEVATE the overall level; it used only the
                              ledger verdict, so a drift left the status 'ok'.
  estimate_divergence.cmd   — UNJUDGED verdicts (couldn't-check) must not exit 0; cannot-tell != clean.

Offline, isolated home.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-spendint-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, backfill, trust, saas, config   # noqa: E402
from spendguard import estimate_divergence as ed               # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── 1. fork reset drops both sqlite connections ──────────────────────────────────────────────────────────────
budget._conn = "SENTINEL_CONN"
budget._LEDGER_TL.led = "SENTINEL_LEDGER"
budget._reset_after_fork()
ck("fork reset drops the shared _conn", budget._conn is None, repr(budget._conn))
ck("fork reset drops the thread-local ledger", getattr(budget._LEDGER_TL, "led", "x") is None)


# ── 2. a failed ledger write is durably captured (not just stderr) ───────────────────────────────────────────
class _Boom:
    def record_event(self, ev):
        raise RuntimeError("disk full")


budget._ledger = lambda: _Boom()
budget._record_spend_event("openai", "gpt-deadletter", "realtime", 0.5, intent="test:dl")
_dl = config.HOME / "spend_events_deadletter.jsonl"
ck("a failed ledger write is durably captured to the dead-letter file",
   _dl.exists() and "gpt-deadletter" in _dl.read_text())


# ── 3. backfill ingests an unpriced (None cost) row without crashing on round(None) ──────────────────────────
backfill._openai_rows = lambda: [("openai", "gpt-unpriced", None, 100, 50, "2026-08-01", "batch_none_test")]
backfill._anthropic_rows = lambda: []
crashed = False
try:
    added, total = backfill.backfill(providers=("openai",))
except TypeError:
    crashed, added, total = True, 0, 0.0
ck("backfill ingests a None-cost row without a round(None) TypeError", not crashed and added == 1, f"added={added}")
ck("...and an unknown cost adds nothing to the total (not called zero)", total == 0.0, f"total={total}")


# ── 4. trust: a server drift elevates the overall level ──────────────────────────────────────────────────────
trust.provider_truth = lambda since=None: {"total": 0.0}
trust._ledger_llm_total = lambda since: {"total": 0.0}
trust.verdict = lambda truth, recorded: ("ok", "ledger fine")
saas.crosscheck = lambda since=None: {"error": None, "in_sync": False, "value_drift": 3.0,
                                      "server_rows": 1, "server_only": 0, "local_only": 2}
r = trust.trust_report(since="2026-08-01", with_server=True)
ck("server drift (in_sync False) elevates the overall level to warn even when the ledger is ok",
   r.get("level") == "warn", str(r.get("level")))
saas.crosscheck = lambda since=None: {"error": None, "in_sync": True, "value_drift": 0.0,
                                      "server_rows": 1, "server_only": 0, "local_only": 0}
ck("an in-sync server keeps the overall level ok", trust.trust_report(since="2026-08-01", with_server=True).get("level") == "ok")


# ── 5. estimate-divergence: UNJUDGED is not a clean pass ─────────────────────────────────────────────────────
ed.enforce = lambda raise_on_fail=False: {"judged": [1], "ungrounded": [], "unjudged": [{"why": "judge failed"}]}
ck("estimate-divergence exits non-zero when everything is UNJUDGED (cannot-tell != clean)", ed.cmd([]) == 1)
ed.enforce = lambda raise_on_fail=False: {"judged": [1], "ungrounded": [], "unjudged": []}
ck("...and exits 0 only when all pairs are judged AND grounded", ed.cmd([]) == 0)

print(f"\n{'[FAIL]' if fails else 'OK'} test_spend_integrity_fixes: {fails} failure(s)")
sys.exit(1 if fails else 0)
