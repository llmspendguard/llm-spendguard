"""chat.py pure transforms that the I/O (cookies/_api/update) hands off to — already decoupled from the network,
but previously untested: project resolution, the conversation→digest, and the per-(conv,day,PROJECT) allocation
SPLIT (money-critical: a conversation's day-value is divided across the projects it touched so per-project sums
stay additive — no double-count). Offline, isolated home. Script-style. (Token math is in test_chat_value.py.)"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-chatd-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import chat

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── _claudeai_project: project_uuid→projmap name · embedded project dict · else "" ──
ck("project: uuid resolves via projmap", chat._claudeai_project({"project_uuid": "p1"}, {"p1": "LMM"}) == "LMM")
ck("project: embedded {name} dict", chat._claudeai_project({"project": {"name": "SlideKit"}}, {}) == "SlideKit")
ck("project: neither → empty", chat._claudeai_project({}, {}) == "")

# ── _digest_conv: value = Σ day values · first_user from the first human msg · prev classification PRESERVED ──
detail = {"model": "claude-opus-4-8", "chat_messages": [
    {"sender": "human", "created_at": "2026-06-01T00:00:00Z", "content": [{"type": "text", "text": "design the schema " + "q" * 400}]},
    {"sender": "assistant", "created_at": "2026-06-01T00:01:00Z", "content": [{"type": "text", "text": "a" * 2000}]}]}
conv = {"uuid": "u1", "name": "Schema chat", "summary": "  some summary  ", "project_uuid": "p1", "updated_at": "2026-06-01T01:00:00Z"}
dg = chat._digest_conv(conv, detail, {"p1": "LMM"}, prev=None)
ck("digest: uuid + title carried", dg["uuid"] == "u1" and dg["title"] == "Schema chat")
ck("digest: value == Σ day values", abs(dg["value"] - round(sum(d["value"] for d in dg["days"].values()), 6)) < 1e-9 and dg["value"] > 0)
ck("digest: first_user extracted from the first human message", dg["first_user"].startswith("design the schema"))
ck("digest: ai_project resolved from projmap", dg["ai_project"] == "LMM")
ck("digest: no prior classification → empty org/team/project/allocation", dg["org"] == "" and dg["project"] == "" and dg["allocation"] == [])
dg2 = chat._digest_conv(conv, detail, {"p1": "LMM"}, prev={"org": "Acme", "team": "NLP", "project": "lmm", "allocation": [{"project": "lmm", "pct": 100}], "classify_conf": 0.9})
ck("digest: PRESERVES a prior agentic classification across re-fetch", dg2["org"] == "Acme" and dg2["project"] == "lmm" and dg2["classify_conf"] == 0.9)

# ── _day_rows: the allocation SPLIT — additive, no double-count, turns counted once ──
st = {"convs": {"u1": {
    "uuid": "u1", "org": "Acme", "team": "NLP", "model": "claude-opus-4-8",
    "allocation": [{"project": "lmm", "pct": 60}, {"project": "slidekit", "pct": 40}],
    "days": {"2026-06-22": {"value": 10.0, "in_tok": 1000, "out_tok": 500, "turns": 3}},
}}}
rows = chat._day_rows(st)
by = {r["project"]: r for r in rows}
ck("day_rows: a 2-project conversation → 2 rows", len(rows) == 2 and set(by) == {"lmm", "slidekit"})
ck("day_rows: value split 60/40 (6.0 / 4.0)", abs(by["lmm"]["value"] - 6.0) < 1e-9 and abs(by["slidekit"]["value"] - 4.0) < 1e-9)
ck("day_rows: ADDITIVE — the split sums back to the day value (no double-count)", abs(sum(r["value"] for r in rows) - 10.0) < 1e-9)
ck("day_rows: tokens split too", by["lmm"]["in_tok"] == 600 and by["slidekit"]["out_tok"] == 200)
ck("day_rows: turns counted ONCE (on the first split only)", by["lmm"]["turns"] == 3 and by["slidekit"]["turns"] == 0)
ck("day_rows: org/team ride along", by["lmm"]["org"] == "Acme" and by["lmm"]["team"] == "NLP")

# single-project conversation (allocation fallback) → one full-value row
st1 = {"convs": {"u2": {"uuid": "u2", "project": "solo", "days": {"2026-06-22": {"value": 5.0, "in_tok": 100, "out_tok": 50, "turns": 1}}}}}
ck("day_rows: single-project → one row, full value", len(chat._day_rows(st1)) == 1 and chat._day_rows(st1)[0]["value"] == 5.0)

# days-cutoff filter (a long-ago day is dropped when a window is given)
old = {"convs": {"u3": {"uuid": "u3", "project": "p", "days": {"2020-01-01": {"value": 9.0, "in_tok": 1, "out_tok": 1, "turns": 1}}}}}
ck("day_rows: a day before the `days` cutoff is filtered out", chat._day_rows(old, days=1) == [] and len(chat._day_rows(old)) == 1)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"chat_digest: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
