"""Offline tests for reconcile_openai PARSERS — day() date extraction + load_key().
NO network: fetch_batches/main are never called. Maximizes line coverage of the pure bits.
"""
import os, sys, tempfile
if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import reconcile_openai as ro, pricing

failures = 0
def check(label, cond):
    global failures
    ok = bool(cond)
    if not ok: failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")


print("-- day(b): epoch created_at -> UTC YYYY-MM-DD --")
# 1700000000 == 2023-11-14T22:13:20Z
check("epoch -> UTC date", ro.day({"created_at": 1700000000}) == "2023-11-14")
# day boundary: 1718668799 == 2024-06-17T23:59:59Z (stays on the 17th in UTC)
check("late-day epoch stays UTC day", ro.day({"created_at": 1718668799}) == "2024-06-17")
check("epoch 0 -> 1970-01-01", ro.day({"created_at": 0}) == "1970-01-01")

print("-- day() feeds the OpenAI batch object shape the code reads --")
b = {"id": "batch_x", "status": "completed", "created_at": 1700000000,
     "model": "gpt-5.5", "request_counts": {"total": 10},
     "usage": {"input_tokens": 1000, "output_tokens": 500,
               "input_tokens_details": {"cached_tokens": 0}}}
check("batch dict day", ro.day(b) == "2023-11-14")
# the cost path main() uses, exercised purely (no network):
u = b["usage"]
c = pricing.batch_cost(b["model"], u["input_tokens"], u["output_tokens"],
                       u["input_tokens_details"]["cached_tokens"])
check("batch_cost on that usage > 0", c > 0)
check("matches canonical batch price", abs(c - pricing.batch_cost("gpt-5.5", 1000, 500)) < 1e-12)

print("-- load_key(): resolves from env (no network, no real ~/.spendguard) --")
os.environ["OPENAI_API_KEY"] = "sk-test-OFFLINE-123"
check("load_key returns env key", ro.load_key() == "sk-test-OFFLINE-123")

print("-- load_key(): missing key -> RAISES (not sys.exit), never a network call --")
# Stub config.api_key to '' (a repo-local ./.env may hold a real key; never depend on its
# absence and never read it). load_key imports api_key from .config at call time. It must RAISE a normal
# Exception (KeyMissing), NOT sys.exit — SystemExit is a BaseException that escapes the `except Exception`
# guards in degradable callers (leak_line / `spendguard doctor`) and aborts them.
import spendguard.config as _cfg
_orig_api_key = _cfg.api_key
_cfg.api_key = lambda name: ""
try:
    ro.load_key()
    check("missing key raises", False)
except SystemExit:
    check("missing key must NOT sys.exit (it would escape except-Exception guards)", False)
except Exception as e:
    check("missing key raises KeyMissing with a clear message",
          isinstance(e, ro.KeyMissing) and "OPENAI_API_KEY not found" in str(e))
finally:
    _cfg.api_key = _orig_api_key

print("-- main(): smoke run, fully stubbed (no network), exercises aggregation+print branches --")
# canned batch rows in the OpenAI Batch object shape main() reads — one of each status path:
# completed (billed), cancelled (waste), in_progress (pending count), failed (skipped),
# completed-with-no-usage (skipped).
_ROWS = [
    {"id": "b1", "status": "completed", "created_at": 1700000000, "model": "gpt-5.5",
     "request_counts": {"total": 5},
     "usage": {"input_tokens": 10_000, "output_tokens": 2_000,
               "input_tokens_details": {"cached_tokens": 1_000}}},
    {"id": "b2", "status": "cancelled", "created_at": 1700100000, "model": "gpt-5.5",
     "request_counts": {"total": 3},
     "usage": {"input_tokens": 5_000, "output_tokens": 0,
               "input_tokens_details": {"cached_tokens": 0}}},
    {"id": "b3", "status": "in_progress", "created_at": 1700200000, "model": "gpt-5.5",
     "request_counts": {"total": 42}, "usage": {}},
    {"id": "b4", "status": "failed", "created_at": 1700300000, "model": "gpt-5.5",
     "request_counts": {"total": 1}, "usage": {}},
    {"id": "b5", "status": "completed", "created_at": 1700400000, "model": "gpt-5.5",
     "request_counts": {"total": 1},
     "usage": {"input_tokens": 0, "output_tokens": 0,
               "input_tokens_details": {"cached_tokens": 0}}},
]
ro.fetch_batches = lambda key: _ROWS      # no network
ro.load_key = lambda: "sk-test-OFFLINE"
_argv = sys.argv
try:
    sys.argv = ["prog", "--by-day", "--since", "2023-11-14", "--estimate", "0.10"]
    ro.main()
    check("main() ran with all flags", True)
    sys.argv = ["prog"]
    ro.main()
    check("main() ran with no args", True)
except SystemExit:
    check("main() did not hard-exit", False)
except Exception as e:
    check(f"main() raised: {e}", False)
finally:
    sys.argv = _argv

print(f"\n{'[FAIL]' if failures else 'OK'} test_reconcile_openai: {failures} failure(s)")
sys.exit(1 if failures else 0)
