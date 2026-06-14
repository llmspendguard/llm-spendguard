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
print("done.")
