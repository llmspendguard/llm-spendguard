"""Coverage audit — flags LLM-calling venvs that aren't gated (the ungated realtime sources). Offline: builds mock
venv site-packages (openai/anthropic dirs + a spendguard sitecustomize) and asserts the audit classifies each
correctly. Zero spend, no real venvs touched."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cov-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import coverage

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


def mk_venv(root, name, llm=(), gated=False):
    sp = os.path.join(root, name, "lib", "python3.11", "site-packages")
    os.makedirs(sp, exist_ok=True)
    for p in llm:
        os.makedirs(os.path.join(sp, p), exist_ok=True)
    if gated:
        with open(os.path.join(sp, "sitecustomize.py"), "w") as f:
            f.write("import spendguard\nspendguard.install()\n")
    return os.path.join(root, name)


tmp = tempfile.mkdtemp(prefix="sg-venvs-")
mk_venv(tmp, "gated-llm", llm=("openai", "anthropic"), gated=True)
mk_venv(tmp, "ungated-llm", llm=("anthropic",), gated=False)
mk_venv(tmp, "no-llm-tool", llm=(), gated=False)              # no LLM SDK → not a realtime source
mk_venv(tmp, "llm-spendguard-dev", llm=("openai",), gated=False)   # spendguard's OWN → self-gates, not a gap
ROOTS = [os.path.join(tmp, "*")]

rows = coverage.audit(roots=ROOTS)
by = {os.path.basename(r["venv"]): r for r in rows}
ck("no-LLM venv excluded (no openai/anthropic SDK)", "no-llm-tool" not in by)
ck("gated LLM venv → gated=True", by.get("gated-llm", {}).get("gated") is True)
ck("ungated LLM venv → gated=False", by.get("ungated-llm", {}).get("gated") is False)
ck("LLM list detected (anthropic)", by.get("ungated-llm", {}).get("llm") == ["anthropic"])
ck("spendguard's own venv flagged own=True (self-gates via require())", by.get("llm-spendguard-dev", {}).get("own") is True)

g = coverage.gaps(roots=ROOTS)
ck("gaps() = ONLY the ungated non-own LLM venv (own venv is NOT a gap)",
   [os.path.basename(r["venv"]) for r in g] == ["ungated-llm"])

print(("[OK]" if not fails else "[FAIL]") + " coverage: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
