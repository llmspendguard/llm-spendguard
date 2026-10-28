"""Phase 3: lossless compressed content, legacy reads, indexed lookup, bounded backfill."""
import json
import os
import sqlite3
import sys
import tempfile


if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-content-compression-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check
from spendguard import callio, config, content_retention, conv, learn
from spendguard.content_compression import COMPRESSED_TEXT_PREFIX
from spendguard.content_compression_backfill import compress_retained_content_database


failures = []
repeated_text = "whole-evidence:" + ("漢字💡" * 2000)

content_retention.record_governed_call_content(
    "lookup-call", repeated_text, repeated_text, {"output": repeated_text})
retention_connection = content_retention._content_retention_db()
retained = retention_connection.execute(
    "SELECT prompt_json,system_json,response_json FROM call_content WHERE call_id=?",
    ("lookup-call",),
).fetchone()
record_script_check(
    failures,
    "call_content compressed write transparently round-trips every byte",
    json.loads(retained[0]) == repeated_text
    and json.loads(retained[1]) == repeated_text
    and json.loads(retained[2]) == {"output": repeated_text},
)
query_plan = retention_connection.execute(
    "EXPLAIN QUERY PLAN SELECT prompt_json FROM call_content WHERE call_id=?",
    ("lookup-call",),
).fetchall()
record_script_check(
    failures,
    "call_content natural-key lookup uses an index SEARCH",
    any("SEARCH" in row[3] and "call_id" in row[3] for row in query_plan)
    and not any("SCAN call_content" in row[3] for row in query_plan),
)

raw_connection = sqlite3.connect(config.db_path())
raw_prompt = raw_connection.execute(
    "SELECT prompt_json FROM call_content WHERE call_id=?", ("lookup-call",)
).fetchone()[0]
record_script_check(
    failures,
    "call_content is physically compressed rather than retained as raw text",
    raw_prompt.startswith(COMPRESSED_TEXT_PREFIX) and repeated_text not in raw_prompt,
)
raw_connection.execute(
    "INSERT INTO call_content(id,call_id,ts,prompt_json,system_json,response_json) VALUES(?,?,?,?,?,?)",
    ("legacy-id", "legacy-call", "legacy-ts", json.dumps(repeated_text), "null", "{}"),
)
raw_connection.commit()
legacy_prompt = retention_connection.execute(
    "SELECT prompt_json FROM call_content WHERE call_id=?", ("legacy-call",)
).fetchone()[0]
record_script_check(failures, "legacy plaintext retained rows remain readable", json.loads(legacy_prompt) == repeated_text)

callio.record_io_sample(
    "compression-intent", "provider", "model", "compression-batch", "compression-custom",
    repeated_text, repeated_text, system=repeated_text, req_schema={"text": repeated_text},
)
callio_row = callio._callio_db().execute(
    "SELECT prompt,output,system,req_schema,truncated FROM call_io WHERE batch=?",
    ("compression-batch",),
).fetchone()
record_script_check(
    failures,
    "call_io stores whole compressed bodies without the former cap",
    callio_row[:3] == (repeated_text, repeated_text, repeated_text)
    and json.loads(callio_row[3]) == {"text": repeated_text}
    and callio_row[4] == 0,
)
incompressible_prompt = "".join(chr(33 + (position % 90)) for position in range(1000))
compressible_longer_prompt = "longer-but-smaller-on-disk:" + ("z" * 2000)
callio.record_io_sample(
    "growth-intent", "provider", "model", "growth-batch", "growth-custom",
    incompressible_prompt, "initial-output",
)
callio.record_io_sample(
    "growth-intent", "provider", "model", "growth-batch", "growth-custom",
    compressible_longer_prompt, "short",
)
grown_prompt, preserved_output = callio._callio_db().execute(
    "SELECT prompt,output FROM call_io WHERE batch=? AND custom_id=?",
    ("growth-batch", "growth-custom"),
).fetchone()
record_script_check(
    failures,
    "grow-only refill compares decoded evidence rather than compressed storage length",
    grown_prompt == compressible_longer_prompt and preserved_output == "initial-output",
)

conv._seg_put_cls(
    "compression-segment", {"project": "project", "confidence": 100},
    seg={"sid": "session", "prompt": repeated_text},
)
segment_prompt = learn._insights_db().execute(
    "SELECT prompt FROM seg_attribution WHERE seg_id=?", ("compression-segment",)
).fetchone()[0]
record_script_check(failures, "seg_attribution prompt round-trips whole and compressed", segment_prompt == repeated_text)
parsed_segments = conv.segment_records(({
    "type": "user", "timestamp": "2026-10-27T00:00:00+00:00",
    "message": {"role": "user", "content": repeated_text},
},), sid="compression-session")
record_script_check(
    failures,
    "conversation segmentation preserves the whole prompt before attribution storage",
    len(parsed_segments) == 1 and parsed_segments[0]["prompt"] == repeated_text,
)

first_backfill = compress_retained_content_database(config.db_path(), batch_rows=1)
second_backfill = compress_retained_content_database(config.db_path(), batch_rows=1)
record_script_check(
    failures,
    "backfill is bounded and resumable while preserving legacy content",
    first_backfill.get("call_content") == 1 and second_backfill.get("call_content", 0) <= 1
    and retention_connection.execute(
        "SELECT prompt_json FROM call_content WHERE call_id=?", ("legacy-call",)
    ).fetchone()[0] == json.dumps(repeated_text),
)

raw_connection.close()
retention_connection.close()
print(f"\n{'[FAIL]' if failures else 'OK'} test_content_compression_and_lookup: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
