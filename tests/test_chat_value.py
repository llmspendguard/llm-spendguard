"""claude.ai chat VALUE math — the financially load-bearing logic (token accounting across all content, the
caching-aware per-turn model, image vision tokens, per-day attribution, allocation split). Pure functions, no
network. Isolated SPENDGUARD_HOME."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import chat

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# _content_toks: human text → input; assistant text+tool_use+thinking → output; tool_result → input; images → input
i, o = chat._content_toks({"sender": "human", "content": [{"type": "text", "text": "x" * 400}]})
ck("human text → input only", i > 0 and o == 0)
i, o = chat._content_toks({"sender": "assistant", "content": [
    {"type": "text", "text": "y" * 400}, {"type": "tool_use", "input": {"code": "z" * 400}},
    {"type": "thinking", "thinking": "t" * 400}]})
ck("assistant text+tool+thinking → output only", o > 0 and i == 0)
i, o = chat._content_toks({"sender": "human", "content": [{"type": "tool_result", "content": "r" * 400}]})
ck("tool_result → input", i > 0 and o == 0)
i, o = chat._content_toks({"sender": "human", "files": [{"file_kind": "image", "file_name": "slide.png"}]})
ck("uploaded image → vision input tokens", i >= chat._img_tokens())
i, o = chat._content_toks({"sender": "assistant", "text": ""})
ck("empty message → zero", i == 0 and o == 0)

# _value_breakdown: per message-DAY + caching-aware (prior context billed at cache-read rate)
detail = {"model": "claude-opus-4-8", "chat_messages": [
    {"sender": "human", "created_at": "2026-06-01T00:00:00Z", "content": [{"type": "text", "text": "q" * 400}]},
    {"sender": "assistant", "created_at": "2026-06-01T00:01:00Z", "content": [{"type": "text", "text": "a" * 4000}]},
    {"sender": "human", "created_at": "2026-06-02T00:00:00Z", "content": [{"type": "text", "text": "q" * 400}]},
    {"sender": "assistant", "created_at": "2026-06-02T00:01:00Z", "content": [{"type": "text", "text": "a" * 2000}]}]}
model, days = chat._value_breakdown(detail)
ck("value split across the 2 days", set(days) == {"2026-06-01", "2026-06-02"})
ck("value > 0", sum(d["value"] for d in days.values()) > 0)
ck("day2 input includes the grown cached context", days["2026-06-02"]["in_tok"] > days["2026-06-01"]["in_tok"])
ck("turns counted per assistant message", days["2026-06-01"]["turns"] == 1)

# _allocation: normalizes pct → fractions; falls back to the primary/single project
al = chat._allocation({"allocation": [{"project": "a", "pct": 70}, {"project": "b", "pct": 30}]})
ck("allocation normalizes to 1.0", abs(sum(w for _, w in al) - 1.0) < 1e-6 and len(al) == 2)
ck("allocation fallback to single project", chat._allocation({"project": "solo"}) == [("solo", 1.0)])

print(("\n[FAIL] " if fails else "\n[OK] ") + f"chat_value: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
