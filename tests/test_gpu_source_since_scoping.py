"""Design item (P9, caller-intent): GPUSource.truth_total/captured DROPPED the reconcile `since` window — the readers
(account_gpu_total / gpu_rows_by_day) then applied their own month-start default, so a reconcile for ANY period always
returned the WHOLE MONTH (truth AND captured), silently wrong for a non-month window. Both now convert the 'YYYY-MM-DD'
since to the epoch since_ts and pass it, so truth and captured cover the SAME requested window.

Offline + deterministic ($0): the vast readers are faked. Isolation: SPENDGUARD_HOME → mkdtemp before import.
"""
import os, sys, tempfile, datetime

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-gpu-since-")

from spendguard import resources   # noqa: E402


def main():
    fails = 0

    def ck(name, cond, extra=""):
        nonlocal fails
        if not cond:
            fails += 1
        print(f"  [{'OK' if cond else 'FAIL'}] {name}{('  — ' + str(extra)) if extra and not cond else ''}")

    # the converter: a 'YYYY-MM-DD' date string → UTC-midnight epoch; None → None (readers apply their month default)
    ck("_since_date_to_ts(None) → None", resources._since_date_to_ts(None) is None)
    exp = datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc).timestamp()
    ck("_since_date_to_ts('2026-06-01') → the UTC-midnight epoch", resources._since_date_to_ts("2026-06-01") == exp)

    seen = []
    _real_acct, _real_rows = resources.account_gpu_total, resources.gpu_rows_by_day
    resources.account_gpu_total = lambda since_ts=None: (seen.append(("truth", since_ts)), 12.5)[1]
    resources.gpu_rows_by_day = lambda since_ts=None, **k: (seen.append(("cap", since_ts)),
                                                            [{"cost": 3.0, "project": "lmm"}])[1]
    try:
        src = resources.GPUSource(conn={"owns_account": True})

        t = src.truth_total("2026-06-01")
        ck("truth_total(since) forwards the CONVERTED since_ts to account_gpu_total (was dropped → always month)",
           ("truth", exp) in seen, seen)
        ck("truth_total returns the account total", t == 12.5)

        cap = src.captured("2026-06-01")
        ck("captured(since) forwards the SAME converted since_ts (truth + captured cover one window)",
           ("cap", exp) in seen, seen)
        ck("captured returns the scoped rows", cap == [{"cost": 3.0, "project": "lmm"}])

        seen.clear()
        src.truth_total(None)
        ck("truth_total(None) → since_ts None (the reader applies its own month-start default), never a crash",
           seen == [("truth", None)], seen)
    finally:
        resources.account_gpu_total, resources.gpu_rows_by_day = _real_acct, _real_rows

    print(f"\n{'[FAIL]' if fails else '[OK]'} test_gpu_source_since_scoping: {fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
