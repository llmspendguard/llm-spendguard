"""There is ONE definition of "money spent in a period", and readers must not each invent their own.

WHY THIS GUARD EXISTS. `charges` holds rows that mean different things — real charges, pre-spend estimates,
provider-batch reconciliation, realtime backfills, quarantined impossibilities — and every reader used to
rebuild its own WHERE clause to exclude the ones that are not period spend. Each had to remember the whole
marker set. They did not. In ONE day:

  * $359.63 of phantom charges, written by READING batch history, blocked the daily cap
  * a $10,409.24 realtime backfill dated today blocked the monthly cap
  * relabelling that row's basis was silently undone by ledger_sync, its writer
  * `_MARKER_MODELS` sat defined-and-referenced-nowhere through all of it

Four incidents, one cause: no single answer to "what counts". Each time, a guard meant to protect spend
instead refused real work over money nobody spent — which is worse than no guard, because it teaches you to
switch it off.

A raw `SUM(cost) FROM charges` is not banned — quarantine listings and audits legitimately need every row —
but it must SAY it is deliberate. An undeclared one is how the marker set gets forgotten again.
"""
import pathlib
import re
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from spendguard import budget                                    # noqa: E402

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "spendguard"
MARKER = "RAW-CHARGES-OK"
failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


def test_countable_excludes_the_right_rows_on_spend_events():
    """The countable definition MOVED from the countable_charges VIEW to SpendLedger._COUNTABLE when charges was
    retired. Same rule, proven on the money-of-record BY CONSTRUCTION: each excluded row-type, added ALONE, must
    leave the countable total unchanged; a real charge and a pre-spend estimate must move it. (What each
    AGGREGATOR counts is test_ledger_marker_matrix's job; this is the single filter's own rule.)"""
    import os
    import tempfile
    from decimal import Decimal
    from spendguard import ledger as L
    led = L.SpendLedger(db_path=os.path.join(tempfile.mkdtemp(prefix="sg-1def-"), "t.db"))

    def rec(usd, **kw):
        ev = {"provider": "openai", "model": "gpt-5.5", "kind": "realtime", "usd": usd,
              "source": "t", "dedup_key": L.live_dedup_key(str(usd) + repr(sorted(kw.items())))}
        ev.update(kw)
        led.record(ev)

    rec("1.00", cost_basis="billed")                              # a real charge → counts
    rec("2.00", cost_basis="estimate")                            # a pre-spend estimate → still counts (binds the cap)
    check("a real charge + a pre-spend estimate ARE counted", Decimal(led.spent_dec()) == Decimal("3.00"),
          led.spent_dec())
    # each of these, added ALONE, must NOT move the countable total — the exclusion is verified per row, not by a
    # single magic sum that a silent logic error could still land on.
    for label, kw in (("meta", {"is_meta": 1}),
                      ("reconciliation marker", {"reconciled": 1, "recon_marker": "(provider-batch)"}),
                      ("quarantined-impossible (void)", {"status": "void"}),
                      ("already-counted restatement (reconstructed)", {"cost_basis": "reconstructed"})):
        before = Decimal(led.spent_dec())
        rec("99.00", **kw)
        check(f"a {label} row is EXCLUDED from the countable total", Decimal(led.spent_dec()) == before,
              f"{led.spent_dec()} != {before}")


def test_spent_since_uses_the_one_definition():
    import ast
    import inspect
    src = inspect.getsource(budget.spent_since)
    # spent_since is repointed onto the money-of-record (spend_events): it delegates to spent_dec, which applies
    # the SINGLE countable filter SpendLedger._COUNTABLE. The one-definition invariant is preserved, relocated
    # from the countable_charges view to _COUNTABLE — still no hand-built marker/exclusion list in the reader.
    check("spent_since delegates to spend_events' spent_dec (the one _COUNTABLE), not a hand-built list",
          "spent_dec" in src and "_MARKER_MODELS" not in src)
    # And that ONE definition is defined exactly once — counted from the PARSED module (AST assignments to the
    # name), not a substring scan that reformatting or a comment could fool.
    from spendguard import ledger as _L
    tree = ast.parse(inspect.getsource(_L))
    ndefs = sum(1 for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name) and t.id == "_COUNTABLE")
    check("_COUNTABLE (the single countable filter) is defined exactly once", ndefs == 1, f"{ndefs} defs")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'[FAIL]' if failures else 'OK'} test_one_definition_of_countable_spend: {failures} failure(s)")
    sys.exit(1 if failures else 0)
