"""`spendguard lanes --bulk --hedge-ms N` forwards N to bulk_delegate(hedge_ms=N), so a --refuse-billed bulk
consumer (e.g. symgrep's index) can RESCUE a straggling per-unit miss onto another healthy $0 lane WITHIN the same
fan — instead of leaving that unit for the next incremental pass when one confined lane mid-run can't serve it (an
OAuth token that won't refresh). The hedge itself (spare-capacity gate, always-$0 no_fallback race) lives in
bulk_delegate/_hedged_attempt and is proven by test_lane_hedging; THIS guards the CLI plumbing: the flag parses,
forwards, defaults OFF (None → the config default), and a malformed value fails LOUD instead of silently arming.
Offline (bulk_delegate spied), zero spend."""
import contextlib
import io
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-hedgeflag-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import lane_balance, lanes as _lanes_cli

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_captured = {}


def _spy_bulk(tasks, intent, **kw):
    _captured["called"] = True
    _captured["hedge_ms"] = kw.get("hedge_ms")
    return [{"text": "ok", "lane": "codex", "use_name": "x", "model": "m", "billed": False, "error": None}
            for _ in tasks]


lane_balance.bulk_delegate = _spy_bulk
_tf = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
_tf.write("task one\ntask two\n")
_tf.close()


def _run_bulk(extra_argv):
    _captured.clear()
    os.environ.pop("SPENDGUARD_BULK_LANES", None)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        _lanes_cli.main(["--bulk", "hedge:test", "--file", _tf.name] + extra_argv)
    return _captured


# ── the flag forwards its integer to bulk_delegate(hedge_ms=…) ──
ck("--hedge-ms 8000 → bulk_delegate(hedge_ms=8000)", _run_bulk(["--hedge-ms", "8000"]).get("hedge_ms") == 8000)

# ── absent → None, so bulk_delegate falls to the config default (off) — behavior unchanged for every existing caller ──
ck("no --hedge-ms → hedge_ms=None (config default, unchanged behavior)", _run_bulk([]).get("hedge_ms") is None)

# ── an explicit 0 is valid and OFF (a caller pinning it off for this run even if a global config armed it) ──
ck("--hedge-ms 0 → hedge_ms=0 (explicit off, forwarded)", _run_bulk(["--hedge-ms", "0"]).get("hedge_ms") == 0)

# ── the value is NOT mistaken for the positional intent (opt-value exclusion) ──
c = _run_bulk(["--hedge-ms", "5000"])
ck("--hedge-ms value is excluded from the positional intent (fan still dispatched)", c.get("called") is True)

# ── a malformed value fails LOUD (never silently arms/leaves-off a mis-typed hedge) — bulk_delegate NOT reached ──
ck("--hedge-ms abc → error, bulk_delegate never called", _run_bulk(["--hedge-ms", "abc"]).get("called") is None)
ck("--hedge-ms -5 (negative) → error, bulk_delegate never called", _run_bulk(["--hedge-ms", "-5"]).get("called") is None)

print(("[OK]" if not fails else "[FAIL]") + " bulk --hedge-ms flag: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
