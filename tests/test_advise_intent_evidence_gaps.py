"""Per-intent catalog evidence gaps surface without changing measured rankings. Offline and isolated."""
import contextlib
import io
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-intent-evidence-gap-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import advise, advisor, calls, mcp_server, model_catalog  # noqa: E402

TEST_INTENT = "synthetic-gap-intent"
TEST_INPUT_TOKENS = 10
TEST_OUTPUT_TOKENS = 20
TEST_RECORDED_COST = 0.0
TEST_QUALITY_CONFIDENCE = 1.0
TEST_REQUIRED_CATALOG_SIZE = 2
CATALOG_IDS = model_catalog.ids()
assert len(CATALOG_IDS) >= TEST_REQUIRED_CATALOG_SIZE
MEASURED_BARE_ID, UNMEASURED_BARE_ID = CATALOG_IDS[:2]
MEASURED_RECORD = model_catalog.model_record(MEASURED_BARE_ID)
UNMEASURED_RECORD = model_catalog.model_record(UNMEASURED_BARE_ID)
MEASURED_ID = f"{MEASURED_RECORD['provider']}:{MEASURED_BARE_ID}"
UNMEASURED_ID = f"{UNMEASURED_RECORD['provider']}:{UNMEASURED_BARE_ID}"

calls.insert(MEASURED_RECORD["provider"], MEASURED_BARE_ID, "realtime", TEST_RECORDED_COST,
             in_tok=TEST_INPUT_TOKENS, out_tok=TEST_OUTPUT_TOKENS, intent=TEST_INTENT,
             quality="good", quality_conf=TEST_QUALITY_CONFIDENCE)

before = advise.ranked(TEST_INTENT)
after = advise.ranked(TEST_INTENT)
assert before["models"] == after["models"]
assert [row["id"] for row in after["models"]] == [MEASURED_ID]
gaps = after["unmeasured_for_intent"]
gap = next(row for row in gaps if row["id"] == UNMEASURED_ID)
assert "EVIDENCE GAP" in gap["note"]
assert f"intent `{TEST_INTENT}`" in gap["note"]
assert "not absent from the catalog" in gap["note"]
assert TEST_INTENT in gap["remedy"] and UNMEASURED_ID in gap["remedy"]
assert all(row["id"] != MEASURED_ID for row in gaps)

mcp = mcp_server._tool_advise({"intent": TEST_INTENT})
assert mcp["unmeasured_for_intent"] == gaps
with contextlib.redirect_stdout(io.StringIO()) as rendered:
    advise.advise(intent=TEST_INTENT)
assert "unmeasured_for_intent" in rendered.getvalue() and UNMEASURED_ID in rendered.getvalue()

cold_intent = TEST_INTENT + "-cold"
cold = advisor.recommend_models(cold_intent, run=False)
assert cold["measured_ranking"] == [] and cold["unmeasured_for_intent"]
assert "EVIDENCE GAP" in cold["note"]
mcp_cold = mcp_server._tool_recommend({"intent": cold_intent})
assert mcp_cold["unmeasured_for_intent"] == cold["unmeasured_for_intent"]

original_recommend = advisor.recommend_models
advisor.recommend_models = lambda **kwargs: cold
try:
    with contextlib.redirect_stdout(io.StringIO()) as recommend_rendered:
        advisor.main(["recommend", "--intent", cold_intent])
finally:
    advisor.recommend_models = original_recommend
assert "unmeasured_for_intent" in recommend_rendered.getvalue()

print("OK test_advise_intent_evidence_gaps")
