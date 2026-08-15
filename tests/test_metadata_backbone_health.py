"""The model-metadata backbone auditor catches the two SILENT failures that degrade output_cap — an empty/stale
LiteLLM limits cache, and a measured cap that has drifted BELOW its published ceiling.

Both are real. Measured 2026-08-14: the synced LiteLLM cache held ZERO models, so pricing.max_output_tokens()
returned None for every model and output_cap's "clamp to the published max" silently became a no-op — and
NOTHING flagged it, because a None limit reads as "no opinion", not "the table is empty". Separately, the kimi
class of bug is a stale measured cap sitting under what the model now finishes in. This auditor must fail on
BOTH, while correctly NOT flagging a brand-new model that only has a measured cap (no published max yet).

The auditor is mechanical (freshness dates + integer compares on a fixed shape), so this test drives it with
controlled inputs offline — it verifies the DETECTION logic, independent of whatever the real cache holds.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-metadata-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import metadata_audit   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


# A healthy cache = fresh + broad. Publisheds and measured caps are injected to exercise each branch.
_HEALTHY_CACHE = {f"m{i}": {"max_output_tokens": 8000} for i in range(metadata_audit.MIN_EXPECTED_MODELS + 100)}


def _run(cache_models, age_days, published, measured_caps):
    metadata_audit.pricing.CONTEXT_LIMITS = cache_models
    metadata_audit.sync.cache_age_days = lambda: age_days
    metadata_audit.pricing.max_output_tokens = lambda m: published.get(m)
    metadata_audit.vendor_call.caps = lambda: measured_caps
    return metadata_audit.backbone_health()


# ── 1. EMPTY cache → NOT ok (the exact silent failure that disabled the published-max clamp) ─────────────────
r = _run({}, None, {}, {})
ck("an EMPTY limits cache is reported NOT ok (would silently disable the published-max clamp)",
   r["ok"] is False and r["cache"]["present"] is False and r["cache"]["reasons"], str(r["cache"]))

# ── 2. measured cap BELOW published → drift, NOT ok (the kimi shape) ──────────────────────────────────────────
r = _run(_HEALTHY_CACHE, 0.5, {"kimi-x": 128000}, {"moonshot/kimi-x": {"max_output_tokens": 26128, "method": "probe"}})
ck("a measured cap BELOW its published max is flagged as drift (stale probe starves the model)",
   r["ok"] is False and len(r["drift"]) == 1 and r["drift"][0]["measured"] == 26128
   and r["drift"][0]["published"] == 128000, str(r["drift"]))

# ── 3. measured-only (no published max yet — too new) → NOT drift, cache healthy → ok ─────────────────────────
r = _run(_HEALTHY_CACHE, 0.5, {}, {"moonshot/kimi-k3": {"max_output_tokens": 26128, "method": "probe"}})
ck("a measured-ONLY cap (no published max — a too-new model) is NOT drift; the 32K floor protects it",
   r["ok"] is True and not r["drift"] and len(r["unknown"]) == 1, f"ok={r['ok']} drift={r['drift']} unknown={r['unknown']}")

# ── 4. measured >= published, fresh broad cache → clean ok ────────────────────────────────────────────────────
r = _run(_HEALTHY_CACHE, 1.0, {"gpt-x": 32000}, {"openai/gpt-x": {"max_output_tokens": 40000, "method": "probe"}})
ck("a measured cap AT/ABOVE published, with a fresh broad cache, is clean (ok, no drift)",
   r["ok"] is True and not r["drift"], f"ok={r['ok']} drift={r['drift']}")

# ── 5. STALE cache (older than the alarm threshold) → NOT ok even if non-empty ────────────────────────────────
r = _run(_HEALTHY_CACHE, metadata_audit.MODEL_METADATA_STALE_DAYS + 5, {}, {})
ck("a STALE cache (older than the alarm threshold) is reported NOT ok",
   r["ok"] is False and r["cache"]["stale"] is True, str(r["cache"]))

print(f"\n{'[FAIL]' if fails else 'OK'} test_metadata_backbone_health: {fails} failure(s)")
sys.exit(1 if fails else 0)
