"""Offline test for the conversation miner's pure logic — NO fs, NO db, NO network.

Covers transcript text extraction (string / block-list / tool_result shapes), the event score, and
that the synth system prompt formats without tripping over its literal JSON braces (the .replace bug).
"""
from spendguard import conv


def check(name, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {name}")
    assert cond


print("-- _text_of --")
check("string content", conv._text_of({"message": {"content": "hello $234"}}) == "hello $234")
blocks = {"message": {"content": [{"type": "text", "text": "a"}, {"type": "tool_use", "name": "x"},
                                  {"type": "text", "text": "b"}]}}
check("block list (text only)", conv._text_of(blocks) == "a\nb")
tr = {"message": {"content": [{"type": "tool_result", "content": "out1"}]}}
check("tool_result string", conv._text_of(tr) == "out1")
check("no message", conv._text_of({"foo": 1}) == "")

print("-- _score (cost + user-statement weighting) --")
gold = {"role": "user", "costs": ["$234", "$33"], "sigs": ["pack", "cancel"], "runs": ["batch_x"]}
meh = {"role": "assistant", "costs": [], "sigs": ["batch"], "runs": []}
check("gold scores above meh", conv._score(gold) > conv._score(meh))

print("-- synth system prompt formats (literal JSON braces, .replace not .format) --")
sysmsg = conv._SYS.replace("{{k}}", "7")
check("k substituted", "AT MOST 7 objects" in sysmsg)
check("JSON braces preserved", '{"intent": str|null' in sysmsg)

print("-- session_chunks NEVER truncates evidence: the tail past char 6000 survives (input is bounded ONLY at the provider window) --")
# The attribution reconstruction reads these chunks to find realtime runs + their tokens. A silent per-part [:6000] cut
# dropped exactly the loop-scale / token tell that can sit past char 6000 of a tool_use command (the script that MADE
# the calls). Assert the WHOLE content survives across the yielded chunks; input is bounded only by adapters._input_fits
# at the provider window (in the consumer), never by a cut here.
import json as _json, os as _os, tempfile as _tf, shutil as _sh                          # noqa: E402
_td = _tf.mkdtemp(prefix="conv-notrunc-")
_CMD_TAIL = "SCALE_TELL_range_10521"                       # a contiguous marker placed well PAST char 6000 of the command
_big_cmd = ("pad_token " * 800) + _CMD_TAIL + (" trailer" * 40)     # > 8000 chars; the marker lives past 6000
_big_txt = ("narrative " * 900) + "TEXT_TAIL_MARKER"               # > 8000 chars of message text, marker at the very end
_msg = {"timestamp": "2026-09-29T00:00:00Z", "message": {"role": "assistant", "content": [
    {"type": "text", "text": _big_txt},
    {"type": "tool_use", "name": "Bash", "input": {"command": _big_cmd}}]}}
with open(_os.path.join(_td, "sess1.jsonl"), "w") as _fh:
    _fh.write(_json.dumps(_msg) + "\n")
_joined = "".join(c for _sid, c in conv.session_chunks(tdir=_td))
check("the tool_use command's tail (past char 6000) is present — never cut at 6000", _CMD_TAIL in _joined)
check("the message text's tail is present too", "TEXT_TAIL_MARKER" in _joined)
_small = [c for _sid, c in conv.session_chunks(tdir=_td, max_chars=4000)]      # large input is CHUNKED (split), not dropped
check("oversized input is CHUNKED into pieces bounded by max_chars, and every part is still seen",
      len(_small) >= 2 and all(len(c) <= 4000 for c in _small) and _CMD_TAIL in "".join(_small))
_sh.rmtree(_td, ignore_errors=True)
print("done.")
