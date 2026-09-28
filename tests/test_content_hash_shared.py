"""Guard for the shared content-hash consolidation (config.content_hash): conv._chash and measurement.item_id /
_meas_sha were two independent truncated-SHA-256 implementations; they now route through the ONE primitive. This is
ONLY correct if it is OUTPUT-PRESERVING — a changed id RE-KEYS durable rows (conv's evidence cache + verdict lookups;
measurement's stored sample_ids / reading recovery). So this test pins each function's output against the ORIGINAL
formula computed from first principles here (raw hashlib, not the impl), for text, empty, None, and unicode — a future
change to config.content_hash or a caller's byte-encoding/length fails loudly instead of silently orphaning rows.

Offline, $0. Isolated SPENDGUARD_HOME.
"""
import hashlib
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-content-hash-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config, conv, measurement    # noqa: E402


def _orig_chash(text):                                # conv._chash BEFORE the refactor: raw UTF-8, strict, 20 hex
    return hashlib.sha256((text or "").encode()).hexdigest()[:20]


def _orig_meas_sha(*parts):                           # measurement._meas_sha BEFORE: \x00-domain-sep, replace, full hex
    h = hashlib.sha256()
    for p in parts:
        h.update(("\x00" + str(p)).encode("utf-8", "replace"))
    return h.hexdigest()


def _orig_item_id(text):                              # measurement.item_id BEFORE: _meas_sha[:16]
    return _orig_meas_sha(text)[:16]


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    cases = ["hello world", "", "x", "café ☕ — unicode", "a" * 5000, "line1\nline2\ttab", None]

    print("-- conv._chash is byte-identical to the original raw/strict/20-hex formula --")
    for t in cases:
        ck("_chash(%r)" % (t if not isinstance(t, str) or len(t) < 20 else t[:12] + "…",),
           conv._chash(t) == _orig_chash(t), (conv._chash(t), _orig_chash(t)))
        ck("_chash length is 20", len(conv._chash(t)) == 20)

    print("\n-- measurement.item_id is byte-identical to the original \\x00/replace/16-hex formula --")
    for t in cases:
        ck("item_id(%r)" % (t if not isinstance(t, str) or len(t) < 20 else t[:12] + "…",),
           measurement.item_id(t) == _orig_item_id(t), (measurement.item_id(t), _orig_item_id(t)))
        ck("item_id length is 16", len(measurement.item_id(t)) == 16)

    print("\n-- measurement._meas_sha (multi-part, used for instrument/sample/reading ids) is unchanged --")
    for parts in [("intent", "kind", "judge-mix"), ("one",), (), ("a", None, 3, "b")]:
        ck("_meas_sha%r" % (parts,), measurement._meas_sha(*parts) == _orig_meas_sha(*parts),
           (measurement._meas_sha(*parts), _orig_meas_sha(*parts)))
    ck("_meas_sha returns the full 64-char hex", len(measurement._meas_sha("x")) == 64)

    print("\n-- the two id families are DISTINCT (domain separation matters: they must NOT collide) --")
    ck("_chash and item_id differ for the same text (different encoding+length)",
       conv._chash("same") != measurement.item_id("same"))

    print("\n-- config.content_hash primitive: sha256 hexdigest truncated to length --")
    ck("content_hash(b'abc', 12) == sha256('abc')[:12]",
       config.content_hash(b"abc", 12) == hashlib.sha256(b"abc").hexdigest()[:12])
    ck("content_hash(b'', 64) == full sha256 of empty",
       config.content_hash(b"", 64) == hashlib.sha256(b"").hexdigest())

    print(f"\n{'[FAIL]' if fails else 'OK'} test_content_hash_shared: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
