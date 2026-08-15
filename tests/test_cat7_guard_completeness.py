"""Cat-7 guard/probe completeness — the two guards that had a real blind spot now cover it:

  * gate.gated_sdk_modules() — the REQUIRE fail-closed probe now checks the SAME SDK set the gate can patch
    (derived from the interceptor specs) PLUS the litellm / google-genai / vertex adapters — not just the two
    hardcoded openai/anthropic names a litellm-only venv slipped past. And it EXCLUDES boto3 (a general AWS SDK
    whose presence does not imply LLM use).
  * token_caps.sites() — a hardcoded output-token cap declared as a POSITIONAL-ONLY signature default (PEP 570)
    was missed (defaults were paired against a.args alone); the scan now finds it, making the module's
    "complete by construction" docstring actually true.

Offline, isolated home; token_caps scans a temp file we write.
"""
import os
import sys
import tempfile
import pathlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cat7-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate, token_caps        # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── REQUIRE probe covers every gatable SDK, and excludes boto3 ─────────────────────────────────────────────────
mods = set(gate.gated_sdk_modules())
ck("probe includes the static-interceptor SDKs (openai, anthropic)",
   {"openai", "anthropic"} <= mods, f"got {sorted(mods)}")
ck("probe includes the adapter SDKs (litellm + google-genai + vertex)",
   {"litellm", "google.generativeai", "google.genai", "vertexai"} <= mods, f"got {sorted(mods)}")
ck("probe EXCLUDES boto3 (present != LLM use)", "boto3" not in mods)

# ── token_caps finds a POSITIONAL-ONLY cap default (was missed before) ──────────────────────────────────────────
d = tempfile.mkdtemp(prefix="tc-")
pathlib.Path(d, "probe.py").write_text("def f(max_tokens=500, /):\n    return max_tokens\n")
found = token_caps.sites(d)
posonly = [s for s in found
           if s["kwarg"] == "max_tokens" and s["value"] == 500 and s["kind"] == "signature-default"]
ck("a positional-only max_tokens default is now found by the scan", len(posonly) == 1, f"got {found}")

print(f"\n{'[FAIL]' if fails else 'OK'} test_cat7_guard_completeness: {fails} failure(s)")
sys.exit(1 if fails else 0)
