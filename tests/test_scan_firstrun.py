"""`spendguard scan` — the zero-install first run (`uvx --from llm-spendguard spendguard scan`).

The old front door was pip install → install-hook → doctor → edit your script → run under the gated interpreter:
four steps and a venv mutation before any number appeared. And `report` — the obvious first command — does a live
provider-billing pull measured at OVER THREE MINUTES on a keyless install. A first impression that hangs is worse
than none.

Invariants pinned here (they are the promises printed on the command's first line):
  • NO network, NO LLM call, NO key required, and no writes outside SPENDGUARD_HOME;
  • the TWO AXES stay separate — est plan value is never summed into billed $, and the output says so;
  • empty machine → a helpful message, never a crash or a fake $0 headline;
  • it presents the EXISTING readers rather than re-implementing transcript parsing.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-scan-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import scan

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


FAKE = {
    "Claude Code": {"sessions": 12, "days": ["2026-07-01", "2026-07-09"], "total_usd": 1500.0,
                    "projects": {"lmm": 1000.0, "manga2anime": 500.0}, "models": {"claude-opus-4-8": 1500.0},
                    "error": None},
    "Codex": {"sessions": 0, "days": [], "total_usd": 0.0, "projects": {}, "models": {}, "error": None},
}

print("-- the two axes stay separate, and the output SAYS the value isn't billed --")
out = scan.render(FAKE)
check("est plan value is labelled as such", "EST PLAN VALUE" in out)
check("it states plainly this is NOT money billed", "NOT money billed" in out)
check("billed $ is shown as its own $0.00 line, not merged", "billed $ (plan-covered)" in out and "$0.00" in out)
check("totals the value axis correctly", "$1,500.00" in out)
check("per-project breakdown present", "lmm" in out and "manga2anime" in out)
check("a source with no data is reported honestly", "no transcripts found" in out)
check("no key/network is claimed anywhere in the header",
      "No keys, no network, nothing leaves this machine" in out)
check("it points at the NEXT step (gate a metered run)", "spendguard run --" in out)

print("-- an empty machine gets guidance, never a crash or a fake headline --")
empty = scan.render({"Claude Code": {"sessions": 0, "days": [], "total_usd": 0.0, "projects": {}, "models": {},
                                     "error": None},
                     "Codex": {"sessions": 0, "days": [], "total_usd": 0.0, "projects": {}, "models": {}, "error": None}})
check("says there's nothing to scan", "Nothing to scan yet" in empty)
check("no invented TOTAL on an empty machine", "TOTAL est value" not in empty)
check("offers the API path instead", "reconcile all" in empty)

print("-- an unreadable source degrades to a note, never an exception --")
broken = scan.render({"Claude Code": {"sessions": 0, "days": [], "total_usd": 0.0, "projects": {}, "models": {},
                                      "error": "permission denied"},
                      "Codex": FAKE["Codex"]})
check("the error is surfaced inline", "unreadable" in broken and "permission denied" in broken)

print("-- the '… N more' rollup accounts for every project (no money vanishes) --")
many = {"Claude Code": {"sessions": 1, "days": ["2026-07-01"], "total_usd": 55.0, "models": {},
                        "projects": {f"p{i}": float(i) for i in range(1, 11)}, "error": None},
        "Codex": FAKE["Codex"]}
o = scan.render(many)
check("overflow row present for >8 projects", "more" in o)
check("total still equals the sum of ALL projects ($55)", "$55.00" in o)

print("-- collect(): reads the EXISTING transcript readers, no re-implementation --")
import inspect
csrc = inspect.getsource(scan.collect)
check("delegates to claudecode/codex update()", "claudecode" in csrc and "codex" in csrc and "update()" in csrc)
check("no direct transcript globbing in scan (that lives in the readers)",
      "glob" not in inspect.getsource(scan) and "*.jsonl" not in inspect.getsource(scan))
check("no network primitives anywhere in the module",
      not any(w in inspect.getsource(scan) for w in ("urllib", "requests", "http", "socket")))
check("no LLM/adapters call (attribution here is the transcript's own project, not the classifier)",
      "adapters" not in inspect.getsource(scan) and "advisor" not in inspect.getsource(scan))

print("-- it runs on a cold, empty home without a key and exits 0 --")
for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)
rc = scan.main([])
check("exit 0 with no keys and no config", rc == 0)
check("--days validates its argument", scan.main(["--days", "abc"]) == 2)

print("-- wired into the CLI as a first-class command --")
from spendguard import cli
check("`spendguard scan` dispatches", 'cmd == "scan"' in inspect.getsource(cli._dispatch))

print(f"\n{'[FAIL]' if failures else 'OK'} test_scan_firstrun: {failures} failure(s)")
sys.exit(1 if failures else 0)
