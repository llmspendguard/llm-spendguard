"""llm_files — the INPUT-completeness twin of the max_tokens/output guard in adapters.py.

Proves the three guarantees the feature exists for:
  (a) attach_whole emits EVERY source line with a correct byte/sha/COMPLETE stamp the model also sees;
  (b) the sanctioned path CANNOT yield a truncated block — attach_whole takes a PATH with no line-limit knob,
      its numbering is byte-exact reversible, and adapters.call(files=[…]) folds the WHOLE stamped block into
      the prompt ahead of the question;
  (c) a rendering that drops a line RAISES PartialFileError (fails closed) instead of shipping a starved prompt,
      and an absent path RAISES FileNotFoundError (a missing file is not an empty one).
Pure/offline — no LLM call, no network. The `files=` wiring is checked against a stubbed _call_once.
"""
import os
import sys
import tempfile
import hashlib
import inspect

os.environ.setdefault("SPENDGUARD_HOME", tempfile.mkdtemp(prefix="sg-llmfiles-"))
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")   # this test exercises file assembly, not the SDK gate

from spendguard import llm_files as lf
from spendguard import adapters


def ck(name, cond):
    """Print the result and RETURN it as a (possibly empty) list of failure names. The caller accumulates at
    module scope, so this function only reads — it never writes shared state."""
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    return [] if ok else [name]


def _raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


# Adversarial content on purpose: a line ("al") that is a SUBSTRING of another ("alpha") — so a substring
# completeness test would false-pass; a blank line; a line carrying the numbering delimiter " | " itself; and
# NO trailing newline on the last line. Byte-exact reconstruction must survive all four.
SRC = "alpha\nal\n\nx | y | z\nlast-no-newline"
d = tempfile.mkdtemp(prefix="sg-llmfiles-src-")
p = os.path.join(d, "sample.py")
with open(p, "wb") as fh:
    fh.write(SRC.encode("utf-8"))
N_LINES = len(SRC.splitlines(keepends=True))            # computed, never hardcoded
N_BYTES = len(SRC.encode("utf-8"))
SHA12 = hashlib.sha256(SRC.encode("utf-8")).hexdigest()[:12]

fails = []

print("-- (a) attach_whole: every line present + correct byte/sha/COMPLETE stamp --")
block, man = lf.attach_whole(p)
fails += ck("header stamps the real line count, byte count, sha256:<12>, and COMPLETE",
            f"({N_LINES} lines, {N_BYTES} bytes, sha256:{SHA12}, COMPLETE)" in block)
fails += ck("every source line appears in the block",
            all(ln in block for ln in ("alpha", "al", "x | y | z", "last-no-newline")))
fails += ck("manifest is the checked record (path/sha/lines/bytes)",
            man == {"path": p, "sha256": SHA12, "lines": N_LINES, "bytes": N_BYTES})
fails += ck("footer closes the block on its own line (no run-on onto a newline-less last line)",
            f"--- END sample.py ({N_LINES} lines) ---" in block and block.rstrip().endswith("---"))

print("-- (b) the sanctioned path cannot truncate: reversible numbering + no line-limit knob + files= wiring --")
# Strip the '    N | ' numbering back off every rendered line and rejoin — must equal the source BYTE-FOR-BYTE.
# This is the exact invariant attach_whole enforces before returning; here we assert it holds for the hard content.
_rendered = lf._render_lines(SRC.splitlines(keepends=True), True)
_recon = "".join(r.partition(" | ")[2] for r in _rendered)
fails += ck("numbering is byte-exact reversible (substring/blank/'|'/no-EOL-newline all survive)", _recon == SRC)
fails += ck("attach_whole exposes NO truncation knob — only (path, line_numbered)",
            list(inspect.signature(lf.attach_whole).parameters) == ["path", "line_numbered"])
# no silent cap on line COUNT: a large file stamps all its lines and carries its last one.
BIG = "".join(f"row-{i}\n" for i in range(4000))
pbig = os.path.join(d, "big.py")
with open(pbig, "wb") as fh:
    fh.write(BIG.encode("utf-8"))
_bblk, _bman = lf.attach_whole(pbig)
fails += ck("large file: all lines stamped (no silent cap), first and last present",
            _bman["lines"] == 4000 and "    1 | row-0\n" in _bblk and "4000 | row-3999" in _bblk)

# adapters.call(files=[…]) must fold the WHOLE stamped block into the prompt, ahead of the question. Stub the
# raw sender so nothing leaves the process; capture what it would have been asked to send.
captured = {}
def _fake_once(model, prompt, max_tokens=None, system=None, reasoning=None, schema=None, timeout_s=None, _skip_lane=False):
    captured["prompt"] = prompt
    return {"text": "ok", "error": None}
adapters._call_once = _fake_once
adapters.call("gpt-x", "QUESTION-XYZ", max_tokens=16, files=[p], _no_guard=True)
_pr = captured.get("prompt", "")
fails += ck("call(files=[…]) assembles the stamped whole-file block AND keeps the question",
            f"sha256:{SHA12}, COMPLETE" in _pr and "QUESTION-XYZ" in _pr and "last-no-newline" in _pr)
fails += ck("file block precedes the question in the assembled prompt",
            "FILE: sample.py" in _pr and _pr.index("FILE: sample.py") < _pr.index("QUESTION-XYZ"))

print("-- (c) fail closed: a dropped line raises PartialFileError; an absent path raises FileNotFoundError --")
_orig_render = lf._render_lines
lf._render_lines = lambda lines, line_numbered: _orig_render(lines, line_numbered)[:-1]   # inject a dropped last line
fails += ck("a rendering that drops a line RAISES PartialFileError (never ships a starved block)",
            _raises(lambda: lf.attach_whole(p), lf.PartialFileError))
lf._render_lines = _orig_render
fails += ck("absent path RAISES FileNotFoundError (missing != empty)",
            _raises(lambda: lf.attach_whole(os.path.join(d, "does-not-exist.py")), FileNotFoundError))

print("-- attach_many: stamps every file, and fails closed if ANY is absent --")
p2 = os.path.join(d, "second.txt")
with open(p2, "wb") as fh:
    fh.write(b"one\ntwo\n")
_mblk, _mans = lf.attach_many([p, p2])
fails += ck("attach_many includes + stamps every file", len(_mans) == 2 and "sample.py" in _mblk and "second.txt" in _mblk)
fails += ck("attach_many fails closed if any path is absent (never silently drops it)",
            _raises(lambda: lf.attach_many([p, os.path.join(d, "nope.txt")]), FileNotFoundError))

print(f"\n{'[FAIL]' if fails else 'OK'} test_llm_files: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
