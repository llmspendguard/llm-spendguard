"""Offline test for setup.py — install_rule (CLAUDE.md) + the cross-interpreter (path-injecting) hook body.
No network, no pip, no subprocess interpreter gating: pure string/file logic."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-test-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import setup

def check(label, ok):
    print(f"  [{'OK' if ok else 'FAIL'}] {label}")
    assert ok, label

print("-- install_rule: create / idempotent update / append-not-clobber --")
d = tempfile.mkdtemp(prefix="sg-rule-")
md = os.path.join(d, "CLAUDE.md")
setup.install_rule(d)
body1 = open(md).read()
check("rule block written", setup._RULE_BEGIN in body1 and setup._RULE_END in body1)
check("mentions spendguard.require()", "spendguard.require()" in body1)
setup.install_rule(d)                                   # re-run = update in place, no duplication
body2 = open(md).read()
check("idempotent: exactly one begin marker", body2.count(setup._RULE_BEGIN) == 1)
check("idempotent: exactly one end marker", body2.count(setup._RULE_END) == 1)

open(md, "w").write("# My Project\nExisting rules.\n")  # pre-existing content must survive
setup.install_rule(d)
body3 = open(md).read()
check("appends below existing content (not clobbered)", body3.startswith("# My Project\nExisting rules.")
      and setup._RULE_BEGIN in body3)


def _block(text):
    """The BEGIN..END managed region of a rule file, extracted mechanically (index of the fixed markers) — a
    structural slice, not a judgement about the content's meaning."""
    b = text.index(setup._RULE_BEGIN)
    return text[b:text.index(setup._RULE_END, b) + len(setup._RULE_END)]


print("-- install_rule writes ALL THREE assistant-rule files, block == canonical _RULE verbatim --")
d2 = tempfile.mkdtemp(prefix="sg-rule3-")
setup.install_rule(d2)
canonical = _block(setup._RULE)
for name in setup._RULE_TARGETS:                        # CLAUDE.md (Claude Code) · AGENTS.md (Codex/standard) · .cursorrules (Cursor)
    p = os.path.join(d2, name)
    body = open(p).read() if os.path.exists(p) else ""
    check(f"{name} written and its managed block is the canonical _RULE verbatim",
          setup._RULE_BEGIN in body and _block(body) == canonical)

print("-- overwriting a rule file PRESERVES user content outside the block + backs up the prior file --")
agents = os.path.join(d2, "AGENTS.md")
open(agents, "w").write("# Team AGENTS\nHand-written rule: always run tests.\n")   # no managed block → must append
setup.install_rule(d2)
ab = open(agents).read()
check("user's own AGENTS.md content survives verbatim (appended, not clobbered)",
      ab.startswith("# Team AGENTS\nHand-written rule: always run tests.") and _block(ab) == canonical)
from spendguard import config as _cfg
bdir = _cfg.HOME / "rule_backups"
check("a recoverable backup of the prior AGENTS.md was written before the overwrite",
      bdir.exists() and any(x.name.startswith("AGENTS.md.") for x in bdir.iterdir()))

print("-- cross-interpreter hook body is PATH-INJECTING (works with no pip / PEP668) --")
# The same .replace() install_hook() applies for the --user/--python path:
src = setup._pkg_src()
cross = setup._HOOK.replace("import spendguard\n        spendguard.install()",
                            f"sys.path.insert(0, {src!r})\n        import spendguard\n        spendguard.install()")
check("injects the package src onto sys.path", f"sys.path.insert(0, {src!r})" in cross)
check("still calls spendguard.install()", "spendguard.install()" in cross)
check("still honors the kill switch", "GATE_DISABLE" in cross and "disabled" in cross)
check("src path exists", os.path.isdir(src) and os.path.exists(os.path.join(src, "spendguard", "__init__.py")))

print("done.")
