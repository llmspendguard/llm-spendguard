"""comprehend — fan a corpus across the $0 lanes for doc-mining, INSTEAD of Claude-only sub-agents.

Offline + hermetic: exercises corpus loading, CONTAINED chunking (parts rejoin to the whole, nothing dropped),
task building, and the ZERO-SPEND estimate path (run=False). It never fans, never calls an LLM, never touches
the network — bulk_delegate is stubbed to FAIL if the estimate path ever reaches it. Cleans up.
"""
import os
import sys
import atexit
import shutil
import tempfile

_HOME = tempfile.mkdtemp(prefix="sg-comprehend-")
os.environ["SPENDGUARD_HOME"] = _HOME
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import comprehend   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    return [] if cond else [name]


_corp = tempfile.mkdtemp(prefix="sg-corp-")
atexit.register(shutil.rmtree, _corp, ignore_errors=True)
for _i, _body in enumerate(["alpha line\nbeta line\n", "gamma\n"]):
    with open(os.path.join(_corp, f"doc{_i}.md"), "w") as _f:
        _f.write(_body)

print("-- resolve_files: globs resolve, dedup, a nonexistent glob yields nothing --")
files = comprehend.resolve_files([os.path.join(_corp, "*.md")])
fails += ck("both docs resolved", len(files) == 2)
dup = comprehend.resolve_files([os.path.join(_corp, "*.md"), os.path.join(_corp, "doc0.md")])
fails += ck("a repeated path is de-duplicated", len(dup) == 2)
fails += ck("a nonexistent glob yields nothing (not a phantom file)",
            comprehend.resolve_files([os.path.join(_corp, "nope-*.md")]) == [])

print("-- chunk_contained: CONTAINMENT — parts rejoin to the whole, none over the cap --")
text = "".join(f"line {n}\n" for n in range(500))
parts = comprehend.chunk_contained(text, 100)
fails += ck("parts concatenate back to the EXACT original (nothing dropped or duplicated)",
            "".join(parts) == text)
fails += ck("every part is within the cap and there is more than one", all(len(p) <= 100 for p in parts) and len(parts) > 1)
fails += ck("text under the cap stays a single part", comprehend.chunk_contained("short\n", 100) == ["short\n"])

print("-- build_tasks: one task per small file; a large file splits into contained parts; unreadable surfaced --")
tasks = comprehend.build_tasks(files, "What is this?")
fails += ck("one task per small file, each carrying its file's whole text",
            len(tasks) == 2 and all(t["parts"] == 1 and t["prompt"] for t in tasks))
_big = os.path.join(_corp, "big.md")
with open(_big, "w") as _f:
    _f.write("x" * 250)
bt = comprehend.build_tasks([_big], "Q", max_chars=100)
fails += ck("a file over max_chars becomes multiple contained parts with UNIQUE keys",
            len(bt) == 3 and len({t["key"] for t in bt}) == 3 and all(t["prompt"] for t in bt))
missing = comprehend.build_tasks([os.path.join(_corp, "gone.md")], "Q")
fails += ck("an unreadable file becomes an ERROR task (surfaced, no prompt), never silently dropped",
            len(missing) == 1 and missing[0]["prompt"] is None and bool(missing[0].get("error")))

print("-- comprehend(run=False): ZERO-SPEND estimate, measurement-based, and it must NOT fan --")
import spendguard.lane_balance as _lb   # noqa: E402


def _no_fan(*a, **k):
    raise AssertionError("bulk_delegate must NOT run in estimate mode (run=False)")


_orig = _lb.bulk_delegate
_lb.bulk_delegate = _no_fan
try:
    res = comprehend.comprehend_corpus([os.path.join(_corp, "*.md")], intent="test:doc-mining", model="claude-opus-5")
finally:
    _lb.bulk_delegate = _orig
fails += ck("estimate mode did not fan (no spend)", res.get("ran") is False)
fails += ck("estimate is measurement-based: in_tok scales with the real corpus size",
            res["estimate"]["in_tok"] > 0 and res["estimate"]["tasks"] >= 2)
fails += ck("no matching files → a clear error, not a phantom success",
            bool(comprehend.comprehend_corpus([os.path.join(_corp, "none-*.md")], intent="test:doc-mining").get("error")))

print("-- default_checkpoint: STABLE per-intent (so a re-run RESUMES), safe filename under the home --")
c1 = comprehend.default_checkpoint("test:doc mining/v2")
c2 = comprehend.default_checkpoint("test:doc mining/v2")
fails += ck("stable across calls (same intent → same path → resume, never re-pay)", c1 == c2)
fails += ck("path is under the spendguard home and safely named",
            c1.startswith(_HOME) and c1.endswith(".jsonl") and "/" not in os.path.basename(c1))

print(f"\n{'[FAIL]' if fails else 'OK'} test_comprehend: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
