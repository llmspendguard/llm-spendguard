"""Coverage audit — flags LLM-calling venvs that aren't gated (the ungated realtime sources). Offline: builds mock
venv site-packages (openai/anthropic dirs + a spendguard sitecustomize) and asserts the audit classifies each
correctly. Zero spend, no real venvs touched."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cov-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from _script_checks import record_script_check  # noqa: E402

from spendguard import coverage

fails = []
def mk_venv(root, name, llm=(), gated=False):
    sp = os.path.join(root, name, "lib", "python3.11", "site-packages")
    os.makedirs(sp, exist_ok=True)
    for p in llm:
        os.makedirs(os.path.join(sp, p), exist_ok=True)
    if gated:
        with open(os.path.join(sp, "sitecustomize.py"), "w") as f:
            f.write("# spendguard-gate-hook/v1\nimport spendguard\nspendguard.install()\n")
    os.makedirs(os.path.join(root, name, "bin"), exist_ok=True)
    open(os.path.join(root, name, "bin", "python"), "a").close()
    return os.path.join(root, name)


tmp = tempfile.mkdtemp(prefix="sg-venvs-")
mk_venv(tmp, "gated-llm", llm=("openai", "anthropic"), gated=True)
mk_venv(tmp, "ungated-llm", llm=("anthropic",), gated=False)
mk_venv(tmp, "no-llm-tool", llm=(), gated=False)              # no LLM SDK → not a realtime source
mk_venv(tmp, "llm-spendguard-dev", llm=("openai",), gated=False)   # spendguard's OWN → self-gates, not a gap
ROOTS = [os.path.join(tmp, "*")]

# The audit asks each target interpreter for its real purelib. These are fabricated venvs, so provide that answer
# at the subprocess boundary rather than running them.
coverage._purelib = lambda venv: os.path.join(venv, "lib", "python3.11", "site-packages")

rows = coverage.audit_gating_coverage(roots=ROOTS)
by = {os.path.basename(r["venv"]): r for r in rows}
record_script_check(fails, "no-LLM venv excluded (no openai/anthropic SDK)", "no-llm-tool" not in by)
record_script_check(fails, "gated LLM venv → gated=True", by.get("gated-llm", {}).get("gated") is True)
record_script_check(fails, "ungated LLM venv → gated=False", by.get("ungated-llm", {}).get("gated") is False)
record_script_check(fails, "LLM list detected (anthropic)", by.get("ungated-llm", {}).get("llm") == ["anthropic"])
record_script_check(fails, "spendguard's own venv flagged own=True (self-gates via require())", by.get("llm-spendguard-dev", {}).get("own") is True)

g = coverage.gaps(roots=ROOTS)
record_script_check(fails, "gaps() = ONLY the ungated non-own LLM venv (own venv is NOT a gap)",
   [os.path.basename(r["venv"]) for r in g] == ["ungated-llm"])

print(("[OK]" if not fails else "[FAIL]") + " coverage: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
