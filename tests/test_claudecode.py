"""Claude Code adapter — parse + INCREMENTAL watermark, NO network. Synthetic transcript dir.

Covers: per-turn cost from usage × pricing · project from cwd · work-done (tools + files) · the watermark
(re-run with no new lines = no double-count; append a turn = only the new line read + accumulated).
"""
import os, sys, json, tempfile, time

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

CC = tempfile.mkdtemp(prefix="cc-")
os.environ["SPENDGUARD_CC_DIR"] = CC
os.makedirs(os.path.join(CC, "proj"), exist_ok=True)
SESS = os.path.join(CC, "proj", "s1.jsonl")


def turn(model, intok, outtok, tools=None, cwd="/x/lmm"):
    content = [{"type": "text", "text": "ok"}]
    for t in (tools or []):
        content.append({"type": "tool_use", "name": t,
                        "input": {"file_path": "/x/lmm/a.py"} if t in ("Edit", "Write") else {}})
    return json.dumps({"type": "assistant", "cwd": cwd, "timestamp": "2026-06-20T10:00:00Z",
                       "message": {"role": "assistant", "model": model, "content": content,
                                   "usage": {"input_tokens": intok, "output_tokens": outtok,
                                             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})


open(SESS, "w").write(turn("claude-opus-4-8", 1000, 500, ["Edit", "Bash"]) + "\n")

from spendguard import claudecode, pricing                      # noqa: E402

fail = 0
def ck(label, cond):
    global fail
    ok = bool(cond); fail += (not ok)
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")

exp = pricing.realtime_cost("claude-opus-4-8", 1000, 500, 0)
st, info = claudecode.update()
spend = [v for v in st["ledger"].values() if not v.get("_work")]
work = [v for v in st["ledger"].values() if v.get("_work")]
ck("1 spend row, project=lmm (from cwd)", len(spend) == 1 and spend[0]["project"] == "lmm")
ck(f"cost == realtime_cost (${exp:.4f})", abs(spend[0]["cost"] - exp) < 1e-9)
ck("work: Edit×1 + Bash×1 + 1 file", work and work[0]["tools"].get("Edit") == 1 and work[0]["tools"].get("Bash") == 1 and len(work[0]["files"]) == 1)
claudecode._save_state(st)

st2, info2 = claudecode.update(claudecode._load_state())         # re-run, nothing new
spend2 = [v for v in st2["ledger"].values() if not v.get("_work")]
ck("watermark: re-run, no new lines → no double-count", abs(spend2[0]["cost"] - exp) < 1e-9 and info2["new_lines"] == 0)
claudecode._save_state(st2)

open(SESS, "a").write(turn("claude-opus-4-8", 2000, 1000, ["Write"]) + "\n")
os.utime(SESS, (9_999_999_999, 9_999_999_999))                  # force mtime ahead so the watermark re-reads
exp2 = exp + pricing.realtime_cost("claude-opus-4-8", 2000, 1000, 0)
st3, info3 = claudecode.update(claudecode._load_state())
spend3 = [v for v in st3["ledger"].values() if not v.get("_work")]
ck("append a turn → accumulated (only the new line read)", abs(spend3[0]["cost"] - exp2) < 1e-9 and info3["new_lines"] == 1)

# day_totals shape (server push rows)
dt = [r for r in claudecode.day_totals("me@x.com") if r["project"] == "lmm"]
ck("day_totals: channel=claude-code, provider=anthropic", dt and dt[0]["channel"] == "claude-code" and dt[0]["provider"] == "anthropic")

print(f"\n{'[FAIL]' if fail else 'OK'} claudecode: {fail} failure(s)")
sys.exit(1 if fail else 0)
