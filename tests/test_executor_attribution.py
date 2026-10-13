"""EVERY recorded call carries a non-null executor, so a ledger query splits $0-lane vs cash-metered with no guess.

WHY THIS GUARD EXISTS. gate._record_rt recorded metered realtime calls with executor=None and treated NULL as an
IMPLICIT "metered API" marker. That is ambiguous (NULL could also mean "never stamped"), and it meant every
honestreview:* row — served metered in the incident window — had executor IS NULL, so cost>0 could not be split
into cash-metered vs a $0 subscription lane, and the escalation ladder could not tell what actually billed.

THE FIX: the metered realtime recorder (gate._record_rt) stamps an EXPLICIT executor='api'; subscription lanes keep
executor=<lane>. Legacy rows stay NULL (no backfill), so every reader that classifies metered-vs-lane treats BOTH
'api' and legacy NULL/'' as metered, and the per-lane rollups + the bandit's per-arm grouping must NOT treat 'api'
as a lane.

The recording test DRIVES gate._record_rt (not a seeded row), so it fails if the real path ever writes NULL again.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-executor-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# gate._record_rt writes a calls row only when recording is EXPLICITLY enabled (privacy fail-closed); turn it on
# in this isolated HOME so the recording test drives the real path instead of asserting against nothing.
os.environ["SPENDGUARD_CALLS"] = "1"

from spendguard import calls, gate    # noqa: E402


def check(results, label, ok, extra=""):
    """Append (ok) to the caller's OWN results list and print — no shared/module state is mutated."""
    results.append(ok)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def _seed_reader_rows():
    # Rows for the READER tests only (classification/rollup) — seeding is honest here: these tests assert how the
    # readers treat an 'api' row, a lane row, and a legacy NULL row, not how recording produces them.
    calls.record_call("anthropic", "claude-opus-4-8", "realtime", 0.02, in_tok=10, out_tok=200,
                      intent="exec-attr", executor="api")                       # metered, explicit
    calls.record_call("openai", "gpt-5", "subscription", 0.0, in_tok=10, out_tok=50, intent="exec-attr",
                      executor="codex")                                         # $0 subscription lane
    calls.record_call("gemini", "gemini-3.8-flash", "realtime", 0.03, in_tok=10, out_tok=20, intent="exec-attr",
                      executor=None)                                            # LEGACY pre-fix metered row


def test_record_rt_stamps_metered_executor_api():
    """Drive the ACTUAL metered recorder and prove it stamps executor='api' (fails if it reverts to NULL)."""
    r = []
    gate._record_rt("exec-probe-metered", {"max_tokens": 500}, 10, 100, cost=0.01, provider="openai")
    row = calls._calls_db().execute(
        "SELECT executor, kind FROM calls WHERE model='exec-probe-metered' ORDER BY rowid DESC LIMIT 1").fetchone()
    check(r, "gate._record_rt records a metered realtime row", row is not None, "no row written")
    check(r, "the metered row's executor is the explicit 'api' (not NULL)",
          bool(row) and row[0] == "api", f"executor={row[0] if row else None!r}")
    check(r, "and its kind is 'realtime'", bool(row) and row[1] == "realtime", f"kind={row[1] if row else None!r}")
    return all(r)


def test_every_new_row_has_a_non_null_executor():
    r = []
    db = calls._calls_db()
    n_null = db.execute("SELECT COUNT(*) FROM calls WHERE intent='exec-attr' AND executor IS NULL").fetchone()[0]
    check(r, "only the deliberately-seeded legacy row is NULL; every new-style row is stamped",
          n_null == 1, f"null rows={n_null} (expected the 1 legacy)")
    return all(r)


def test_split_metered_vs_lane_reconciles():
    r = []
    db = calls._calls_db()
    metered = db.execute("SELECT COUNT(*), COALESCE(SUM(cost),0) FROM calls WHERE intent='exec-attr' "
                         "AND COALESCE(NULLIF(executor,''),'') IN ('', 'api')").fetchone()
    lane = db.execute("SELECT COUNT(*), COALESCE(SUM(cost),0) FROM calls WHERE intent='exec-attr' "
                      "AND COALESCE(NULLIF(executor,''),'') NOT IN ('', 'api')").fetchone()
    total = db.execute("SELECT COUNT(*), COALESCE(SUM(cost),0) FROM calls WHERE intent='exec-attr'").fetchone()
    check(r, "metered split = 2 rows (1 api + 1 legacy null); lane split = 1 row (codex)",
          metered[0] == 2 and lane[0] == 1, f"metered={metered[0]} lane={lane[0]}")
    check(r, "costs reconcile: metered$ + lane$ == total$ (no row uncounted)",
          abs((metered[1] + lane[1]) - total[1]) < 1e-9, f"{metered[1]}+{lane[1]} vs {total[1]}")
    check(r, "lane spend is $0 (subscription); metered carries the cash",
          lane[1] == 0.0 and metered[1] > 0, f"lane${lane[1]} metered${metered[1]}")
    return all(r)


def test_mean_out_groups_metered_by_provider_not_api():
    r = []
    keys = {k[0] for k in calls.mean_out_by_executor_model("exec-attr")}
    check(r, "metered arms are per-provider, never collapsed into one 'api' bucket",
          "api" not in keys and "anthropic" in keys, f"arm keys={sorted(keys)}")
    check(r, "the subscription lane appears as its own lane arm", "codex" in keys, f"arm keys={sorted(keys)}")
    return all(r)


def test_per_lane_rollup_excludes_api():
    # Mirrors the fixed lane_economics / lane_balance WHERE clause: a per-lane executor rollup excludes 'api'.
    r = []
    lanes = [x[0] for x in calls._calls_db().execute(
        "SELECT executor FROM calls WHERE intent='exec-attr' AND executor IS NOT NULL "
        "AND executor NOT IN ('', 'api') GROUP BY executor").fetchall()]
    check(r, "per-lane rollup lists only real lanes (codex), never 'api'", lanes == ["codex"], f"lanes={lanes}")
    return all(r)


_seed_reader_rows()
_ok = True
for _fn in (test_record_rt_stamps_metered_executor_api,
            test_every_new_row_has_a_non_null_executor,
            test_split_metered_vs_lane_reconciles,
            test_mean_out_groups_metered_by_provider_not_api,
            test_per_lane_rollup_excludes_api):
    _ok = _fn() and _ok

print(f"\n{'OK' if _ok else '[FAIL]'} test_executor_attribution")
sys.exit(0 if _ok else 1)
