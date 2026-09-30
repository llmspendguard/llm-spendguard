"""Embedding models are a CATALOG (the SSOT), never hardcoded ids (Ash: "just like the lmm catalog we have to have one
for embed models ... no hard coding ever").

model_catalog.embedding_models() is the ONE place that answers "which models embed" — catalog records marked
capabilities.mode == 'embedding' (openai text-embedding-*, gemini gemini-embedding-*, voyage voyage-*). This asserts:
the resolver returns the curated embedders (cheapest-input first), per-provider filtering works, the default embed model
is catalog-derived (not a bare literal), a CHAT reachability probe never picks an embedding model (they price out=0.0, so
"cheapest output" would otherwise always select one and 404 on /chat/completions — the voyage-as-chat bug adding voyage
surfaced), and — behaviourally — the smoke harness DERIVES its embed targets from the resolver (swap the resolver, the
cells follow; a hardcoded id would not move).
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-embedcat-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import model_catalog, reliability, adapters   # noqa: E402

_fails = []
def ck(label, cond, extra=""):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mode(model_id):
    return (model_catalog.model_record(model_id) or {}).get("capabilities", {}).get("mode")


def main():
    # ── the resolver returns the curated embedders, every one truly marked mode=='embedding' ──
    allm = model_catalog.embedding_models()
    provs = {p for p, _m in allm}
    ck("embedding_models() is non-empty", bool(allm), str(allm))
    ck("every returned model is catalog mode=='embedding'", all(_mode(m) == "embedding" for _p, m in allm), str(allm))
    ck("covers openai + gemini + voyage (the wired providers)", {"openai", "gemini", "voyage"} <= provs, str(provs))

    # ── sorted cheapest-input first (so a caller taking [0] gets the cheapest) ──
    ins = [(model_catalog.model_record(m) or {}).get("price", {}).get("in_") for _p, m in allm]
    ck("sorted by input price ascending", ins == sorted(ins), str(ins))

    # ── per-provider filtering ──
    ck("embedding_models('voyage') → voyage only",
       [p for p, _m in model_catalog.embedding_models("voyage")] == ["voyage"],
       str(model_catalog.embedding_models("voyage")))
    ck("embedding_models('nope') → [] (honest absence, no fallback)", model_catalog.embedding_models("nope") == [])

    # ── the default embed model is CATALOG-DERIVED (a real embedding model), not a bare literal ──
    ck("_embed_default_model() is a catalog embedding model", _mode(adapters._embed_default_model()) == "embedding",
       str(adapters._embed_default_model()))

    # ── a CHAT reachability probe NEVER selects an embedding model (the voyage-as-chat / out=0.0 bug) ──
    vp = reliability._probe_default("voyage")
    ck("_probe_default('voyage') is None (voyage is embed-ONLY → not a chat target)", vp is None, str(vp))
    op = reliability._probe_default("openai")
    ck("_probe_default('openai') is a CHAT model, not an embedding one", op is not None and _mode(op) != "embedding", str(op))
    ck("voyage is NOT in the metered CHAT slate (plan)", not any(p == "voyage" for p, _m in reliability.plan()["metered"]))

    # ── BEHAVIOURAL no-hardcode: the harness derives embed targets from the resolver — swap it, the cells follow ──
    sys.path.insert(0, os.path.join(REPO, "scripts", "reliability"))
    import consumer_provider_smoke as harness
    _orig_em, _orig_key = model_catalog.embedding_models, adapters.config.api_key
    try:
        model_catalog.embedding_models = lambda provider=None: [("gemini", "sentinel-embed-x")]
        adapters.config.api_key = lambda env: "k"          # pretend every provider is keyed, so the filter admits it
        cells = harness._embed_cells()
        ck("harness embed cells FOLLOW the swapped resolver (derived, not a hardcoded literal)",
           cells == [("gemini", "sentinel-embed-x")], str(cells))     # bare model id, uniform with chat cells
    finally:
        model_catalog.embedding_models, adapters.config.api_key = _orig_em, _orig_key

    print(f"\n{'[FAIL]' if _fails else 'OK'} test_embedding_model_catalog: {len(_fails)} failure(s)")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
