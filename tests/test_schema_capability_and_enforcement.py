"""#1 capability-aware auto-route — the two structural primitives it routes on:
  • adapters.schema_capability(provider) — can this vendor's METERED path ENFORCE a shape (anthropic/openai) or only
    return parseable JSON (every other OpenAI-compatible vendor)? Derived from the same _schema_kind the realtime
    request uses, so the capability view can't drift from what json_schema_request actually builds.
  • output_contract.needs_enforcement(contract) — does the contract DECLARE a constraint a prompt-only path cannot
    guarantee (a required list / nonempty marker, anywhere in the tree)? Structural, never a judgement of meaning.

Offline, isolated home; both are pure functions (no network, no ledger).
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-schemacap-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, output_contract   # noqa: E402


def ck(results, label, cond):
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


def main():
    results = []

    # ── A. schema_capability: only the two real strict vendors enforce; every compat vendor is json_object ──
    ck(results, "anthropic → strict", adapters.schema_capability("anthropic") == adapters.SCHEMA_STRICT)
    ck(results, "openai → strict", adapters.schema_capability("openai") == adapters.SCHEMA_STRICT)
    for compat in ("zai", "moonshot", "deepseek", "gemini"):
        ck(results, f"{compat} → json_object (no strict meter)",
           adapters.schema_capability(compat) == adapters.SCHEMA_JSON_OBJECT)
    ck(results, "_schema_kind matches json_schema_request's dialects",
       adapters._schema_kind("anthropic") == "anthropic" and adapters._schema_kind("openai") == "openai"
       and adapters._schema_kind("zai") == "compat")

    # ── B. needs_enforcement: a declared required/nonempty (at any depth) is strict; lenient otherwise ──
    ck(results, "required list → strict", output_contract.needs_enforcement({"required": ["a"]}))
    ck(results, "nonempty marker → strict", output_contract.needs_enforcement({"nonempty": ["a"]}))
    ck(results, "type+properties, no required/nonempty → lenient",
       not output_contract.needs_enforcement({"type": "object", "properties": {"x": {"type": "string"}}}))
    ck(results, "required NESTED under an array items → strict (recursive)",
       output_contract.needs_enforcement(
           {"type": "object", "properties": {"issues": {"type": "array", "items": {"required": ["line"]}}}}))
    ck(results, "'json' string contract → lenient", not output_contract.needs_enforcement("json"))
    ck(results, "bare key-list contract → lenient", not output_contract.needs_enforcement(["a", "b"]))
    ck(results, "callable verifier → lenient", not output_contract.needs_enforcement(lambda x: True))

    # the real honestreview panel FINDING_SCHEMA shape (required + nonempty on nested items) → strict
    finding = {"type": "object", "required": ["issues"],
               "properties": {"issues": {"type": "array", "items": {
                   "type": "object", "required": ["line", "severity", "issue"], "nonempty": ["issue", "severity"]}}}}
    ck(results, "the honestreview FINDING_SCHEMA → strict (this is what churned on lanes)",
       output_contract.needs_enforcement(finding))

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_schema_capability_and_enforcement: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
