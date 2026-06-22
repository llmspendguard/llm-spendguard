"""conv.py + claudecode.py pure transforms — already separated from the file I/O (_scan_new_lines / _events_in do
the reads), but previously untested: project attribution from text/cwd, the cost-from-usage math (incl. cache
tokens), and the score-ranked dedup. Offline, isolated home. Script-style. (Module 3 of the decoupling follow-up.)"""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-tpure-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import conv, claudecode, pricing

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── conv._project_of: text → project via the rule patterns (case-insensitive); no match → "" ──
ck("conv project: 'extract entities from the corpus' → nlp-pipeline", conv._project_of("Extract entities from the CORPUS") == "nlp-pipeline")
ck("conv project: 'caption each video frame' → vision-pipeline", conv._project_of("caption each video frame") == "vision-pipeline")
ck("conv project: unrelated text → '' (unclassified)", conv._project_of("schedule a meeting about budgets") == "")
ck("conv project: empty/None → ''", conv._project_of("") == "" and conv._project_of(None) == "")

# ── conv._dedup_top: rank by _score (desc), drop near-identical text, cap at k ──
events = [
    {"text": "low signal note", "role": "assistant"},
    {"text": "the user said it cost $40", "role": "user", "costs": ["$40"], "runs": True},   # high score
    {"text": "the user said it cost $40", "role": "user", "costs": ["$40"], "runs": True},   # DUPLICATE text
    {"text": "a distinct medium event", "sigs": ["x"], "role": "user"},
]
top = conv._dedup_top(events, k=10)
texts = [e["text"] for e in top]
ck("conv dedup: identical text collapses to one", texts.count("the user said it cost $40") == 1)
ck("conv dedup: highest-score event ranks first", top[0]["text"] == "the user said it cost $40")
ck("conv dedup: caps at k", len(conv._dedup_top(events, k=1)) == 1)

# ── claudecode._row_cost: $ from a usage dict, counting cache-read + cache-creation as input; returns (cost,in,out) ──
u = {"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 100}
cost, tin, tout = claudecode._row_cost("claude-opus-4-8", u)
exp = pricing.realtime_cost("claude-opus-4-8", 1000 + 100 + 200, 500, 200)
ck("claudecode cost: matches pricing.realtime_cost (cache tokens count as input)", abs(cost - exp) < 1e-12)
ck("claudecode cost: total in = input + cache_creation + cache_read, out passthrough", tin == 1300 and tout == 500)
cost0, tin0, _ = claudecode._row_cost("totally-unknown-model", u)
ck("claudecode cost: unknown model → $0 (never crashes), tokens still counted", cost0 == 0.0 and tin0 == 1300)

# ── claudecode._project_of: cwd basename → project; empty → the 'claude-code' default ──
ck("claudecode project: basename of cwd", claudecode._project_of("/Users/me/Documents/lmm") == "lmm")
ck("claudecode project: trailing slash handled", claudecode._project_of("/Users/me/slide-recon/") == "slide-recon")
ck("claudecode project: empty → 'claude-code'", claudecode._project_of("") == "claude-code")

print(("\n[FAIL] " if fails else "\n[OK] ") + f"transcript_pure: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
