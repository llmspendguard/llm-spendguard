"""A prompt alone does not describe a call — the SYSTEM message and the output contract shape the answer.

WHY THIS GUARD EXISTS. call_io stored prompt + output and nothing about the request. Replaying those prompts
bare produced answers 10-32x larger than the originals, and the harness reported it as "median |token error|
1,107%" against the estimator. It was not the estimator. The originals were schema-forced -- one true answer was
the four tokens `NO`, another `{"shadow_parent":"ranitidine","confidence":"high"}` -- and replayed without
their contract the models wrote prose. The tell: the two intents whose originals were free-text landed at
-10% and -31%, while every schema-forced one blew out. A missing field, not a wrong number.

`fetch_openai` also took msgs[-1] alone, which keeps the question and discards the instructions.
"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-reqshape-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import inspect, json, sqlite3                            # noqa: E402

from spendguard import callio, config                    # noqa: E402

failures = 0


def check(label, ok, extra=""):
    global failures
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  — {extra}" if not ok and extra else ""))


SCHEMA = {"type": "json_schema", "json_schema": {"name": "verdict"}}
callio.record_io_sample("probe", "openai", "gpt-5.5", "batch_1", "cid_1", "is X a Y?", "NO", out_tok=4,
              system="Answer with exactly YES or NO.", req_schema=SCHEMA, req_max_tokens=16)

c = sqlite3.connect(config.db_path())
row = c.execute("SELECT system, req_schema, req_max_tokens FROM call_io WHERE custom_id='cid_1'").fetchone()
check("the SYSTEM message is stored", row and row[0] == "Answer with exactly YES or NO.", str(row))
check("the output CONTRACT is stored", row and json.loads(row[1]) == SCHEMA, str(row[1] if row else None))
check("the request's max_tokens is stored", row and row[2] == 16, str(row[2] if row else None))

# A later capture that learns the shape must backfill a row that predates it -- strictly new information.
callio.record_io_sample("probe", "openai", "gpt-5.5", "batch_2", "cid_2", "q", "a", out_tok=3)
callio.record_io_sample("probe", "openai", "gpt-5.5", "batch_2", "cid_2", "q", "a", out_tok=3,
              system="sys-later", req_max_tokens=32)
row2 = c.execute("SELECT system, req_max_tokens FROM call_io WHERE custom_id='cid_2'").fetchone()
check("a row captured before the shape existed is BACKFILLED, not left blank",
      row2 and row2[0] == "sys-later" and row2[1] == 32, str(row2))

src = inspect.getsource(callio.fetch_openai)
check("fetch_openai extracts the system message, not just the last message",
      '"system"' in src and "role" in src, "msgs[-1] alone keeps the question and drops the instructions")
check("fetch_openai extracts the output contract", "response_format" in src)
check("fetch_openai extracts the request's max_tokens", "max_tokens" in src)

print(f"\n{'[FAIL]' if failures else 'OK'} test_callio_request_shape: {failures} failure(s)")
sys.exit(1 if failures else 0)
