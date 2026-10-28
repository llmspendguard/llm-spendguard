"""Guard: overage.current_overage_status is TTL-cached (in-process + disk), self-describing, and hermetic under
test isolation.

Why it matters: the real probe (_compute_overage_status → claudecode._overage_events) re-reads the whole
claude-code transcript tree (~10s) and sits on plan_admission's hot path — a subscription-lane call that assesses
shed/fallback calls it for the subagent_host lane. Before the cache, EVERY context-wrapped call paid ~10s and a
one-shot `spendguard delegate` was ~21s. The cache must (a) compute once per TTL, (b) let force / max_age_s=0
recompute, (c) stamp every return with freshness so a TTL-served value is never a silent skip, (d) serve a fresh
PROCESS from the disk cache, (e) be bypassed under SPENDGUARD_TEST_ISOLATED so it cannot bleed across runs, and
(f) write the memo monotonically.

Offline + deterministic: _compute_overage_status is replaced (at module scope) with a stub that stamps each
computation with a UNIQUE marker, so "was it recomputed?" is read from the marker (same marker across calls = the
scan was skipped) WITHOUT a mutable call-counter. The cache path is a temp file. No transcripts, no DB, no LLM.
"""
import os
import sys
import tempfile
import uuid

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-overage-cache-")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_TEST_ISOLATED", None)              # the cache is bypassed under isolation; exercise it ON first
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import overage  # noqa: E402

_CACHE_FILE = os.path.join(tempfile.mkdtemp(prefix="sg-overage-cachefile-"), "overage_status_cache.json")
# Stub the ~10s probe with a unique-marked status (module scope — not a per-call counter, so nothing is mutated).
overage._compute_overage_status = lambda: {"on_overage_now": False, "meaning": "x", "observed_overage_windows": 0,
                                           "real_overage_by_month_usd": {}, "mk": uuid.uuid4().hex}
overage._status_cache_path = lambda: _CACHE_FILE

fails = []


def ck(label, condition, detail=""):
    print(f"  [{'OK' if condition else 'FAIL'}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        fails.append(label)


def _clear_memo():
    overage._STATUS_MEMO.update(t=0.0, val=None)


def _rm_cache():
    try:
        os.remove(_CACHE_FILE)
    except OSError:
        pass


# ── (a) explicit refresh computes; ordinary reads ride the in-process memo ──
_clear_memo()
_rm_cache()
r1 = overage.refresh_overage_status_cache()
r2 = overage.current_overage_status()
ck("2nd call did NOT recompute (same marker = in-process memo hit)", r1.get("mk") == r2.get("mk"), (r1, r2))
ck("1st return is marked not-cached", r1.get("cached") is False, r1)
ck("2nd return is marked cached", r2.get("cached") is True, r2)
ck("every return carries freshness provenance (as_of + age_s)",
   isinstance(r1.get("as_of"), float) and isinstance(r2.get("age_s"), float), (r1, r2))

# ── (b) force=True recomputes (new marker); (c) max_age_s=0 recomputes ──
r3 = overage.current_overage_status(force=True)
ck("force=True recomputes (new marker)", r3.get("mk") != r1.get("mk"), (r1, r3))
r4 = overage.current_overage_status(max_age_s=0)
ck("max_age_s=0 recomputes (new marker)", r4.get("mk") != r3.get("mk"), (r3, r4))

# ── (d) a FRESH process (memo empty) is served from the DISK cache written by (c), no recompute ──
_clear_memo()
rd = overage.current_overage_status()
ck("fresh process served from disk cache (marker matches last compute)", rd.get("mk") == r4.get("mk"), (r4, rd))
ck("disk-served return is marked cached", rd.get("cached") is True, rd)

# ── (e) isolation prevents disk bleed but never permits an implicit slow refresh ──
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
_clear_memo()
_rm_cache()
ri1 = overage.current_overage_status()
ri2 = overage.current_overage_status()
ck("under TEST_ISOLATED missing cache is explicit unknown", ri1.get("stale") is True and ri1.get("on_overage_now") is None,
   (ri1, ri2))
ck("under TEST_ISOLATED ordinary calls never run the slow probe", "mk" not in ri1 and "mk" not in ri2, (ri1, ri2))
ck("under TEST_ISOLATED no disk cache file is written", not os.path.exists(_CACHE_FILE), _CACHE_FILE)
os.environ.pop("SPENDGUARD_TEST_ISOLATED", None)

# ── (f) monotonic memo: an OLDER computed_at must not clobber a NEWER cached value ──
_clear_memo()
new_status = {"on_overage_now": True, "tag": "new"}
overage._memo_put(1000.0, new_status)
overage._memo_put(500.0, {"on_overage_now": False, "tag": "old"})     # older → must be ignored
ck("monotonic memo: older computed_at did not overwrite newer",
   overage._STATUS_MEMO["val"] is new_status and overage._STATUS_MEMO["t"] == 1000.0, dict(overage._STATUS_MEMO))

print(f"\n{'[FAIL]' if fails else 'OK'} test_overage_status_cached: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
