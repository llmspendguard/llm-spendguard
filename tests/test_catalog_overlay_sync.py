"""Client data-plane T2 sync: saas.sync_catalog_overlay() pulls the server's spendguard-CURATED catalog
(GET /v1/models?curated=1) into catalog_synced.json — the overlay model_catalog layers above the shipped floor. So a
curated fact the server carries (a newly-measured embed ceiling, a verified price) reaches THIS install with no
package release (docs/DATA_PLANE.md §9.2, the client half of item 2).

Pins: a successful pull writes the overlay + model_catalog reflects it; an unreachable server FAILS OPEN (returns 0,
leaves any existing overlay untouched — never wipes a good overlay with nothing); an empty curated response does NOT
overwrite a good overlay; a deliberate spend/deadline stop PROPAGATES (never a silent skip). Offline: saas._request is
mocked (no network, no key, no spend); isolated SPENDGUARD_HOME.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-catalog-sync-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import saas, model_catalog, gate   # noqa: E402


def main():
    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    _real = saas._request
    curated = {"gemini-embedding-001": {"id": "gemini-embedding-001", "provider": "gemini",
               "capabilities": {"mode": "embedding", "embed_max_batch": {"value": 222}}}}
    try:
        # 1) a successful pull writes catalog_synced.json AND model_catalog reflects it
        saas._request = lambda method, path, **kw: {"models": curated, "count": 1, "source": "spendguard-curated"}
        n = saas.sync_catalog_overlay()
        ck("a successful pull returns the curated model count", n == 1, n)
        ck("catalog_synced.json was written under HOME", os.path.exists(model_catalog._overlay_path() or ""))
        ck("model_catalog reflects the synced curated fact (gemini embed ceiling 222, not the shipped 100)",
           model_catalog.embed_batch_ceiling("gemini-embedding-001") == 222,
           model_catalog.embed_batch_ceiling("gemini-embedding-001"))

        # 2) unreachable server → FAIL OPEN (returns 0), the existing overlay is NOT wiped
        saas._request = lambda method, path, **kw: (_ for _ in ()).throw(RuntimeError("connection refused"))
        n2 = saas.sync_catalog_overlay()
        ck("unreachable server → returns 0 (fail-open)", n2 == 0, n2)
        ck("the existing overlay is NOT wiped by an unreachable sync (gemini still 222)",
           model_catalog.embed_batch_ceiling("gemini-embedding-001") == 222)

        # 3) an empty curated response does NOT overwrite a good overlay
        saas._request = lambda method, path, **kw: {"models": {}, "count": 0}
        n3 = saas.sync_catalog_overlay()
        ck("empty curated response → returns 0, overlay untouched (gemini still 222)",
           n3 == 0 and model_catalog.embed_batch_ceiling("gemini-embedding-001") == 222, n3)

        # 4) a deliberate spend/deadline stop PROPAGATES (never downgraded to a silent skip)
        def _stop(*a, **k):
            raise gate.SpendGateRefused("cap exceeded")
        saas._request = _stop
        propagated = False
        try:
            saas.sync_catalog_overlay()
        except gate.SpendGateRefused:
            propagated = True
        ck("a deliberate spend stop PROPAGATES (not swallowed by fail-open)", propagated)
    finally:
        saas._request = _real

    print(f"\n{'[FAIL]' if fails else 'OK'} test_catalog_overlay_sync: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
