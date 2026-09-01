"""A subscription LANE must never STRAND a call when its plan is down/exhausted — it falls back to the provider's
METERED API. That only works if the lane's model id maps to an id the metered API actually serves + prices; the lane
NAMING (agy's reasoning-suffix ids) must not break that. This pins the equivalence so it can't silently rot:

  1. adapters.metered_fallback_id is the ONE mapping. For gemini/agy it splits the reasoning SUFFIX off
     (gemini-3.7-flash-medium → gemini-3.7-flash + 'medium'), because the metered API wants the BARE id + a reasoning
     PARAMETER (the suffixed id 404s). Every other provider's lane id already IS its metered id (passthrough).
  2. lane_catalog.audit_lane_fallback checks EVERY configured lane use-name's fallback id is PRICED (and, when the
     catalog is synced, SERVED). A lane whose id is unpriced/unmapped is flagged ok=False — the exact 'a down lane
     strands the call' defect, caught here instead of in production.

Hermetic: config + served_check are monkeypatched (no isolated home, no network, no re-exec), zero spend. If a
future lane model becomes unpriced or a suffix stops being split, a check here goes red."""
import sys

from spendguard import adapters, lane_catalog, config, vendor_call

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── 1. metered_fallback_id: the single equivalence ──────────────────────────────────────────────────────────────
for t in adapters._GEMINI_REASONING_TIERS:
    mid, tier = adapters.metered_fallback_id("gemini", f"gemini-3.7-flash-{t}")
    ck(f"gemini agy suffix -{t} → bare id + reasoning (metered API wants bare id)",
       mid == "gemini-3.7-flash" and tier == t)
mid, tier = adapters.metered_fallback_id("gemini", "gemini-3.7-flash")
ck("gemini bare id passes through, no reasoning", mid == "gemini-3.7-flash" and tier is None)
for prov, m in [("openai", "gpt-5.6-sol"), ("anthropic", "claude-opus-4-8"), ("zai", "glm-5.3")]:
    mid, tier = adapters.metered_fallback_id(prov, m)
    ck(f"{prov} lane id IS its metered id (passthrough, no suffix game)", mid == m and tier is None)

# ── 2. audit_lane_fallback catches BOTH a good mapping AND a broken (unpriced) one ───────────────────────────────
_orig_cfg = config._cfg_get
_orig_served = vendor_call.served_check
def _fake_cfg(*a, **k):
    if a[:2] == ("advisor", "lane_models"):
        # three real, priced lane models + one deliberately BOGUS unpriced one (the break the audit must catch)
        return {"claude-code": "claude-opus-4-8", "codex": "gpt-5.6-sol",
                "gemini": "gemini-3.7-flash-medium", "zai-coding": "totally-not-a-real-model-xyz-000"}
    return _orig_cfg(*a, **k)
config._cfg_get = _fake_cfg
vendor_call.served_check = lambda prov, mid: "unchecked"   # hermetic: no catalog cache read, no network
try:
    rows = lane_catalog.audit_lane_fallback()
    by = {}
    for r in rows:
        by.setdefault(r["lane"], []).append(r)

    gem = by.get("gemini", [])
    ck("gemini suffix use-names ALL map to the bare metered id", bool(gem) and all(r["metered_id"] == "gemini-3.7-flash" for r in gem))
    ck("gemini fallback id is priced + ok", bool(gem) and all(r["priced"] and r["ok"] for r in gem))
    ck("gemini metered id genuinely DIFFERS from the suffix use-name (the split did work)",
       any(r["metered_id"] != r["use_name"] for r in gem))

    real = by.get("codex", []) + by.get("claude-code", [])
    ck("codex + claude-code fallback ids priced + ok", bool(real) and all(r["priced"] and r["ok"] for r in real))

    zai = by.get("zai-coding", [])
    ck("a BOGUS/unpriced lane model → priced=False (the break is detected)", bool(zai) and all(not r["priced"] for r in zai))
    ck("a BOGUS/unpriced lane model → ok=False (would STRAND a down-lane call — flagged, not shipped)",
       bool(zai) and all(not r["ok"] for r in zai))
finally:
    config._cfg_get = _orig_cfg
    vendor_call.served_check = _orig_served

print(("\n[OK] " if not fails else "\n[FAIL] ") + "lane_metered_fallback_equivalence: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
