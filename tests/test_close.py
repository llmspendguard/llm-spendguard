"""Monthly close, client view (close.py) — month windowing, provider totals from truth rows, CSV,
and CLI wiring. Offline (truth.rows monkeypatched), zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-close-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402

from spendguard import close, truth

fails = []
record_script_check(fails, "month window normal", close.month_window("2026-06") == ("2026-06-01", "2026-07-01"))
record_script_check(fails, "month window december rolls the year", close.month_window("2026-12") == ("2026-12-01", "2027-01-01"))

truth.rows = lambda since=None: [
    {"day": "2026-06-01", "provider": "openai", "usd": 10.0},
    {"day": "2026-06-15", "provider": "openai", "usd": 5.0},
    {"day": "2026-06-20", "provider": "vastai", "usd": 2.5},
    {"day": "2026-07-01", "provider": "openai", "usd": 99.0},   # next month — must be excluded
]
s = close.build_close("2026-06")
record_script_check(fails, "providers aggregated within the month only",
   s["providers"][0] == {"provider": "openai", "usd": 15.0, "days": 2} and len(s["providers"]) == 2)
record_script_check(fails, "total is the month sum", abs(s["total_usd"] - 17.5) < 1e-9)
record_script_check(fails, "past month flagged not-current", s["current_month"] is False)

csv = close.to_csv(s)
record_script_check(fails, "csv carries total + per-provider rows", "total_usd,17.50" in csv and "openai,15.00,2" in csv and "vastai,2.50,1" in csv)

out = os.path.join(tempfile.mkdtemp(), "close.csv")
rc = close.main(["--month", "2026-06", "--csv", out])
record_script_check(fails, "CLI runs + writes csv", rc == 0 and os.path.exists(out) and "openai,15.00,2" in open(out).read())
record_script_check(fails, "CLI rejects a bad month", close.main(["--month", "junk"]) == 2)

from _script_checks import cli_dispatch_preserves_owner_call
record_script_check(fails, "CLI wired: `spendguard close`",
                    cli_dispatch_preserves_owner_call("close", "spendguard.close:main", "none"))

print(("[OK]" if not fails else "[FAIL]") + " close: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
