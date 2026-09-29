"""A genuinely-MALFORMED schema is refused BEFORE any provider call — $0, attributed to the CONTRACT, never billed then
discarded.

THE BUG CLASS THIS GUARDS. A schema whose `required`/`nonempty` holds a non-string entry (e.g. a nested list) cannot be
validated: output_contract._check_schema would try to use that entry as a dict key, raising the opaque
'TypeError: cannot use list as a dict key', which check_item catches and files as a schema_violation — AFTER the answer
is billed. It is knowable from the schema ALONE, so adapters._call_guarded runs a wellformed-contract preflight (the
shape twin of the input-fits preflight) and returns a clean error with NO dispatch.

NOT the same as the healiom-investor-score $0.10 loss: that was a WELL-FORMED union type ("type": ["number","null"]) the
validator failed to support (fixed in output_contract._check_schema; guarded in test_output_contract.py). A union type
is valid and MUST validate, so this preflight correctly does NOT refuse it — this file guards only the rarer,
caller-authored malformed-contract case.

Offline + isolated: adapters._call_once (the dispatch seam _call_guarded recurses into via call(_no_guard=True)) is
replaced by a recorder. A malformed-schema call must NEVER reach it; a well-formed one must.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-malformedschema-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters   # noqa: E402


def main():
    results = []

    def ck(label, cond):
        results.append(bool(cond))
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")

    dispatched = []

    def _rec_call_once(model, prompt, **kw):   # the seam call(_no_guard=True) reaches; a malformed schema must NOT get here
        dispatched.append({"model": model, "schema": kw.get("schema")})
        return {"provider": "openai", "model": model, "text": '{"patient_id": "p1"}', "in_tok": 1, "out_tok": 1,
                "latency": 0.0, "cost": 0.01, "error": None, "finish_reason": "stop"}
    adapters._call_once = _rec_call_once

    BAD = {"type": "object", "required": [["patient_id"]]}     # a non-string key → the contract cannot be validated
    GOOD = {"type": "object", "required": ["patient_id"], "properties": {"patient_id": {"type": "string"}}}

    # reasoning pinned (not best-value) + no_substitution so the path is deterministic: straight to _call_guarded.
    r = adapters.call("openai:gpt-5.5", "score this lead", schema=BAD, reasoning="minimal", no_substitution=True)
    ck("a malformed schema returns an ERROR (never a silent pass)", isinstance(r, dict) and bool(r.get("error")))
    ck("the error names it a malformed CONTRACT (a caller bug), not a model schema_violation",
       "malformed contract" in (r.get("error") or ""))
    ck("…and it never leaks the raw hashing TypeError ('unhashable' / 'dict key')",
       "unhashable" not in (r.get("error") or "") and "dict key" not in (r.get("error") or ""))
    ck("it is $0 — nothing was billed (cost falsy)", not r.get("cost"))
    ck("NO dispatch happened — refused BEFORE the provider call", len(dispatched) == 0)

    r2 = adapters.call("openai:gpt-5.5", "score this lead", schema=GOOD, reasoning="minimal", no_substitution=True)
    ck("a WELL-FORMED schema is NOT refused — it reaches dispatch exactly once",
       len(dispatched) == 1 and not r2.get("error"))
    ck("the dispatched call carried the caller's schema through", dispatched and dispatched[0]["schema"] == GOOD)

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_malformed_contract_refused_before_spend: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
