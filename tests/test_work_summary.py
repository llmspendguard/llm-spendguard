"""workdone caged summarizer — the pure prompt builder + the ESTIMATE-FIRST path (zero spend, no network). The
actual caged generation (run=True) makes gated LLM calls under caps.meta and isn't exercised here. Offline,
isolated home. Script-style."""
import os, sys, tempfile, json

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-wsum-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check
from spendguard import conv, workdone
from unittest.mock import patch

fails = []
# ── _summary_prompt: pure — includes project, commit subjects, top intents, the instruction; bounded ──
p = workdone._summary_prompt("lmm", ["fix the resolver", "add gap-fill"], {"extract": 5, "typing": 3})
record_script_check(fails, "prompt names the project", "Project: lmm" in p)
record_script_check(fails, "prompt includes commit subjects", "fix the resolver" in p and "add gap-fill" in p)
record_script_check(fails, "prompt includes intents × counts", "extract×5" in p and "typing×3" in p)
record_script_check(fails, "prompt asks for the accomplishment summary", "Summarize what was accomplished" in p)
big = workdone._summary_prompt("x", [f"commit {i}" for i in range(100)], {f"i{i}": i for i in range(50)})
record_script_check(fails, "prompt caps commits at 30", big.count("- commit ") == 30)
record_script_check(fails, "prompt caps intents at 12", big.count("×") <= 13)  # 12 intents (+ none elsewhere)

# ── the system prompt forbids secrets/PII (scrub by construction) ──
record_script_check(fails, "system prompt forbids secrets/keys/paths/PII", "NO secrets" in workdone._SUMMARY_SYS and "PII" in workdone._SUMMARY_SYS)

# ── load_summaries: empty when no cache; reads the cache file ──
record_script_check(fails, "load_summaries empty when no cache", workdone.load_summaries() == {})
json.dump({"lmm": "Shipped the resolver."}, open(workdone._summaries_path(), "w"))
record_script_check(fails, "load_summaries reads the cached digest", workdone.load_summaries().get("lmm") == "Shipped the resolver.")

# ── summarize(run=False): ESTIMATE-ONLY — zero spend, no network, returns {projects, est_usd, model} ──
# SPENDGUARD_HOME isolates the ledger, but conv's transcript input is separately
# rooted in the user's home. Keep REAL aggregation over an empty corpus fixture.
with tempfile.TemporaryDirectory(prefix="sg-summary-corpus-") as summary_corpus:
    with patch.object(conv, "_DEFAULT_TDIR", summary_corpus):
        est = workdone.summarize(run=False)
record_script_check(fails, "estimate path returns projects + est_usd + model (no spend)",
   "projects" in est and "est_usd" in est and "model" in est and "summarized" not in est)
record_script_check(fails, "empty isolated home → 0 projects, $0 estimate (no git/corpus, no network)",
   est["projects"] == 0 and est["est_usd"] == 0.0)

print(("\n[FAIL] " if fails else "\n[OK] ") + f"work_summary: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
