"""config.update_json stages each write in a UNIQUE tmp file — the fix for the measured resource_state corruption.

update_json writes `tmp`, then os.replace(tmp, path). The replace is atomic, but the tmp name used to be a FIXED
`<name>.tmp` SHARED by every process writing that path. resource_state._save fires on the hot dispatch path from many
concurrent processes (scheduled reconcile, lane-drain, MCP servers, hook subprocesses) and the per-process lock does
not reach across them: two writers opened the same `.tmp`, their writes interleaved, and os.replace promoted the
torn bytes → the next reader couldn't parse it and quarantined it. That produced 140 resource_state_state.json.corrupt.*
copies (2026-10-09). A per-writer staging name (pid + random) makes every os.replace promote a COMPLETE file.

Guards: (1) two writes to one path use DISTINCT staging files, not the old fixed `<name>.tmp`; (2) many concurrent
writers never yield a torn/unparseable read, produce no `.corrupt` quarantine copy, and leak no `.tmp`. Offline,
isolated HOME, no network."""
import os
import sys
import json
import threading
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-tmpiso-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import config  # noqa: E402

fails = []
def ck(label, cond):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)

HOME = config.HOME

# ── 1. DISTINCT staging file per write (capture the tmp handed to os.replace) ──
P = HOME / "iso_state.json"
seen = []
_orig_replace = os.replace
def _rec_replace(src, dst):
    seen.append(str(src))
    return _orig_replace(src, dst)
os.replace = _rec_replace
try:
    config.update_json(P, lambda d: {"n": 1}, reason="t")
    config.update_json(P, lambda d: {"n": 2}, reason="t")
finally:
    os.replace = _orig_replace
ck("two writes to one path use DISTINCT staging files", len(seen) == 2 and seen[0] != seen[1])
ck("staging name is NOT the old shared fixed <name>.tmp", not any(s.endswith(P.name + ".tmp") for s in seen))
ck("the final file holds the last write and parses", json.loads(P.read_text()).get("n") == 2)

# ── 2. concurrent writers: always parseable, no quarantine, no leaked tmp ──
Q = HOME / "concurrent_state.json"
Q.write_text('{"start": 0}')
errs = []
def _hammer(wid):
    try:
        for i in range(40):
            config.update_json(Q, lambda d, _w=wid, _i=i: {"w": _w, "i": _i}, reason="t")
            json.loads(Q.read_text())              # a torn write would raise here
    except Exception as e:                         # noqa: BLE001 — a parse/IO failure is exactly the bug under test
        errs.append(repr(e))
threads = [threading.Thread(target=_hammer, args=(w,)) for w in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()
ck("8 concurrent writers × 40: every read parsed (no torn promote)", not errs)
ck("no .corrupt quarantine copies were produced", not list(HOME.glob("concurrent_state.json.corrupt.*")))
ck("no .tmp staging file leaked", not list(HOME.glob("concurrent_state.json.*.tmp")))

print(f"\n{'[FAIL]' if fails else 'OK'} test_update_json_concurrent_tmp_isolation: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
