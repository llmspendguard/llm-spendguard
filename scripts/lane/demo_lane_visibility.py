"""Demo — what the lane-visibility feature puts in front of you. Renders the three receipt surfaces (the one-line
Stop-hook widget, the footer, the full `spendguard receipt` table) against a SEEDED, ISOLATED home so it is safe to
run anywhere and produces the same output every time. NOT a test (no asserts) — a look at the feature.

    python scripts/lane/demo_lane_visibility.py
"""
import os
import sys
import json
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-lanedemo-")   # ISOLATED — never touches ~/.spendguard
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ["SPENDGUARD_NO_AUTOINSTALL"] = "1"
os.environ["SPENDGUARD_CALLS"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

from spendguard import receipt, calls, budget   # noqa: E402

TODAY = receipt._windows()[0]

# 1) est-value per lane (what each plan DELIVERED) — claude-code carries the bulk, the others a little.
for src, usd in (("claude-code", 6565.90), ("codex", 210.40), ("gemini", 0.50), ("zai-coding", 0.04)):
    receipt.stamp_est_value([{"day": TODAY, "spend_micros": round(usd * 1_000_000), "billed": False,
                              "org": "healiom", "team": "eng", "project": "llm-spendguard"}], source=src)

# 2) which lane SERVED spendguard's own work this month (the ledger rows the receipt counts)
for ex, prov, model, n in (("codex", "openai", "gpt-5.5", 12), ("gemini", "gemini", "gemini-3.7-flash", 3),
                           ("zai-coding", "zai", "glm-5.3", 5), ("claude-code", "anthropic", "claude-opus-4-8", 20)):
    for _ in range(n):
        calls.record(prov, model, "subscription", 0.0, in_tok=8000, out_tok=1200, executor=ex, project="llm-spendguard")

# 3) realistic real-$ axis (billed API + a $400 plan fee) so the two axes show side by side
budget.spent_since = lambda day, project=None, conv=None: {TODAY: 4.20, receipt._windows()[1]: 31.5,
                                                           receipt._windows()[2]: 2189.0}.get(day, 0.0)
receipt._plan_usd = lambda: (400.0, False)

t = receipt.tally()
print("\n" + "=" * 100)
print("1) THE INLINE ONE-LINE WIDGET  (Claude Code Stop-hook `systemMessage` — what you see each turn on desktop):")
print("=" * 100)
print("   " + receipt.render_line(t))
print("\n" + "=" * 100)
print("2) THE FOOTER  (spendguard receipt --footer / per-flow tail):")
print("=" * 100)
print(receipt.render_tally(t))
print("\n" + "=" * 100)
print("3) THE FULL RECEIPT  (spendguard receipt):")
print("=" * 100)
print(receipt.render_tree())
print()
