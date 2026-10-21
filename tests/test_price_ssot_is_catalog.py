"""Offline guard: curated catalog prices win over every breadth price source."""
import math
import os
import sys
import tempfile
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-price-ssot-")
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
# A global spendguard startup hook may have preloaded a different checkout before these env vars existed.
# This offline guard must exercise this repository's modules and isolated configuration.
for name in list(sys.modules):
    if name == "spendguard" or name.startswith("spendguard."):
        del sys.modules[name]

from spendguard import model_catalog, pricing  # noqa: E402

fails = []


def ck(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(label)


priced = 0
for mid in model_catalog.all_records():
    row = model_catalog.model_price(mid)
    if not row:
        continue
    priced += 1
    try:
        cost = pricing.realtime_cost(mid, 1000, 1000)
        ck(f"catalog price: {mid}", isinstance(cost, float) and (cost > 0 or model_catalog.billing_exempt(mid) and cost == 0), cost)
    except Exception as exc:
        ck(f"catalog price: {mid}", False, str(exc))
ck("catalog contains priced models", priced > 0)

for mid in ("gpt-6.1-sol", "gpt-6-luna"):
    try:
        ck(f"formerly ambiguous: {mid}", pricing.realtime_cost(mid, 1000, 1000) > 0)
    except Exception as exc:
        ck(f"formerly ambiguous: {mid}", False, str(exc))

sample = "gpt-6.1-sol"
row = model_catalog.model_price(sample)
expected = (1000 * row["in_"] + 1000 * row["out"]) / 1_000_000
with patch.object(pricing, "_vendor_qualified", side_effect=AssertionError("catalog leaked into breadth")):
    ck("catalog authoritative", math.isclose(pricing.realtime_cost(sample, 1000, 1000), expected))
    for suffix in ("-20261013", "-2026-10-13", "-preview", "-latest", "-v12", "-preview-20261013"):
        ck(f"catalog variant {suffix}", pricing.price(sample + suffix) == pricing.price(sample))
    ck("provider prefix uses catalog", pricing.price("openai:" + sample) == pricing.price(sample))

records = dict(model_catalog.all_records())
records[sample] = dict(records[sample], aliases=["ssot-test-alias"])
with patch.object(model_catalog, "all_records", return_value=records):
    ck("catalog alias", pricing.price("ssot-test-alias") == pricing.price(sample))
with patch.object(pricing, "_vendor_qualified", return_value={"provider": "azure", "in_": 123}):
    ck("explicit different provider falls through", pricing.price(sample, provider="azure")["provider"] == "azure")

try:
    result = pricing.realtime_cost("totally-unknown-stub-xyz", 1000, 1000)
    ck("unknown raises PriceUnknownError", False, result)
except pricing.PriceUnknownError as exc:
    ck("unknown raises PriceUnknownError", True)
    ck("unknown error names SSOT", "model_catalog.json" in str(exc))
except Exception as exc:
    ck("unknown raises PriceUnknownError", False, str(exc))

print(f"\n{'[FAIL]' if fails else 'OK'} test_price_ssot_is_catalog: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
