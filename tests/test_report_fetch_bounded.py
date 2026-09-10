"""The spend report's OpenAI batch pull must be BOUNDED so a scheduled run can never pin CPU for 25 minutes.

The runaway was `reconcile_openai.fetch_batches` = an unbounded `while True` that pulled the entire batch history
every run and, on a stuck `has_more`/non-advancing cursor, looped forever at ~94% CPU. Guarantees pinned here:
  • NON-ADVANCING CURSOR TERMINATES — a server that returns has_more=true with the same last id forever stops
    (the whole point: it must not hang);
  • SINCE BOUND STOPS EARLY — newest-first paging halts once a page's oldest batch predates `since`, so a
    one-month report does not page all history;
  • PAGE CAP TERMINATES — an always-advancing has_more still stops at max_pages (belt to the cursor's braces);
  • WINDOW SUMS ARE IDENTICAL — bounding avoids work, it does NOT change the answer: the in-window $ with a
    `since` bound equals the in-window $ from an unbounded pull;
  • THE WATCHDOG FIRES — report._install_watchdog(t) raises within the wall-clock budget (and no-ops when unset).
Offline: urllib.request.urlopen + config.ssl_context are stubbed — no network, no spend.
"""
import datetime
import json
import os
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-fetchbound-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import reconcile_openai as ro, report, config      # noqa: E402
from urllib.parse import urlparse, parse_qs                         # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_UTC = datetime.timezone.utc


def _ts(y, m, d):
    return int(datetime.datetime(y, m, d, tzinfo=_UTC).timestamp())


def _batch(bid, y, m, d, status="completed", model="gpt-5-nano", it=1000, ot=10):
    return {"id": bid, "created_at": _ts(y, m, d), "status": status,
            "usage": {"input_tokens": it, "output_tokens": ot}, "model": model}


class _Resp:
    """A minimal stand-in for urlopen's response: a context manager whose .read() feeds json.load()."""
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *a):
        return self._b


def _after_of(req):
    return (parse_qs(urlparse(req.full_url).query).get("after") or [None])[0]


class _Pager:
    """Serves a fixed list of newest-first pages by cursor: after=None → page 0; after=<last id of page i> → page i+1."""
    def __init__(self, pages):
        self.pages, self.calls = pages, 0
        self._next = {}
        for i, pg in enumerate(pages):
            if pg:
                self._next[pg[-1]["id"]] = i + 1

    def __call__(self, req, context=None, timeout=None):
        self.calls += 1
        after = _after_of(req)
        idx = 0 if not after else self._next.get(after, len(self.pages))
        pg = self.pages[idx] if idx < len(self.pages) else []
        return _Resp({"data": pg, "has_more": idx < len(self.pages) - 1})


class _Stuck:
    """Always returns has_more=true with the SAME last id — the non-advancing cursor that used to loop forever."""
    def __init__(self):
        self.calls = 0

    def __call__(self, req, context=None, timeout=None):
        self.calls += 1
        return _Resp({"data": [_batch("STUCK", 2026, 9, 5)], "has_more": True})


class _Advancing:
    """Always has_more=true with a fresh advancing id — the cursor never repeats, so only the page cap can stop it."""
    def __init__(self):
        self.calls = 0

    def __call__(self, req, context=None, timeout=None):
        self.calls += 1
        return _Resp({"data": [_batch(f"b{self.calls}", 2026, 9, 5)], "has_more": True})


config.ssl_context = lambda: None                      # keep the stub network-free
_warns = []
config.warn_once = lambda m: _warns.append(m)


# ── NON-ADVANCING CURSOR TERMINATES (the hang guard) ──
_warns.clear()
stuck = _Stuck()
ro.urllib.request.urlopen = stuck
rows = ro.fetch_batches("k")
ck("a non-advancing cursor TERMINATES (does not hang)", stuck.calls <= 3 and isinstance(rows, list))
ck("...and the stuck cursor is surfaced via warn_once", any("did not advance" in w for w in _warns))

# ── SINCE BOUND STOPS EARLY (newest-first) ──
pages = [
    [_batch("s5", 2026, 9, 5), _batch("s3", 2026, 9, 3)],     # page 0: all in-window (>= 2026-09-01)
    [_batch("s2", 2026, 9, 2), _batch("a30", 2026, 8, 30)],   # page 1: oldest (08-30) < since → STOP after this page
    [_batch("a20", 2026, 8, 20), _batch("a10", 2026, 8, 10)],  # page 2: must NEVER be fetched
]
pager = _Pager(pages)
ro.urllib.request.urlopen = pager
rows = ro.fetch_batches("k", since="2026-09-01")
ck("since-bound paging STOPS at the first page whose oldest batch predates the window", pager.calls == 2)
ck("...and the un-needed older page was never fetched", all(b["id"] not in ("a20", "a10") for b in rows))

# ── PAGE CAP TERMINATES even when the cursor keeps advancing ──
adv = _Advancing()
ro.urllib.request.urlopen = adv
_warns.clear()
rows = ro.fetch_batches("k", max_pages=3)
ck("an always-advancing has_more stops at the page cap", adv.calls == 3 and any("page cap" in w for w in _warns))

# ── WINDOW SUMS ARE IDENTICAL: bounding avoids work, it does not change the in-window answer ──
report.load_key = lambda: "k"
report.pricing.cost_or_unpriced = lambda *a, **k: 1.0    # each completed batch = $1 → sums are countable
full_pages = [
    [_batch("w5", 2026, 9, 5), _batch("w2", 2026, 9, 2)],     # in-window
    [_batch("o31", 2026, 8, 31), _batch("o15", 2026, 8, 15)],  # out-of-window (older)
]
ro.urllib.request.urlopen = _Pager(full_pages)
full, _ = report.openai_by_day(since=None)               # unbounded pull → sees all days
ro.urllib.request.urlopen = _Pager(full_pages)
bounded, _ = report.openai_by_day(since="2026-09-01")    # bounded pull → stops early
win = lambda bd: round(sum(v for d, v in bd.items() if d >= "2026-09-01"), 6)
ck("in-window $ is identical with and without the since bound", win(full) == win(bounded) and win(bounded) == 2.0)

# ── THE WATCHDOG FIRES within its wall-clock budget, and no-ops when unset ──
ck("watchdog is a no-op when max_seconds is unset", report._install_watchdog(None)() is None)
fired = False
cancel = report._install_watchdog(0.05)
try:
    time.sleep(0.4)                                      # SIGALRM interrupts the sleep and raises in-thread
except report._WatchdogFired:
    fired = True
finally:
    cancel()
ck("the --max-seconds watchdog raises _WatchdogFired within the budget", fired)

print(("[OK]" if not fails else "[FAIL]") + " report fetch bounded: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
