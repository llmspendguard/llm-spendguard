"""Cat-8 cache correctness — a corrupt cache row can't crash the lookup or return the wrong entry:

  * semcache._unpack — a truncated/corrupt embedding blob (length not a multiple of 4) made struct.unpack raise
    and crash the WHOLE semantic lookup on one bad row. It now returns None (the row is skipped by get()).
  * end-to-end: with one good and one corrupt-emb row, get(threshold>0) still finds the good match, never
    crashes, and never serves the corrupt row's output.

Offline, isolated home; _embed is stubbed (no network).
"""
import os
import sys
import tempfile
import struct

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-cat8-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import semcache          # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# ── _unpack tolerates corrupt/empty blobs ──────────────────────────────────────────────────────────────────────
ck("_unpack(None) → None", semcache._unpack(None) is None)
ck("_unpack(b'') → None", semcache._unpack(b"") is None)
ck("_unpack of a valid packed vector round-trips", semcache._unpack(struct.pack("2f", 1.0, 2.0)) == [1.0, 2.0])
ck("_unpack of a truncated blob (3 bytes) → None, not a crash", semcache._unpack(b"abc") is None)
ck("_unpack of a 5-byte blob → None, not a crash", semcache._unpack(b"abcde") is None)

# ── get(threshold>0) survives a corrupt-emb row and still finds the good semantic match ─────────────────────────
VEC = [1.0, 0.0, 0.0]
semcache._embed = lambda text: VEC                    # deterministic, offline
semcache.put("the good prompt", semcache._SCOPE_ANY, "GOOD-OUTPUT", store_embedding=True)
with semcache._lock:                                  # inject a corrupt-emb row (3-byte blob) under the same scope
    semcache._semcache_db().execute("INSERT INTO semcache VALUES (?,?,?,?,?,?,?)",
                           ("corrupt1", "2026-08-15", semcache._SCOPE_ANY, "hh", "junk prompt", "BAD-OUTPUT", b"abc"))
    semcache._semcache_db().commit()
try:
    hit = semcache.get_cached("a semantically-near prompt", "openai:gpt-x", threshold=0.5)
    crashed = False
except Exception:
    hit, crashed = None, True
ck("get(threshold>0) does not crash on a corrupt-emb row", not crashed)
ck("get(threshold>0) returns the GOOD match, never the corrupt row's output", hit == "GOOD-OUTPUT", f"got {hit!r}")

print(f"\n{'[FAIL]' if fails else 'OK'} test_cat8_cache_correctness: {fails} failure(s)")
sys.exit(1 if fails else 0)
