"""Cache token columns flow through budget.input_cache_split into the human receipt, including honest no-data."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cache-receipt-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import budget, receipt  # noqa: E402

budget._record_spend_event("anthropic", "claude-test", "realtime", 0.01, project="cache-axis-test",
                           in_tok=500, cache_read_tok=300, cache_write_tok=200, source="test")
split = budget.input_cache_split(project="cache-axis-test")
line = receipt._cache_split_line({"input_cache": split})

assert split["input_tok"] == 1000, split
assert split["cache_read_share"] == 0.3, split
assert split["cache_write_share"] == 0.2, split
assert "read 300 (30.0%)" in line, line
assert "write 200 (20.0%)" in line, line
assert "total input 1.0K" in line, line
assert receipt._cache_split_line({"input_cache": {"input_tok": 900}}).endswith(
    "no measured token data for this scope")

print("[OK] receipt cache axes: persisted split renders read/write shares and honest no-data")
