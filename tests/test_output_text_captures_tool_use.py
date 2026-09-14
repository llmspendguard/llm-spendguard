"""gate._output_text records the REAL answer — including an Anthropic forced-tool / `schema` response whose answer
lives in a tool_use block's `input`, not a text block. The prior recorder joined only text blocks, so a clean
structured opus success was stored as an EMPTY output_snip — making a fanned pinned-matrix vote LOOK empty in the
ledger even though the caller got the real verdict (adapters._call_once reads tool_use.input; the ledger recorder
must too, or the two disagree). This guards that the recorder no longer drops the tool_use answer.

Offline: fake provider result shapes, no API call.
"""
import os
import sys
import json
import tempfile

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-ot-"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import gate   # noqa: E402


def report_check(name, cond):
    """Print one PASS/FAIL line and return [] on pass or [name] on fail, so the caller accumulates failures."""
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


fails = []


class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _AnthResult:
    def __init__(self, content):
        self.content = content


class _OpenAIResult:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": content})})]


print("-- OpenAI text answer (choices[0].message.content) is captured --")
fails += report_check("openai text captured", gate._output_text(_OpenAIResult("hello")) == "hello")

print("-- Anthropic TEXT answer (a text block) is captured --")
fails += report_check("anthropic text captured",
                      gate._output_text(_AnthResult([_Block(type="text", text="a verdict")])) == "a verdict")

print("-- Anthropic FORCED-TOOL/schema answer (tool_use.input) is captured, NOT recorded empty --")
_tu = _Block(type="tool_use", input={"verdict": "REAL", "why": "x"})
_out = gate._output_text(_AnthResult([_tu]))
fails += report_check("tool_use answer captured as its input JSON (not empty)",
                      bool(_out) and json.loads(_out) == {"verdict": "REAL", "why": "x"})

print("-- when both are present a text block wins (text is the visible answer) --")
fails += report_check("text preferred over tool_use when both present",
                      gate._output_text(_AnthResult([_Block(type="text", text="T"), _tu])) == "T")

print("-- a truly empty content still records '' (no invented answer) --")
fails += report_check("empty content → '' (unchanged)", gate._output_text(_AnthResult([])) == "")

print(f"\n{'[FAIL]' if fails else 'OK'} test_output_text_captures_tool_use: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
