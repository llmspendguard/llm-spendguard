"""Phase-4 guard: the read-time SIDEBAR-TITLE join + the unified per-conversation view. Conversations are labeled
by the human title from the desktop session store (resolved at READ time — titles change, so a stale denormalized
copy would rot), and the view shows est-value and REAL overflow $ as SEPARATE columns, never summed. Pins:

  1. _sidebar_titles: cliSessionId -> title, and when a transcript has several session records (resumes) the MOST
     RECENTLY active one wins; a conversation with no record is absent (the caller then falls back to the uuid).
  2. conversations view: ranks by est-value, labels each by title (uuid fallback), and only shows the Real overflow
     column once overflow has been reconciled — and never adds it into the est-value.

Hermetic: isolated SPENDGUARD_HOME + a fabricated session store + seeded ledger rows. Zero spend."""
import os, sys, tempfile, json, io, contextlib

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    home = tempfile.mkdtemp(prefix="spendguard-cctitles-")
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = home
    os.execv(sys.executable, [sys.executable] + sys.argv)

# CC sessions dir derived from the isolated HOME — set HERE (not only inside the block above) so it holds whether
# this test self-re-execs OR the pytest runner pre-isolated it via SPENDGUARD_TEST_ISOLATED (which skips the block).
os.environ["SPENDGUARD_CC_SESSIONS_DIR"] = os.path.join(os.environ["SPENDGUARD_HOME"], "sessions")

from spendguard import claudecode, budget

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── fabricate the desktop session store ──
SESS = os.path.join(os.environ["SPENDGUARD_CC_SESSIONS_DIR"], "x")
os.makedirs(SESS, exist_ok=True)
def write_sess(fn, cli, title, la):
    with open(os.path.join(SESS, fn), "w") as f:
        json.dump({"cliSessionId": cli, "title": title, "lastActivityAt": la}, f)
write_sess("local_1.json", "convA", "OLD title A", 100)
write_sess("local_2.json", "convA", "NEW title A", 200)          # newer lastActivityAt → this one wins
write_sess("local_3.json", "convB", "Title B", 50)
# convC has NO session record → must fall back to its uuid

titles = claudecode._sidebar_titles()
ck("convA resolves to the MOST RECENTLY active title", titles.get("convA") == "NEW title A")
ck("convB resolves to its title", titles.get("convB") == "Title B")
ck("convC (no record) is absent from the title map", "convC" not in titles)
ck("_conv_label uses the title when present", claudecode._conv_label("convA", titles) == "NEW title A")
ck("_conv_label falls back to the uuid when there is no title", claudecode._conv_label("convC", titles).startswith("convC"))

# ── seed est_chat rows: convA $300, convB $100, convC $50 ──
def seed(conv, mid, val, cr, ts):
    budget._record_spend_event("anthropic", "claude-haiku-4-5", "est_chat", float(val),
                               conv_id=conv, occurred_at=ts, in_tok=0, cache_read_tok=cr,
                               source="claude-code", project="claude-code", dedup_key="cc:" + mid)
seed("convA", "a1", 100, 500000, "2026-08-24T01:00:00+00:00")
seed("convA", "a2", 200, 600000, "2026-08-24T02:00:00+00:00")
seed("convB", "b1", 100, 100000, "2026-08-24T01:00:00+00:00")
seed("convC", "c1", 50, 50000, "2026-08-24T01:00:00+00:00")

def render():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        claudecode.conversations_cmd(top=10)
    return buf.getvalue()

out = render()
ck("view labels convA by its title and ranks it first (est $300 > $100 > $50)",
   "NEW title A" in out and "Title B" in out and out.index("NEW title A") < out.index("Title B"))
ck("convC (no title) is shown by its uuid", "convC" in out)
ck("before reconciliation, no reconciled overflow column (only the hint to run it)",
   "SEPARATE column" not in out and "$25.00" not in out)

# ── reconcile an overflow row for convA → the Real overflow column appears, SEPARATE from est-value ──
budget._record_spend_event("anthropic", "claude-haiku-4-5", "realtime", 25.0,
                           conv_id="convA", occurred_at="2026-08-24T00:00:00+00:00",
                           source="claude-code-overflow", project="claude-code", dedup_key="cc-of:w:convA:m")
out2 = render()
ck("once reconciled, Real overflow $ appears as a SEPARATE column (never summed into est-value)",
   "Real overflow $25.00" in out2 and "300.00 est" in out2)

print(("[OK]" if not fails else "[FAIL]") + " claude-code-conversations-titles: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
