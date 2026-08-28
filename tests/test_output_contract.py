"""The output CONTRACT — a bulk run must be authorized by a test that checked the output, on the real data.

WHAT WAS WRONG. `test_job`'s verifier was optional and `verify_fn=None` recorded `verified=1` ("None → trust
that it ran"). So a full paid batch could be authorized by a sample that proved only that the API returned
something. Same shape as every other bug this session: a check that records DONE rather than CORRECT.

The failure that actually costs money is not item 1 failing — you would see that. It is item 1 parsing, item
400 arriving with a sentence before the JSON, and nothing saying a word until the batch is paid for. So the
contract is checked against EVERY item of the sample, and output that only parses after stripping a fence is
counted separately as `salvaged` rather than waved through.

FORMAT, NOT MEANING — pinned here: this module decides whether output parses into the declared shape (mechanical,
determined by the bytes). Whether an answer is CORRECT stays with the agentic quality path.

Invariants:
  • no contract and no verifier → UNVERIFIED, never "verified";
  • every item is checked, not just the first;
  • salvaged ≠ clean;
  • a changed contract, or a test on different data, expires the authorization;
  • the block message says WHICH of those failed.
"""
import os, sys, tempfile

# Set UNCONDITIONALLY, before any spendguard import: under test_runner.py the isolation flag is already set, so
# anything inside the self-isolation block below would silently not apply and the gate would run in `warn` mode
# — the tests would then pass for the wrong reason. (Same trap documented in tests/test_receipt.py.)
os.environ["SPENDGUARD_ENFORCE"] = "block"              # exercise the real gate, not the roll-out grace period
os.environ["SPENDGUARD_REQUIRE_EVAL"] = "0"             # this file tests the contract/data-signature freshness layer;
                                                        # the EVAL checkpoint has its own test_lifecycle_eval_gate.py

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-contract-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import io, contextlib, inspect
from spendguard import output_contract as oc, bulkgate

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


GOOD = '{"patient_id": "p1", "findings": ["a"]}'
FENCED = '```json\n{"patient_id": "p2", "findings": []}\n```'
PREAMBLE = 'Here is the JSON you asked for:\n{"patient_id": "p3", "findings": []}'
MISSING = '{"patient_id": "p4"}'
PROSE = 'I was unable to read this page.'
KEYS = ["patient_id", "findings"]

print("-- required keys: the cheapest useful contract --")
r = oc.check([GOOD, GOOD], KEYS)
check("clean output is clean", r.clean and r.parsed == 2 and r.failed == 0)
r = oc.check([GOOD, MISSING], KEYS)
check("a missing key FAILS, and names the key", r.failed == 1 and "findings" in r.first_failure, r.first_failure)
r = oc.check([GOOD, PROSE], KEYS)
check("prose instead of JSON fails with a readable reason", r.failed == 1 and "not JSON" in r.first_failure,
      r.first_failure)

print("-- salvaged output is NOT clean (this is the item-400 failure) --")
r = oc.check([GOOD, FENCED], KEYS)
check("a code fence parses but is counted as salvaged", r.parsed == 2 and r.salvaged == 1)
check("…and salvaged is therefore NOT clean", r.clean is False)
r = oc.check([GOOD, PREAMBLE], KEYS)
check("a prose preamble is salvaged too", r.salvaged == 1)
check("the summary tells the operator what to do about it", "salvaged" in oc.check([FENCED], KEYS).summary())

print("-- EVERY item is checked, not just the first --")
big = [GOOD] * 399 + [MISSING]
r = oc.check(big, KEYS)
check("a failure at item 400 of 400 is caught", r.failed == 1 and r.failures[0]["index"] == 399)
check("the counts add up", r.parsed + r.failed == 400)

print("-- the other contract forms --")
check("'json' accepts any parseable JSON", oc.check([GOOD, '[]'], "json").clean)
check("'json' still rejects prose", oc.check([PROSE], "json").failed == 1)
schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "number"}}}
check("schema: type mismatch fails and names the path",
      "n" in oc.check(['{"n": "seven"}'], schema).first_failure)
check("schema: a boolean is not a number (the classic JSON trap)",
      oc.check(['{"n": true}'], schema).failed == 1)
check("schema: correct types pass", oc.check(['{"n": 7}'], schema).clean)
check("callable: False means fail", oc.check([{"score": 0}], lambda i: i["score"] > 0).failed == 1)
check("callable: a raise is a failure, not a crash",
      oc.check([{}], lambda i: i["missing"]).failed == 1)

calls = []
oc.check([1, 2, 3], lambda i: calls.append(i) or True)
check("a callable verifier runs ONCE per item (may be expensive/stateful)", len(calls) == 3, str(len(calls)))

print("-- identity: a changed contract must expire the authorization --")
check("the same contract hashes the same", oc.contract_hash(KEYS) == oc.contract_hash(list(reversed(KEYS))))
check("a different contract hashes differently", oc.contract_hash(KEYS) != oc.contract_hash(KEYS + ["extra"]))
check("a contract is described legibly for the ledger", oc.describe(KEYS) == "keys:findings,patient_id")
check("no contract → empty identity, not a fake one", oc.contract_hash(None) == "")

print("-- data signature: the inputs are fingerprinted, never stored --")
a, b = oc.data_signature(["page1", "page2"]), oc.data_signature(["page1", "page3"])
check("different data → different signature", a != b)
check("same data → same signature", a == oc.data_signature(["page1", "page2"]))
check("no data → empty, not a hash of nothing", oc.data_signature([]) == "")
check("the raw data never appears in the signature", "page1" not in a)
src = inspect.getsource(oc)
check("the module stores nothing and reaches nowhere",
      not any(w in src for w in ("urllib", "requests", "socket", "sqlite3", "open(")))

print("-- THE FIX: no contract and no verifier is UNVERIFIED, not verified --")
SIG = "test-sig-1"
bulkgate.record_estimate(SIG, "test-model", 10.0, 500)
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    bulkgate.test_job(SIG, lambda n: [GOOD] * n, n=3)
st = bulkgate.gate_status(SIG)
check("a test with nothing checking it is recorded UNVERIFIED", st["verified"] is False)
check("…so it does NOT authorize a bulk run", st["fresh"] is False)
check("and it says so on stderr", "UNVERIFIED" in buf.getvalue())
check("the reason is legible", "did NOT match" in st["reason"] or "contract" in st["reason"], st["reason"])

print("-- a contract-checked test DOES authorize, and records the evidence --")
bulkgate.test_job(SIG, lambda n: [GOOD] * n, n=3, contract=KEYS, items=["p1", "p2", "p3"])
st = bulkgate.gate_status(SIG, contract=KEYS, data_sig=oc.data_signature(["p1", "p2", "p3"]))
check("verified", st["verified"] is True)
check("fresh → authorized", st["fresh"] is True)
check("the ledger records what was checked", st["contract"] == "keys:findings,patient_id")
check("…and how the sample did", st["parsed"] == 3 and st["failed"] == 0)
check("check_bulk passes", bulkgate.check_bulk(SIG, "test-model", 500, 10.0,
                                               contract=KEYS, data_sig=oc.data_signature(["p1", "p2", "p3"])) == "pass")

print("-- a CHANGED contract expires it (tested v1, ran v2) --")
st2 = bulkgate.gate_status(SIG, contract=KEYS + ["diagnosis"])
check("a widened contract is no longer fresh", st2["fresh"] is False)
check("and the reason names the contract change", "contract CHANGED" in st2["reason"], st2["reason"])
try:
    bulkgate.check_bulk(SIG, "test-model", 500, 10.0, contract=KEYS + ["diagnosis"])
    blocked = False
except bulkgate.GateBlocked as e:
    blocked, msg = True, str(e)
check("check_bulk BLOCKS on the changed contract", blocked)
check("the block message says which check failed", blocked and "contract CHANGED" in msg)

print("-- a test on DIFFERENT data expires it (three toy rows ≠ the corpus) --")
st3 = bulkgate.gate_status(SIG, contract=KEYS, data_sig=oc.data_signature(["real1", "real2"]))
check("different data → not fresh", st3["fresh"] is False)
check("and the reason names the data", "DIFFERENT data" in st3["reason"], st3["reason"])

print("-- a sample that FAILS the contract cannot authorize anything --")
SIG2 = "test-sig-2"
bulkgate.record_estimate(SIG2, "test-model", 10.0, 500)
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    bulkgate.test_job(SIG2, lambda n: [GOOD] * (n - 1) + [MISSING], n=3, contract=KEYS, items=["a", "b", "c"])
st = bulkgate.gate_status(SIG2, contract=KEYS)
check("verified=False when the sample failed", st["verified"] is False)
check("the failure detail is kept for the operator", st["failed"] == 1 and "findings" in st["failure"])
check("and it was reported at test time, not silently", "did NOT satisfy" in buf.getvalue())
try:
    bulkgate.check_bulk(SIG2, "test-model", 500, 10.0, contract=KEYS)
    blocked2, msg2 = False, ""
except bulkgate.GateBlocked as e:
    blocked2, msg2 = True, str(e)
check("the bulk run is blocked", blocked2)
check("the message quotes the actual failure so it is actionable", blocked2 and "findings" in msg2, msg2[:200])

print("-- salvaged-only output does not authorize either --")
SIG3 = "test-sig-3"
bulkgate.record_estimate(SIG3, "test-model", 10.0, 500)
with contextlib.redirect_stderr(io.StringIO()):
    bulkgate.test_job(SIG3, lambda n: [FENCED] * n, n=3, contract=KEYS, items=["a"])
st = bulkgate.gate_status(SIG3, contract=KEYS)
check("fenced-only output is not verified", st["verified"] is False)
check("the salvage count is recorded", st["salvaged"] == 3)

print("-- gated_batch carries the same discipline --")
gsrc = inspect.getsource(bulkgate.gated_batch)
check("its .test takes a contract", "contract=None" in gsrc)
check("its .run asserts the SAME contract it was tested with", "_contract" in gsrc)
check("legacy verify_fn still works (no forced migration)",
      "verify_fn" in inspect.getsource(bulkgate.test_job))

print("-- REALTIME gets the same check (a loop cannot be gated: the money goes call by call) --")
from spendguard import calls
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    with calls.context(intent="rt-extract", contract=KEYS):
        calls.check_output(GOOD)
        calls.check_output(GOOD)
        t_mid = calls.contract_tally()
        calls.check_output(MISSING)              # call 3 stops matching — the item-400 failure, live
        t_end = calls.contract_tally()
err = buf.getvalue()
check("clean calls are counted", t_mid["parsed"] == 2 and t_mid["failed"] == 0)
check("a failing call is counted", t_end["failed"] == 1)
check("it WARNS while the loop is still running (not at the end)", "CONTRACT FAILED" in err)
check("the warning names the call number and the reason",
      "#3" in err and "findings" in err, err[:160])
check("it says the loop is still spending", "still" in err and "spending" in err)
check("the flow receipt reports the contract", "contract [rt-extract]" in err, err[-200:])

print("-- and a realtime flow with NO contract is unaffected (opt-in, zero cost) --")
buf2 = io.StringIO()
with contextlib.redirect_stderr(buf2):
    with calls.context(intent="plain"):
        calls.check_output(PROSE)                # would fail any contract; none declared → nothing happens
check("no contract declared → no contract output at all", "contract [" not in buf2.getvalue())
check("check_output is a no-op without a contract", calls.contract_tally() == {})

print("-- a broken contract cannot break the caller's loop --")
with contextlib.redirect_stderr(io.StringIO()):
    with calls.context(intent="boom", contract=lambda item: 1 / 0):
        calls.check_output(GOOD)                 # raises inside the contract
check("a raising contract does not propagate", True)

print(f"\n{'[FAIL]' if failures else 'OK'} test_output_contract: {failures} failure(s)")
sys.exit(1 if failures else 0)
