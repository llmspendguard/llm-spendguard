"""#1 — the gate's ACTUAL-usage extractors must reach THROUGH a with_raw_response wrapper.

THE DEFECT (found live by the consumer×provider smoke). adapters calls `chat.completions.with_raw_response.create` to
read the vendor's rate-limit headers off the response. That returns a LegacyAPIResponse/APIResponse WRAPPER whose
`.usage` attribute does NOT exist — the parsed body (with usage) sits behind `.parse()` (cached). The gate's act
extractor did `getattr(result, "usage", None)` → None → the recorder fell back to the max_tokens ESTIMATE, which for a
cold model is the published output CEILING. Measured: one deepseek call recorded in=5 / out=393,216 / $0.47 for a reply
that truly used 35 / 16 tokens / $0.00005 — a ~9,000x over-record on EVERY compat call, poisoning the ledger, cost
advice, and estimates.

THE FIX. _usage_bearer parses through a raw-response wrapper so the act extractor reads the true usage; a plain result
(usage on the object) is unchanged, and a genuinely usage-less result still reads None (→ an honest estimate, not a
fabricated one). This asserts all three shapes: chat (prompt/completion), responses (input/output), anthropic
(input/output). Offline — fake SDK objects, no network, no spend.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rawusage-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import gate   # noqa: E402


class _ChatUsage:
    prompt_tokens = 35
    completion_tokens = 16


class _RespUsage:
    input_tokens = 40
    output_tokens = 12


class _Parsed:
    def __init__(self, usage):
        self.usage = usage


class _RawWrapper:
    """A LegacyAPIResponse/APIResponse stand-in: NO `.usage`, the parsed body (with usage) behind a cached `.parse()`."""
    def __init__(self, usage):
        self._usage = usage
        self.parses = 0

    def parse(self):
        self.parses += 1
        return _Parsed(self._usage)


class _NoUsage:
    """A result with neither `.usage` nor `.parse` — genuinely nothing to read (→ None, an honest estimate follows)."""


def ck(results, label, cond, extra=""):
    results.append(bool(cond))
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


def main():
    results = []

    # ── the DEFECT case: a raw wrapper with usage only behind .parse() ──
    ck(results, "chat: raw wrapper → real (prompt, completion) via .parse()",
       gate._act_oai_chat(_RawWrapper(_ChatUsage())) == (35, 16),
       extra=str(gate._act_oai_chat(_RawWrapper(_ChatUsage()))))
    ck(results, "responses: raw wrapper → real (input, output) via .parse()",
       gate._act_oai_resp(_RawWrapper(_RespUsage())) == (40, 12),
       extra=str(gate._act_oai_resp(_RawWrapper(_RespUsage()))))
    ck(results, "anthropic: raw wrapper → real (input, output) via .parse()",
       gate._act_anth_msg(_RawWrapper(_RespUsage())) == (40, 12),
       extra=str(gate._act_anth_msg(_RawWrapper(_RespUsage()))))

    # ── a PLAIN parsed result (usage on the object) is unchanged, and NOT parsed again ──
    ck(results, "chat: plain parsed result still read directly", gate._act_oai_chat(_Parsed(_ChatUsage())) == (35, 16))
    plain = _Parsed(_ChatUsage())
    _ = gate._act_oai_chat(plain)
    ck(results, "a result that already has .usage is NOT re-parsed", not hasattr(plain, "parses"))

    # ── a genuinely usage-less result → None (the recorder then makes an HONEST estimate, not a fabricated usage) ──
    ck(results, "chat: no usage + no parse → None (honest, not fabricated)", gate._act_oai_chat(_NoUsage()) is None)

    # ── _usage_bearer parses AT MOST once (it is cached in the real SDK; we must not force extra parses) ──
    w = _RawWrapper(_ChatUsage())
    gate._act_oai_chat(w)
    ck(results, "the wrapper is parsed exactly once per extraction", w.parses == 1, extra=f"parses={w.parses}")

    n_fail = results.count(False)
    print(f"\n{'[FAIL]' if n_fail else 'OK'} test_gate_usage_from_raw_response: {n_fail} failure(s)")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
