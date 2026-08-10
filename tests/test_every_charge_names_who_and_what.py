"""A ledger that cannot say WHO spent it and WHAT it bought is a total, not an account.

THE STANDARD THIS FILE ENFORCES. For any dollar in the ledger a reader must be able to answer, from the
authoritative record alone and without reconstructing anything:

    which VENDOR was actually billed        provider   — from the registry, never inferred
    WHAT the money bought                   intent     — 'review:config.py', captured at the charge
    WHAT RAN IT                             actor      — 'repo_review_panel.py:fan_out:238'
    WHICH KEY served it                     key_fp
    WHAT KIND of number it is               basis      — estimate · billed · assumed · reconstructed
    and every later CORRECTION to any of the above, with its before and after.

WHY EACH OF THESE EXISTS, MEASURED
  provider   The gate resolved a charge's vendor with
                 "anthropic" if str(model).startswith("claude") else "openai"
             so every OpenAI-COMPATIBLE vendor landed on the OpenAI line: 697 rows and $30.97 of Moonshot
             (kimi-k3, $24.28) and z.ai (glm-5.2, $6.69) spend. Not a cosmetic label — `saas reconcile`
             compares the ledger to provider billing PER PROVIDER, so OpenAI was over-attributed by
             exactly what the other two were missing and the leak verdict was wrong in both directions.
             adapters.provider_for already answered every one of those correctly, and RAISES rather than
             guess. It simply was not on the path. That was the fourth thing in one day that existed and
             was not called where it was needed.

  intent     The money table held provider/model/cost/project; the PURPOSE lived only in `calls`, a
             separate table with no join key back to the charge. "What was this $23 for?" was
             unanswerable from the authoritative record.

  actor      Same gap for "what ran it".

UNKNOWN IS A VALID ANSWER AND "openai" IS NOT. A row that names no vendor can be found and fixed. A row
that names the WRONG vendor cannot even be seen — which is why this ran for months.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-whowhat-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import budget, calls, gate                                  # noqa: E402
from spendguard.ledger import SpendLedger                                   # noqa: E402

failures = 0


def check(label, cond, extra=""):
    global failures
    if not cond:
        failures += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


print("  the vendor is RESOLVED, never inferred:")
# The OpenAI-compatible vendors are the whole point: they are indistinguishable from OpenAI by request
# shape, and a name-prefix rule silently files all of them under OpenAI.
for model, want in (("kimi-k3", "moonshot"), ("glm-5.2", "zai"),
                    ("gpt-5.5", "openai"), ("claude-opus-4-8", "anthropic")):
    got = gate._provider_of(model)
    check(f"{model} bills to {want}", got == want, f"got {got!r}")

for unknown in ("a-model-nobody-registered", "", None):
    got = gate._provider_of(unknown)
    check(f"an unregistered model ({unknown!r}) is UNKNOWN, not openai",
          got == gate.UNKNOWN_PROVIDER,
          f"got {got!r} — a wrongly-named vendor is invisible; an unnamed one can be found and fixed")


# AND THE RECORD PATH MUST ACTUALLY CALL IT. The checks above exercise the resolver; they say nothing
# about whether the code that writes charges uses it. Measured: with the resolver correct and the call
# site reverted to the old prefix rule, every check above still passed — a guard that could not fail,
# which is the same defect as the code it guards. The only way to know is to record a charge and read the
# row back.
print("\n  ...and the WRITE PATH resolves it, not just the helper:")
# The cross-process ledger only records under the sqlite backend; with the default in-memory backend the
# gate writes no row at all and this check would pass vacuously on a None it never examined.
from spendguard import config as _config                                    # noqa: E402
_config.budget_backend = lambda: "sqlite"
_before = budget._db().execute("SELECT COALESCE(MAX(rowid),0) FROM charges").fetchone()[0]
gate._record_rt("kimi-k3", {}, in_tok=10, out_tok=10, cost=0.01)
_row = budget._db().execute(
    "SELECT provider, model FROM charges WHERE rowid > ? ORDER BY rowid DESC LIMIT 1", (_before,)).fetchone()
check("a charge written by the gate names the REAL vendor",
      _row is not None and _row[0] == "moonshot",
      f"row was {_row!r} — the resolver being right does not mean the writer calls it")


print("\n  every charge carries WHAT it bought and WHAT ran it:")
with calls.context(intent="review:some-file.py"):
    budget.record(provider="moonshot", model="kimi-k3", kind="realtime", cost=1.23)
row = budget._db().execute(
    "SELECT provider, model, intent, actor, key_fp, basis FROM charges ORDER BY rowid DESC LIMIT 1"
).fetchone()
check("the charge records the intent it was made under", row[2] == "review:some-file.py", str(row))
check("...and what ran it", bool(row[3]), f"actor was {row[3]!r}")
check("...and the vendor as given", row[0] == "moonshot", str(row))

# A charge made OUTSIDE any declared intent must be honest about that rather than borrowing a stale one.
budget.record(provider="openai", model="gpt-5.5", kind="realtime", cost=0.5)
row = budget._db().execute("SELECT intent FROM charges ORDER BY rowid DESC LIMIT 1").fetchone()
check("a charge with no declared intent records an empty one, not the previous flow's",
      row[0] == "", f"got {row[0]!r}")


print("\n  history can be repaired, and the repair is journalled:")
SpendLedger()                                    # spend_audit lives in the ledger schema
budget._db().execute("UPDATE charges SET provider='openai' WHERE model='kimi-k3'")
budget._db().commit()

dry = budget.reattribute_providers(apply=False)
check("dry by default: it reports without writing", dry["n"] >= 1 and dry["applied"] is False, str(dry["n"]))
still = budget._db().execute("SELECT provider FROM charges WHERE model='kimi-k3'").fetchone()[0]
check("...and really did not write", still == "openai", still)

res = budget.reattribute_providers(apply=True)
fixed = budget._db().execute("SELECT provider FROM charges WHERE model='kimi-k3'").fetchone()[0]
check("applying corrects the vendor", fixed == "moonshot", fixed)
aud = budget._db().execute(
    "SELECT field, old_value, new_value FROM spend_audit WHERE actor='reattribute_providers'").fetchall()
check("...and the correction is journalled with its before and after",
      any(a == ("provider", "openai", "moonshot") for a in aud), str(aud[:3]))
check("...and says WHY, not just what", bool(budget._db().execute(
    "SELECT reason FROM spend_audit WHERE actor='reattribute_providers' LIMIT 1").fetchone()[0]))

# A REPAIR MUST NOT DESTROY INFORMATION. Overwriting a recorded vendor with "unknown" is a downgrade
# dressed as a correction — only a positive identification that disagrees may change a row.
budget.record(provider="some-vendor-we-recorded", model="a-model-nobody-registered",
              kind="realtime", cost=0.25)
budget.reattribute_providers(apply=True)
kept = budget._db().execute(
    "SELECT provider FROM charges WHERE model='a-model-nobody-registered'").fetchone()[0]
check("a recorded vendor is never overwritten with 'unknown'", kept == "some-vendor-we-recorded", kept)

print(f"\n{'[FAIL]' if failures else 'OK'} test_every_charge_names_who_and_what: {failures} failure(s)")
sys.exit(1 if failures else 0)
