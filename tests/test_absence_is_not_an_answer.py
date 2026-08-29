"""Absence is UNKNOWN. It is never permission, never zero, and never silence.

THE INVARIANT THIS PROJECT IS BUILT ON, stated once and enforced here. A missing value is a question that
has not been answered, and every failure this repo has shipped in the last day was a place where something
answered it anyway:

    unpriced          !=  $0                    a model with no price recorded real spend as free
    truncated         !=  clean                 a capped response was measured as if it had finished
    context window    !=  output ceiling        a bound read off the wrong axis
    a validator silent !=  a validator dissenting   3 of 9 "splits" were one model returning nothing
    ownership absent  !=  ownership granted     <- below
    audit write failed !=  nothing to audit     <- below

The mirror case is just as bad and is the same mistake pointing the other way: a value that IS present and
happens to be zero, read as absence. In an accounting tool 0.0 is real and common — a $0 day, a
zero-confidence attribution, a free cached read — and `x or default` erases every one of them. Ten such
sites were confirmed by a four-vendor review of this repo; they land in this file as they are fixed.

WHY ONE FILE. These live in reconcile.py, budget.py, advise.py, brief.py, close.py and cachetest.py, and
nothing about their locations connects them. What connects them is the rule, so the rule is what the guard
is organised around — otherwise the next instance gets fixed in isolation and the class survives.
"""
import io
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-absence-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, reconcile                                    # noqa: E402

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── OWNERSHIP ABSENT IS NOT OWNERSHIP GRANTED ─────────────────────────────────────────────────────────
# owner_ok decides whether this host may absorb a shared provider account's unattributed gap into its own
# ledger. It used to read `if conn.get("enabled") and not conn.get("owns_account")`, gating ownership on
# whether SaaS sync happened to be switched on — two different things.
print("  ownership:")
check("standalone (no connection at all) still reconciles fully",
      reconcile.owner_ok({})[0] is True and reconcile.owner_ok(None)[0] is True)
check("a stated account OWNER reconciles",
      reconcile.owner_ok({"enabled": True, "owns_account": True})[0] is True)
check("a stated NON-owner does not",
      reconcile.owner_ok({"enabled": True, "owns_account": False})[0] is False)
check("a stated NON-owner does not, even with sync switched OFF",
      reconcile.owner_ok({"enabled": False, "owns_account": False})[0] is False,
      "the provider account is shared whether or not we are uploading; turning off the upload does not "
      "stop the local ledger absorbing other tenants' spend, and enabling it later propagates that")
check("a connection that does not STATE ownership is refused, not assumed to own",
      reconcile.owner_ok({"enabled": False})[0] is False
      and reconcile.owner_ok({"enabled": True})[0] is False,
      "absence is not permission — this is the one guard whose job is to stop us claiming other "
      "people's money")
_ok, why = reconcile.owner_ok({"enabled": False})
check("...and the refusal says which flag to set, so a wrong refusal is one message to fix",
      "owns_account" in why, why)


# ── A FAILED AUDIT WRITE IS NOT 'NOTHING TO AUDIT' ────────────────────────────────────────────────────
# quarantine_charge mutates the ledger. Its audit INSERT used to run outside the lock, after the mutation
# had already committed, wrapped in `except Exception: pass` — so a changed ledger with no record of the
# change was indistinguishable from an unchanged one, and nobody was told.
print("  audit trail:")
# spend_audit is created by SpendLedger's schema, NOT by budget._ledger_db(), so its existence depends on whether
# a ledger has ever been opened against this database. Opened explicitly here rather than assumed — the
# whole point below is that the two states (table present / absent) must behave differently and visibly.
from spendguard.ledger import SpendLedger                                   # noqa: E402
SpendLedger()

budget.record(provider="openai", model="gpt-5.5", kind="realtime", cost=1.23,
              project="absence-guard", conv_id="c-absence")
_se = budget._ledger().query(where={"project_primary": "absence-guard"})
row = _se[0]["id"] if _se else None                        # the spend_events id (the row quarantine targets)
check("seeded a charge to quarantine", row is not None)

if row:
    n = budget.quarantine_charge(row=row, reason="guard: audit must accompany the mutation")
    check("the charge was quarantined", n == 1, f"rowcount={n}")
    audited = budget._ledger_db().execute(
        "SELECT COUNT(*) FROM spend_audit WHERE actor='quarantine_charge' AND event_id=?",
        (str(row),)).fetchone()[0]
    check("the mutation left an audit row, written under the same lock and commit",
          audited == 1, f"audit rows={audited}")

    # AND WHEN THE AUDIT CANNOT BE WRITTEN, THE MUTATION DOES NOT HAPPEN EITHER. quarantine_charge voids via the
    # ATOMIC update() — the status change and its chained audit row commit together or not at all — so a failed
    # audit ROLLS BACK the void and RAISES rather than leaving a changed ledger with no record of the change.
    # Simulated by making the ledger's chained _audit raise (the way an unmigrated schema would), no schema surgery.
    budget.record(provider="openai", model="gpt-5.5", kind="realtime", cost=4.56,
                  project="absence-guard-2", conv_id="c-absence-2")
    _se2 = budget._ledger().query(where={"project_primary": "absence-guard-2"})
    row2 = _se2[0]["id"] if _se2 else None
    _led = budget._ledger()
    _orig_audit = _led._audit
    _led._audit = lambda *a, **k: (_ for _ in ()).throw(Exception("simulated: no such table: spend_audit"))
    raised = False
    try:
        budget.quarantine_charge(row=row2, reason="guard: audit failure must not pass silently")
    except Exception:
        raised = True
    finally:
        _led._audit = _orig_audit
    check("a failed audit write RAISES rather than mutating silently", raised)
    live = [r for r in budget._ledger().query(where={"project_primary": "absence-guard-2"}) if r["status"] != "void"]
    check("...and the void is ROLLED BACK — no changed ledger with no record of the change", len(live) == 1,
          f"{len(live)} live rows")

# ── AND THE MIRROR: A PRESENT ZERO IS NOT AN ABSENT VALUE ────────────────────────────────────────────
# `x or default` cannot tell 0.0 from None, and in an accounting tool 0.0 is real and common. Each check
# feeds a LEGITIMATE zero and asserts it survives — the behaviour, not the expression that produces it.
print("  present-but-zero:")
from spendguard import advise, close                                        # noqa: E402

# A labeller that recorded confidence 0.0 said something. `qconf or 0.7` promoted it to a normal label.
_real_rows = advise._rows
advise._rows = lambda as_of=None, intent=None: [
    ("openai", "m-zero-conf", "i", 1.0, 100, 100, "good", 0.0),
    ("openai", "m-no-conf", "i", 1.0, 100, 100, "good", None),
]
try:
    agg = advise.evidence()
    check("an explicit confidence of 0.0 weights 0.0, not the default",
          agg["openai:m-zero-conf"]["labeled"] == 0.0,
          f"weighted {agg['openai:m-zero-conf']['labeled']} — 'no confidence in this label' became a "
          "full-strength label")
    check("...while a confidence that was never recorded still gets the stated default",
          agg["openai:m-no-conf"]["labeled"] == advise._UNSTATED_CONFIDENCE,
          str(agg["openai:m-no-conf"]["labeled"]))
finally:
    advise._rows = _real_rows

# A $/M-output of exactly 0.00 is the BEST row in the table (served entirely from cache), and it rendered
# as '—', which reads as "could not be computed".
_real_rows = advise._rows
advise._rows = lambda as_of=None, intent=None: [("openai", "m-free", "i", 0.0, 100, 500_000, "good", 0.9)]
buf, real_stdout = io.StringIO(), sys.stdout
try:
    sys.stdout = buf
    advise.advise()
finally:
    sys.stdout, advise._rows = real_stdout, _real_rows
# ASSERT ON THE ROW, NOT ON THE PAGE. The first version of this check was `"$0.00" in output`, which
# passed whether or not the bug was present: the `$ total` column also renders $0.00 for a free model, so
# the assertion was satisfied by a different column than the one under test. A guard that cannot fail is
# the same defect as the code it guards — measured here by reverting the fix and watching it still pass.
#
# With cost 0.0 and a real output-token count, THREE cells compute to exactly 0.0 — $/M-out, $/good, and
# the total. Under the truthiness bug the first two render '—'; under the fix none of them do. So the
# discriminating question is whether the model's row contains a dash at all.
_row_line = next((ln for ln in buf.getvalue().splitlines() if "m-free" in ln), "")
check("a computed 0.00 prints as a number, not as '—'",
      bool(_row_line) and "—" not in _row_line,
      f"row rendered {_row_line!r} — the cheapest row in the table read as unmeasurable")

# A truth row whose usd cannot be added is EXCLUDED and NAMED. `usd or 0` would have dropped real money
# out of a month-end close in silence; leaving it raw raised mid-close.
from spendguard import truth as _truth                                      # noqa: E402
_real_truth_rows = _truth.rows
month = "2026-05"
_truth.rows = lambda since=None: [
    {"provider": "openai", "day": "2026-05-02", "usd": 10.0},
    {"provider": "openai", "day": "2026-05-03", "usd": 0.0},        # a real $0 day
    {"provider": "openai", "day": "2026-05-04", "usd": None},       # unusable
]
err = io.StringIO()
real_stderr, sys.stderr = sys.stderr, err
try:
    got = close.build_close(month)
finally:
    sys.stderr, _truth.rows = real_stderr, _real_truth_rows
check("a real $0.00 day is counted, and the unusable row is not",
      abs(got["total_usd"] - 10.0) < 1e-9, f"total={got.get('total_usd')}")
check("...and the excluded row is surfaced, not swallowed",
      len(got.get("unusable_rows") or []) == 1 and "WARN" in err.getvalue(),
      f"unusable={got.get('unusable_rows')} stderr={err.getvalue()[:80]!r}")

print(f"\n{'[FAIL]' if failures else 'OK'} test_absence_is_not_an_answer: {failures} failure(s)")
sys.exit(1 if failures else 0)
