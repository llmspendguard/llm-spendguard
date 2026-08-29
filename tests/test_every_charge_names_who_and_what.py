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
_led = budget._ledger()


def _newest(where=None):
    """The most-recently-inserted spend_events row (the money-of-record; charges is retired). Ordered by the
    sqlite rowid — recorded_at has only second granularity, so two rows in one second would tie ambiguously."""
    w, args = "", []
    if where:
        w = " WHERE " + " AND ".join(f"{k}=?" for k in where)
        args = list(where.values())
    r = _led._conn.execute(f"SELECT id FROM spend_events{w} ORDER BY rowid DESC LIMIT 1", args).fetchone()
    return _led.get(r[0]) if r else None


gate._record_rt("kimi-k3", {}, in_tok=10, out_tok=10, cost=0.01)
_row = _newest(where={"model": "kimi-k3"})
check("a charge written by the gate names the REAL vendor",
      _row is not None and _row["provider"] == "moonshot",
      f"row was {_row!r} — the resolver being right does not mean the writer calls it")


print("\n  every charge carries WHAT it bought and WHAT ran it:")
with calls.context(intent="review:some-file.py"):
    budget.record_charge(provider="moonshot", model="kimi-k3", kind="realtime", cost=1.23)
row = _newest()
check("the charge records the intent it was made under", row["intent"] == "review:some-file.py", str(row))
check("...and what ran it", bool(row["actor"]), f"actor was {row['actor']!r}")
check("...and the vendor as given", row["provider"] == "moonshot", str(row))

# A charge made OUTSIDE any declared intent must be honest about that rather than borrowing a stale one.
budget.record_charge(provider="openai", model="gpt-5.5", kind="realtime", cost=0.5)
check("a charge with no declared intent records an empty one, not the previous flow's",
      _newest()["intent"] == "", f"got {_newest()['intent']!r}")


print("\n  history can be repaired, and the repair is journalled:")
_led._conn.execute("UPDATE spend_events SET provider='openai' WHERE model='kimi-k3'")   # seed a mislabelled vendor
_led._conn.commit()


def _provider_of(model):
    return _newest(where={"model": model})["provider"]

dry = budget.reattribute_providers(apply=False)
check("dry by default: it reports without writing", dry["n"] >= 1 and dry["applied"] is False, str(dry["n"]))
check("...and really did not write", _provider_of("kimi-k3") == "openai", _provider_of("kimi-k3"))

res = budget.reattribute_providers(apply=True)
check("applying corrects the vendor", _provider_of("kimi-k3") == "moonshot", _provider_of("kimi-k3"))
aud = budget._ledger_db().execute(
    "SELECT field, old_value, new_value FROM spend_audit WHERE actor='reattribute_providers'").fetchall()
check("...and the correction is journalled with its before and after",
      any(a == ("provider", "openai", "moonshot") for a in aud), str(aud[:3]))
check("...and says WHY, not just what", bool(budget._ledger_db().execute(
    "SELECT reason FROM spend_audit WHERE actor='reattribute_providers' LIMIT 1").fetchone()[0]))

# A REPAIR MUST NOT DESTROY INFORMATION. Overwriting a recorded vendor with "unknown" is a downgrade
# dressed as a correction — only a positive identification that disagrees may change a row.
budget.record_charge(provider="some-vendor-we-recorded", model="a-model-nobody-registered",
              kind="realtime", cost=0.25)
budget.reattribute_providers(apply=True)
check("a recorded vendor is never overwritten with 'unknown'",
      _provider_of("a-model-nobody-registered") == "some-vendor-we-recorded",
      _provider_of("a-model-nobody-registered"))


# ── AND THE KEY FINGERPRINT MUST BELONG TO THE VENDOR ON THE ROW ─────────────────────────────────────
# Re-attributing the vendor fixed `provider` and left `key_fp`, so 688 rows read "moonshot spend, served
# by an OpenAI key" — a self-contradicting record, which in a forensic table is its own kind of wrong.
print("\n  the key fingerprint belongs to the vendor on the row:")
budget.record_charge(provider="moonshot", model="kimi-k3", kind="realtime", cost=2.0)
_eid = _newest(where={"model": "kimi-k3"})["id"]           # the spend_events id of the row we stamp
_openai_fp = "aaaaaaaa:bbbb"
_led._conn.execute("UPDATE spend_events SET key_fp=? WHERE id=?", (_openai_fp, _eid))
_led._conn.commit()

import spendguard.config as _c2                                             # noqa: E402
_real_fp = _c2.key_fingerprint
_c2.key_fingerprint = lambda p: (_openai_fp if p == "openai" else "")
try:
    _d = budget.reattribute_providers(apply=False)
    check("a fingerprint belonging to another vendor is DETECTED", _d["stale_key_fp"] >= 1, str(_d["stale_key_fp"]))
    budget.reattribute_providers(apply=True)
finally:
    _c2.key_fingerprint = _real_fp
_fp_now = _led.get(_eid)["key_fp"]
check("...and cleared to UNKNOWN, not rewritten with today's key",
      _fp_now == "", f"got {_fp_now!r} — today's key did not serve a call made before it existed")

# The env var for each vendor's key comes from the provider registry, which already knew moonshot and z.ai
# while this module's own three-entry map did not — so their charges were stamped with an empty fingerprint.
check("the key env is resolved from the provider registry, not a short local copy",
      _c2._provider_key_env("moonshot") == "MOONSHOT_API_KEY"
      and _c2._provider_key_env("zai") == "ZAI_API_KEY",
      f"{_c2._provider_key_env('moonshot')} / {_c2._provider_key_env('zai')}")
print(f"\n{'[FAIL]' if failures else 'OK'} test_every_charge_names_who_and_what: {failures} failure(s)")
sys.exit(1 if failures else 0)
