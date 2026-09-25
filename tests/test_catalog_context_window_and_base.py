"""The catalog is the SSOT for two more per-model facts, WITH provenance (Ash 2026-09-25):
  • context_window as {value, source, verified} — the METERED API's input window, so a caller (7thsense's comprehend
    segmenter) can size to the REAL limit instead of a hardcoded guess, and every value RECORDS where it came from.
  • provider_base — exactly one reliable, priced BASE model per cloud provider (the tier-3 last-resort fallback
    target); kimi is Moonshot-only (no self-hosted kimi), so kimi-k2.6's provider must be moonshot.
Guards the accessors, the provenance, the one-base-per-provider invariant, and the kimi provider fix."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-ctxbase-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import model_catalog as mc  # noqa: E402

_fails = []
def ck(label, cond):
    if not cond:
        _fails.append(label)
    print(f"  [{'OK' if cond else 'FAIL'}] {label}")


print("-- catalog validates clean (context_window shape + one-base-per-provider) --")
probs = mc.validate_catalog()
ck(f"validate_catalog is clean ({probs})", probs == [])

print("-- context_window accessor + provenance --")
ck("glm-5.3 context_window = 1,000,000 (from Z.ai docs)", mc.context_window("glm-5.3") == 1000000)
ck("gemini-3.8-flash context_window = 1,048,576 (from Google docs)", mc.context_window("gemini-3.8-flash") == 1048576)
ck("a provider-prefixed id resolves too", mc.context_window("zai:glm-5.3") == 1000000)
ck("an unknown context window is None (not a fake value)", mc.context_window("kimi-k3") is None)
# provenance: any record with a context_window VALUE must record its source (never a bare number)
missing_prov = []
for mid in mc.ids():
    cw = (mc.model_record(mid) or {}).get("context_window")
    if isinstance(cw, dict) and cw.get("value") is not None and not cw.get("source"):
        missing_prov.append(mid)
ck(f"every known context_window records a source ({missing_prov})", missing_prov == [])
gcw = (mc.model_record("glm-5.3") or {}).get("context_window") or {}
ck("glm-5.3 context_window source is the Z.ai provider doc", "z.ai" in (gcw.get("source") or ""))

print("-- provider_base: one reliable, priced, same-provider base per cloud provider --")
for prov in ("openai", "anthropic", "gemini", "zai", "deepseek", "moonshot", "qwen"):
    base = mc.provider_base(prov)
    ck(f"{prov} has a base model ({base})", bool(base))
    if base:
        rec = mc.model_record(base) or {}
        ck(f"{prov} base {base} is same-provider + priced",
           (rec.get("provider") == prov) and ((rec.get("price") or {}).get("in_") is not None))
ck("self-hosted 'custom' has NO base (no same-provider cloud guarantee)", mc.provider_base("custom") is None)
ck("moonshot base is kimi-k3 (kimi is always k3+)", mc.provider_base("moonshot") == "kimi-k3")

print("-- kimi provider consistency: no self-hosted kimi --")
ck("kimi-k2.6 provider is moonshot (not custom)", (mc.model_record("kimi-k2.6") or {}).get("provider") == "moonshot")

print(f"\n{'[FAIL]' if _fails else 'OK'} test_catalog_context_window_and_base: {len(_fails)} failure(s)")
sys.exit(1 if _fails else 0)
