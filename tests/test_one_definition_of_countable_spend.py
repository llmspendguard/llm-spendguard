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


def test_the_view_excludes_every_marker():
    """Built from the constants, so adding a marker cannot leave the view behind."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE charges (ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, cost REAL, "
              "project TEXT, conv_id TEXT, key_fp TEXT, basis TEXT)")
    budget._create_countable_view(c)
    rows = [("t", "2026-01-01", "openai", "gpt-5.5", "realtime", 1.0, "", "", "", "billed")]
    for m in budget._MARKER_MODELS:
        rows.append(("t", "2026-01-01", "openai", m, "realtime", 500.0, "", "", "", "billed"))
    rows.append(("t", "2026-01-01", "openai", "gpt-5.5", "realtime", 500.0, "", budget.QUARANTINE_CONV, "",
                 "estimate"))
    rows.append(("t", "2026-01-01", "openai", "gpt-5.5", "realtime", 500.0, "", "", "",
                 budget.BASIS_RECONSTRUCTED))
    rows.append(("t", "2026-01-01", "openai", "gpt-5.5", "meta", 500.0, "", "", "", "billed"))
    c.executemany("INSERT INTO charges VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    total = c.execute(f"SELECT COALESCE(SUM(cost),0) FROM {budget.COUNTABLE_VIEW}").fetchone()[0]
    check("only the one real charge counts; every marker, quarantine, backfill and meta row is out",
          total == 1.0, f"${total}")


def test_an_estimate_still_counts():
    """An estimate MUST bind a pre-spend cap — that is the entire point of estimating. Excluding it would
    make the guard useless in the only direction where it can still prevent a loss."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE charges (ts TEXT, day TEXT, provider TEXT, model TEXT, kind TEXT, cost REAL, "
              "project TEXT, conv_id TEXT, key_fp TEXT, basis TEXT)")
    budget._create_countable_view(c)
    c.execute("INSERT INTO charges VALUES ('t','2026-01-01','openai','gpt-5.5','realtime',7.0,'','','','estimate')")
    total = c.execute(f"SELECT COALESCE(SUM(cost),0) FROM {budget.COUNTABLE_VIEW}").fetchone()[0]
    check("a pre-spend estimate is still countable", total == 7.0, f"${total}")


def test_no_module_sums_raw_charges_without_declaring_it():
    """The anti-amnesia rule. A raw sum is allowed where it is genuinely wanted, but it must say so —
    otherwise the next one silently forgets a marker, which is exactly how this went wrong four times."""
    offenders = []
    pat = re.compile(r"SUM\(cost\)", re.I)
    for f in sorted(SRC.glob("*.py")):
        lines = f.read_text().splitlines()
        # Scope to the ENCLOSING FUNCTION, not a fixed line window: a declaration belongs to the function it
        # describes, and a query can sit twenty lines below the def that justifies it.
        for i, line in enumerate(lines):
            if not pat.search(line):
                continue
            start = next((j for j in range(i, -1, -1) if lines[j].startswith("def ")), 0)
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("def ")), len(lines))
            body = "\n".join(lines[max(0, start - 2):end])
            if "charges" not in body:
                continue                                  # summing some other table (calls, gate_calls)
            if budget.COUNTABLE_VIEW in body or "COUNTABLE_VIEW" in body or MARKER in body:
                continue
            offenders.append(f"{f.name}:{i + 1}")
    check(f"every raw charges sum either uses {budget.COUNTABLE_VIEW} or declares {MARKER}",
          not offenders, f"{len(offenders)} undeclared: {offenders[:6]}")


def test_spent_since_uses_the_view():
    import inspect
    src = inspect.getsource(budget.spent_since)
    # The source interpolates the NAME, not the value — checking for the value here found nothing and
    # reported a defect that did not exist.
    check("spent_since reads the view, not a hand-built exclusion list",
          "COUNTABLE_VIEW" in src and "_MARKER_MODELS" not in src)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\n{'[FAIL]' if failures else 'OK'} test_one_definition_of_countable_spend: {failures} failure(s)")
    sys.exit(1 if failures else 0)
