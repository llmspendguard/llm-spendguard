"""Auto-fresh Learnings (#49, review.auto_fresh) — cadence-gated caged refresh: runs only when due per
advisor.auto_fresh (off|weekly|daily), records state, never raises into the report. Offline (review()
mocked — the real one spends caged meta-$), zero spend. Also: close --account prints the account axis."""
import os, sys, tempfile, json, time

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-fresh-")
    os.execv(sys.executable, [sys.executable] + sys.argv)
os.environ.pop("SPENDGUARD_AUTO_FRESH", None)

from spendguard import review, config

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

calls = []
review.review = lambda run=False, top=10: calls.append((run, top))

os.environ["SPENDGUARD_AUTO_FRESH"] = "off"
ck("off → skipped, review never called", "skipped" in review.auto_fresh() and not calls)

os.environ["SPENDGUARD_AUTO_FRESH"] = "weekly"
r1 = review.auto_fresh(now=1_000_000)
ck("weekly + never-ran → runs caged review(run=True, top=3)", r1.get("ran") and calls == [(True, 3)])
state = json.loads((config.HOME / "review_state.json").read_text())
ck("state recorded", abs(state["last_fresh"] - 1_000_000) < 1)
r2 = review.auto_fresh(now=1_000_000 + 3 * 86400)
ck("3 days later on weekly → not due", "skipped" in r2 and len(calls) == 1)
r3 = review.auto_fresh(now=1_000_000 + 8 * 86400)
ck("8 days later → due again", r3.get("ran") and len(calls) == 2)

def _boom(run=False, top=10):
    raise RuntimeError("synthesis exploded")
review.review = _boom
(config.HOME / "review_state.json").write_text("{}")
r4 = review.auto_fresh(now=time.time())
ck("a synthesis failure returns {error}, never raises (report-safe)", "error" in r4)
del os.environ["SPENDGUARD_AUTO_FRESH"]

# ── close --account: the account-axis section prints ──
from spendguard import close, truth, ledger_sync
truth.rows = lambda since=None: [{"day": "2026-06-01", "provider": "openai", "usd": 10.0}]
ledger_sync.leak_line = lambda since: "accounted $9.50 vs provider $10.00 — leak $0.50"

# ── run-rate forecast: open month, 6 observed days at $10/day → p50 = MTD + remaining×$10 ──
import datetime
_today = datetime.date.today()
_days = [(_today.replace(day=1) + datetime.timedelta(days=i)).isoformat() for i in range(6)]
truth.rows = lambda since=None: [{"day": d, "provider": "openai", "usd": 10.0} for d in _days]
fs = close.build(_today.strftime("%Y-%m"))
_last = (datetime.date(_today.year + (_today.month == 12), _today.month % 12 + 1, 1)
         - datetime.timedelta(days=1)).day
ck("forecast present for the open month with ≥5 observed days", "forecast" in fs)
ck("run-rate math: MTD + remaining × daily median ($10/day flat)",
   abs(fs["forecast"]["p50_usd"] - (60.0 + (_last - _today.day) * 10.0)) < 1e-6
   and fs["forecast"]["p50_usd"] == fs["forecast"]["p90_usd"])   # flat series → p50 == p90
fs2 = close.build((_today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m"))
ck("no forecast for a CLOSED month (nothing to extrapolate)", "forecast" not in fs2)
truth.rows = lambda since=None: [{"day": _days[0], "provider": "openai", "usd": 10.0}]
ck("<5 observed days → no forecast (honest, not noisy)", "forecast" not in close.build(_today.strftime("%Y-%m")))
truth.rows = lambda since=None: [{"day": "2026-06-01", "provider": "openai", "usd": 10.0}]
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = close.main(["--month", "2026-06", "--account"])
out = buf.getvalue()
ck("close --account runs", rc == 0)
ck("account axis printed with the shared-account caveat", "account axis" in out and "sibling orgs" in out)
ck("machine accounted-vs-provider line included", "leak $0.50" in out)

print(("[OK]" if not fails else "[FAIL]") + " auto-fresh + account view: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
