"""Offline test for the Layer-2 advisor's JSON parser — NO network, NO API calls.

The reasoner can wrap its JSON in ```json fences and can be truncated at max_tokens; the parser
must tolerate both (salvage complete objects, drop a trailing partial). Pure function — no db needed.
"""
from spendguard.advisor import _parse_insights


def check(name, text, expect_lessons):
    got = _parse_insights(text)
    lessons = [o.get("lesson") for o in got] if got else None
    ok = lessons == expect_lessons
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {lessons}")
    assert ok, f"{name}: got {lessons}, expected {expect_lessons}"


clean = '[{"intent":null,"lesson":"a","confidence":0.8,"evidence":"x"}]'
fenced = '```json\n[{"intent":"t","lesson":"b","confidence":0.7,"evidence":"y"}]\n```'
truncated = ('```json\n[{"intent":null,"lesson":"c","confidence":0.9,"evidence":"z"},'
             '{"intent":"t2","lesson":"dd')                    # cut mid second object
multi = '[{"lesson":"one","confidence":0.5},{"lesson":"two","confidence":0.6}]'
garbage = "I cannot produce JSON for this."

print("-- advisor._parse_insights --")
check("clean array", clean, ["a"])
check("```json fenced", fenced, ["b"])
check("truncated -> salvage complete obj", truncated, ["c"])
check("multi-object", multi, ["one", "two"])
assert _parse_insights(garbage) is None, "non-JSON must return None (caller falls back)"
print("  [OK] non-JSON -> None (fallback path)")
print("done.")
