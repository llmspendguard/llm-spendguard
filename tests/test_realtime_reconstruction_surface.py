"""RealtimeSource surfaces the ADMIN-FREE reconstruction as an ESTIMATE — not a misleading 'bill unreadable' UNKNOWN.

MEASURED gap (2026-09-17): `spendguard reconcile all` printed "realtime truth UNKNOWN — the account/provider bill could
not be read (key/network)" even though spendguard's whole realtime premise is reconstructing it WITHOUT an admin key
(admin is a dev-only cross-check; `reconcile_realtime` deleted the admin path on purpose). This pins the corrected
surface: a reconstruction cache → reconcile reads ESTIMATED (reconstructed); a STALE cache says so accurately; a CORRUPT
cache is a FAILURE (never masked as absence); and a completeness verdict with an estimated realtime source is NOT
'INCOMPLETE — UNKNOWN'. Offline + hermetic: writes a fake reconstruction cache under SPENDGUARD_HOME, no network."""
import json
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-rt-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import ledger_sync, reconcile, config   # noqa: E402

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


_CACHE = str(config.HOME / "realtime_reconstruction.json")


def _write_cache(rows, since="2026-06-01"):
    with open(_CACHE, "w") as f:
        json.dump({"since": since, "total": sum(r["usd"] for r in rows), "rows": rows}, f)


def _rm_cache():
    if os.path.exists(_CACHE):
        os.remove(_CACHE)


print("-- realtime_reconstruction_estimate: absence vs corrupt vs fresh vs stale (a corrupt cache is NOT absence) --")
_rm_cache()
ck("no cache -> None (the find has never run)", ledger_sync.realtime_reconstruction_estimate("2026-09-01") is None)

with open(_CACHE, "w") as f:
    f.write('{"rows": [')                                  # truncated JSON
_bad = ledger_sync.realtime_reconstruction_estimate("2026-09-01")
ck("corrupt cache -> {'error': …} (a dependency FAILURE, never masked as absence)",
   isinstance(_bad, dict) and bool(_bad.get("error")))

_write_cache([{"day": "2026-09-05", "usd": 12.0, "org": "ensight"},
              {"day": "2026-09-06", "usd": 8.0, "org": "healiom"},
              {"day": "2026-08-20", "usd": 99.0, "org": "ensight"},   # out of window
              {"day": "2026-09-07", "usd": 0.0, "org": "ensight"}])   # nonpositive
_fresh = ledger_sync.realtime_reconstruction_estimate("2026-09-01")
ck("fresh cache covers the window", _fresh["covers_window"] is True)
ck("window total sums only in-window POSITIVE rows (12+8=20)", _fresh["total"] == 20.0)
ck("out-of-window + nonpositive rows are COUNTED, never dropped silently",
   _fresh["dropped"]["out_of_window"] == 1 and _fresh["dropped"]["nonpositive"] == 1)
ck("by_project maps org->project (ensight->llm-spendguard, healiom->lmm)",
   {d["project"] for d in _fresh["by_project"]} == {"llm-spendguard", "lmm"})

_write_cache([{"day": "2026-07-15", "usd": 50.0, "org": "ensight"}])
_stale = ledger_sync.realtime_reconstruction_estimate("2026-09-01")
ck("cache whose newest row predates the window -> covers_window False (STALE)", _stale["covers_window"] is False)

print("-- RealtimeSource.truth_total: None truth, but an ESTIMATE + note (admin-free), never a fake bill --")
os.environ.pop("SPENDGUARD_ADMIN_ORACLE", None)
_write_cache([{"day": "2026-09-05", "usd": 20.0, "org": "ensight"}])
_src = ledger_sync.RealtimeSource(conn={}, since="2026-09-01")
ck("truth_total stays None without an admin key (no verified bill)", _src.truth_total("2026-09-01") is None)
ck("but a reconstructed ESTIMATE is surfaced ($20)", _src.estimate is not None and _src.estimate["total"] == 20.0)
ck("the note says 'reconstructed estimate' (not 'bill could not be read')",
   "RECONSTRUCTED estimate" in (_src.note or ""))

_rm_cache()
_src2 = ledger_sync.RealtimeSource(conn={}, since="2026-09-01")
ck("no cache -> None truth, no estimate, a 'run the find' note",
   _src2.truth_total("2026-09-01") is None and _src2.estimate is None and "run" in (_src2.note or "").lower())

print("-- reconcile.completeness: an estimated realtime source is NOT 'unknown/INCOMPLETE' --")
_res = {"realtime": {"truth_total": None, "captured": 200.0, "residual": None,
                     "estimate": {"total": 20.0, "by_project": []}, "note": "reconstructed"}}
_comp = reconcile.completeness(_res)
ck("an estimated source does NOT sink completeness (complete stays True)", _comp["complete"] is True)
ck("the verdict names it ESTIMATED (reconstructed), not UNKNOWN",
   "ESTIMATED" in _comp["msg"] and "UNKNOWN" not in _comp["msg"].upper())
ck("the source status is 'estimated'", _comp["sources"]["realtime"]["status"] == "estimated")

_res2 = {"realtime": {"truth_total": None, "captured": 200.0, "residual": None, "estimate": None}}
_comp2 = reconcile.completeness(_res2)
ck("truth None AND no reconstruction -> UNKNOWN, still sinks completeness",
   _comp2["complete"] is False and _comp2["sources"]["realtime"]["status"] == "unknown")

print(f"\n{'[FAIL]' if fails else 'OK'} test_realtime_reconstruction_surface: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
