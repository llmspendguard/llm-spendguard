"""CI actions must pin what actually RUNS, not just the wrapper that runs it.

On 2026-08-06 the secrets job went red with no repo change. The TruffleHog action was SHA-pinned to v3.95.6 —
but the action pulls `ghcr.io/trufflesecurity/trufflehog:latest`, so the SCANNER floated to 3.96.0 by itself.
3.96.0 then reported 4 "verified" Lob credentials that were pytest FUNCTION NAMES: Lob's key format is
`test_` + 35 characters, and `test_rollup_no_filter_sends_all_projects` is exactly that shape.

Two lessons, both pinned here:
  • a SHA on the wrapper is FALSE CONFIDENCE when the wrapper pulls :latest — pin the thing that executes;
  • "verified" is not proof: the tool claimed it verified a function name against Lob's live API.

This test reads the workflow, so it fails if either pin is dropped.
"""
import sys, re, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows" / "security.yml"

failures = 0
def check(label, cond, extra=""):
    global failures
    ok = bool(cond)
    if not ok:
        failures += 1
    print(f"  [{'OK' if ok else 'FAIL'}] {label}{('  — ' + extra) if extra and not ok else ''}")


s = WF.read_text()
print("-- the scanner itself is pinned, not only the action wrapping it --")
check("the action is SHA-pinned", re.search(r"trufflesecurity/trufflehog@[0-9a-f]{40}", s) is not None)
ver = re.search(r"version:\s*([0-9]+\.[0-9]+\.[0-9]+)", s)
check("the SCANNER version is pinned (the miss that broke the job)", ver is not None,
      "no `version:` — the action will pull :latest and drift")
if ver:
    sha_comment = re.search(r"trufflehog@[0-9a-f]{40}\s*#\s*v?([0-9]+\.[0-9]+\.[0-9]+)", s)
    check("…and it matches the version the SHA claims", sha_comment and sha_comment.group(1) == ver.group(1),
          f"comment says v{sha_comment.group(1) if sha_comment else '?'}, version: says {ver.group(1)}")

print("-- the Lob exclusion is present AND justified in place --")
check("lob is excluded", "--exclude-detectors=lob" in s)
check("the reason is written where the next person will read it",
      "pytest FUNCTION NAMES" in s and "no Lob dependency" in s)
check("and the weaker meaning of --only-verified is recorded", '"verified" is not proof' in s)

print("-- the false positive is REAL and reproducible, not a story --")
pat = re.compile(r"\btest_[A-Za-z0-9_]{35}\b")          # Lob's shape: test_ + exactly 35
# Skip THIS file: it quotes the offending name in its own docstring on purpose, and the scanner correctly
# matches it — a guard that documents a pattern will always contain the pattern.
hits = []
for f in sorted((ROOT / "tests").glob("*.py")):
    if f.name == pathlib.Path(__file__).name:
        continue
    for i, ln in enumerate(f.read_text().splitlines(), 1):
        hits += [f"{f.name}:{i}" for _ in pat.findall(ln)]
check("test names of exactly Lob's shape still exist (so the exclusion is still needed)", hits, str(hits))
check("…and they are function definitions, not credentials",
      all("def " in (ROOT / "tests" / h.split(":")[0]).read_text().splitlines()[int(h.split(":")[1]) - 1]
          for h in hits))

print("-- no real Lob key is hiding behind the exclusion --")
real = re.compile(r"\b(live|test)_[0-9a-f]{32,40}\b")   # a genuine key is HEX, not words with underscores
found = [str(f) for f in ROOT.rglob("*.py") if ".venv" not in str(f) and real.search(f.read_text())]
check("no hex-shaped Lob credential anywhere in the tree", not found, str(found[:3]))

print(f"\n{'[FAIL]' if failures else 'OK'} test_ci_pins: {failures} failure(s)")
sys.exit(1 if failures else 0)
