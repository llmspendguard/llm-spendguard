"""The agy SUBSCRIPTION lane's id namespace is part of the served-set — so a served-check recognises an agy id.

agy serves gemini under reasoning-tier-suffixed ids (gemini-3.1-pro-high) that the metered Gemini /models list
NEVER returns. A served-check that consulted only the metered catalog false-negatived a perfectly-served agy panel
model (honestreview dropped its 5th reviewer every run). This guards the fix:
  • antigravity_exec.model_ids parses `agy models` (two-column, banner skipped) — a PURE parse, no memo,
  • catalog records agy's gemini ids under a SEPARATE lane_models key at pull time (family-filtered),
  • catalog.served() unions metered ∪ lane (True/False/None), while catalog.live_model_ids() stays METERED-ONLY
    (reliability picks metered probe targets from it — a lane-only id would break that),
  • vendor_call.served_check() honours the lane namespace, so an agy id is 'served', never a false 'stale'.
Offline: agy + metered fetch are stubbed; no subprocess, no network.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-agy-served-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import antigravity_exec, catalog, vendor_call

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── _parse_agy_models: id = column 1 of each '<id>\t<display>' row; the 'Fetching…' banner (no TAB) is skipped ──
_SAMPLE = ("Fetching available models...\n"
           "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
           "gemini-3.7-flash-high\tGemini 3.7 Flash (High)\n"
           "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n")
ids = antigravity_exec._parse_agy_models(_SAMPLE)
ck("_parse_agy_models takes column 1 and skips the no-TAB banner",
   ids == ["gemini-3.1-pro-high", "gemini-3.7-flash-high", "claude-sonnet-4-6"])
ck("_parse_agy_models('') → [] (fail-soft, never crashes)", antigravity_exec._parse_agy_models("") == [])

# ── pull records ONLY the gemini family under lane_models (agy also serves claude-*/gpt-oss-*) ──
catalog.config.api_key = lambda env="": ""             # no metered key for ANY provider → the fetch loop skips all (no network)
antigravity_exec.model_ids = lambda timeout_s=30: ["gemini-3.1-pro-high", "gemini-3.7-flash-high",
                                                   "claude-sonnet-4-6", "gpt-oss-120b-medium"]
n_ok, n_ids, errs = catalog.pull_live_catalog()
_lane = catalog.lane_model_ids("gemini") or []
ck("pull records agy's gemini ids under lane_models", set(_lane) == {"gemini-3.1-pro-high", "gemini-3.7-flash-high"})
ck("...and does NOT fold agy's non-gemini families into the gemini lane set",
   "claude-sonnet-4-6" not in _lane and "gpt-oss-120b-medium" not in _lane)
ck("no metered key → the fetch loop is skipped cleanly, not an error", not errs)

# ── served() unions BOTH namespaces; live_model_ids() stays METERED-ONLY; None when nothing is known ──
catalog._load_catalog = lambda: {"models": {"gemini": ["gemini-3.1-pro-preview"]},
                                 "lane_models": {"gemini": ["gemini-3.1-pro-high"]}}
ck("served() True for a LANE id (agy) absent from the metered catalog", catalog.served("gemini", "gemini-3.1-pro-high") is True)
ck("served() True for a METERED id", catalog.served("gemini", "gemini-3.1-pro-preview") is True)
ck("served() False for an id in NEITHER namespace", catalog.served("gemini", "gemini-9-imaginary") is False)
ck("live_model_ids() stays METERED-only (no agy lane id leaks in — reliability probe targets safe)",
   "gemini-3.1-pro-high" not in (catalog.live_model_ids("gemini") or []))
catalog._load_catalog = lambda: {}                     # neither namespace known
ck("served() None when the catalog cannot be checked (never read as 'no')", catalog.served("gemini", "anything") is None)

# ── served_check() honours the lane namespace: an agy id is 'served' WITHOUT a live metered confirm (no false 'stale') ──
catalog._load_catalog = lambda: {"models": {"gemini": ["gemini-3.1-pro-preview"]},
                                 "lane_models": {"gemini": ["gemini-3.1-pro-high"]}}


def _serves_must_not_run(*a, **k):
    raise AssertionError("serves() was called for a lane id already in the cache — that is a needless live fetch")


vendor_call.serves = _serves_must_not_run
ck("served_check() → 'served' for an agy lane id, with NO live metered confirm",
   vendor_call.served_check("gemini", "gemini-3.1-pro-high") == "served")
ck("served_check() → 'served' for a metered id (fast path)",
   vendor_call.served_check("gemini", "gemini-3.1-pro-preview") == "served")

# a genuinely unknown id, metered cache present → CONFIRM LIVE (serves) before ever calling it stale
vendor_call.serves = lambda vendor, model: False       # live says the metered API does not serve it
ck("served_check() → 'stale' for an id in NEITHER cache once a live metered confirm denies it",
   vendor_call.served_check("gemini", "gemini-9-imaginary") == "stale")

# only a lane list exists (no metered cache) and the id is not in it → 'unchecked', never a metered-confirm 'stale'
catalog._load_catalog = lambda: {"lane_models": {"gemini": ["gemini-3.1-pro-high"]}}
vendor_call.serves = _serves_must_not_run
ck("served_check() → 'unchecked' when only a lane list exists and lacks the id (no bogus metered confirm)",
   vendor_call.served_check("gemini", "gemini-3.7-flash-high") == "unchecked")

print(("[OK]" if not fails else "[FAIL]") + " agy served set: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
