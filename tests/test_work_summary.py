"""workdone caged summarizer — the pure prompt builder + the ESTIMATE-FIRST path (zero spend, no network). The
actual caged generation (run=True) makes gated LLM calls under caps.meta and isn't exercised here. Offline,
isolated home. Script-style."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-wsum-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import workdone

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── _summary_prompt: pure — includes project, commit subjects, top intents, the instruction; bounded ──
p = workdone._summary_prompt("lmm", ["fix the resolver", "add gap-fill"], {"extract": 5, "typing": 3})
ck("prompt names the project", "Project: lmm" in p)
ck("prompt includes commit subjects", "fix the resolver" in p and "add gap-fill" in p)
ck("prompt includes intents × counts", "extract×5" in p and "typing×3" in p)
ck("prompt asks for the accomplishment summary", "Summarize what was accomplished" in p)
big = workdone._summary_prompt("x", [f"commit {i}" for i in range(100)], {f"i{i}": i for i in range(50)})
ck("prompt caps commits at 30", big.count("- commit ") == 30)
ck("prompt caps intents at 12", big.count("×") <= 13)  # 12 intents (+ none elsewhere)

# ── the system prompt forbids secrets/PII (scrub by construction) ──
ck("system prompt forbids secrets/keys/paths/PII", "NO secrets" in workdone._SUMMARY_SYS and "PII" in workdone._SUMMARY_SYS)

# ── load_summaries: empty when no cache; reads the cache file ──
ck("load_summaries empty when no cache", workdone.load_summaries() == {})
json.dump({"lmm": "Shipped the resolver."}, open(workdone._summaries_path(), "w"))
ck("load_summaries reads the cached digest", workdone.load_summaries().get("lmm") == "Shipped the resolver.")

# ── summarize(run=False): ESTIMATE-ONLY — zero spend, no network, returns {projects, est_usd, model} ──
est = workdone.summarize(run=False)
ck("estimate path returns projects + est_usd + model (no spend)",
   "projects" in est and "est_usd" in est and "model" in est and "summarized" not in est)
ck("empty isolated home → 0 projects, $0 estimate (no git/corpus, no network)",
   est["projects"] == 0 and est["est_usd"] == 0.0)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"work_summary: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
