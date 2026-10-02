"""calls.mean_out_by_executor_model — the measured OUTPUT norm the value judge prices realized cost on — must EXCLUDE
`suspect` rows (a per-call out_tok that exceeds the model's ceiling: an aggregate/backfill/bug value, physically
impossible for one call). Leaving them in lets a single 2M-tok artifact dominate the mean and makes the value judge
price an arm as hugely (falsely) expensive. This guards the fix (WHERE … AND suspect IS NULL), matching the exclusion
reconcile_calls already applies.

Offline, isolated SPENDGUARD_HOME, zero spend (direct ledger inserts; no LLM)."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-suspect-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import calls  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


INTENT = "suspect-exclusion-intent"
# Two rows for the SAME arm (openai:gpt-5-nano): one CLEAN (200 out_tok), one SUSPECT (2,000,000 out_tok — impossible).
with calls._lock:
    db = calls._calls_db()
    db.execute("INSERT INTO calls(id,intent,provider,model,out_tok,suspect) VALUES(?,?,?,?,?,?)",
               ("row-clean", INTENT, "openai", "gpt-5-nano", 200, None))
    db.execute("INSERT INTO calls(id,intent,provider,model,out_tok,suspect) VALUES(?,?,?,?,?,?)",
               ("row-suspect", INTENT, "openai", "gpt-5-nano", 2_000_000,
                "out_tok 2000000 > ceiling (impossible per-call: aggregate/backfill/bug)"))
    db.commit()

norms = calls.mean_out_by_executor_model(INTENT)
arm = norms.get(("openai", "gpt-5-nano"), {})
ck("the arm is present (the clean row is counted)", bool(arm), extra=repr(norms))
ck("mean_out reflects ONLY the clean row (200), not the 2M-tok suspect artifact",
   abs(arm.get("mean_out", 0) - 200.0) < 1e-9, extra=f"mean_out={arm.get('mean_out')}")
ck("n counts ONLY the clean row (the suspect row is excluded, not just down-weighted)", arm.get("n") == 1,
   extra=f"n={arm.get('n')}")

print(f"\n{'[FAIL]' if fails else 'OK'} test_mean_out_excludes_suspect: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
