"""Gemini embed_content capture (vertex_adapter) — the google.genai EMBEDDINGS surface joins the same
realtime ledger as generate_content (provider='google'), fail-open. The real SDK is heavy/optional, so a
STUB google.genai module stands in — but the response shapes mirror the documented SDK (per-embedding
statistics.token_count; usage_metadata fallback). Offline, zero spend."""
import os
import sys
import tempfile
import types

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-vembed-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# ── stub the google.genai module tree (models.Models / AsyncModels with both methods) ──
class Models:
    def generate_content(self, *a, **kw):
        return types.SimpleNamespace(usage_metadata=types.SimpleNamespace(
            prompt_token_count=100, candidates_token_count=40))

    def embed_content(self, *a, **kw):
        return types.SimpleNamespace(embeddings=[
            types.SimpleNamespace(statistics=types.SimpleNamespace(token_count=700)),
            types.SimpleNamespace(statistics=types.SimpleNamespace(token_count=300)),
        ])


class AsyncModels:
    async def embed_content(self, *a, **kw):
        return types.SimpleNamespace(usage_metadata=types.SimpleNamespace(prompt_token_count=55))
    async def generate_content(self, *a, **kw):
        return types.SimpleNamespace(usage_metadata=types.SimpleNamespace(
            prompt_token_count=1, candidates_token_count=1))


gm = types.ModuleType("google.genai.models")
gm.Models, gm.AsyncModels = Models, AsyncModels
genai = types.ModuleType("google.genai")
genai.models = gm
google = types.ModuleType("google")
google.genai = genai
sys.modules["google"] = google
sys.modules["google.genai"] = genai
sys.modules["google.genai.models"] = gm

from spendguard import vertex_adapter, gate  # noqa: E402

captured = []
gate._record_rt = lambda model, kw, i, o, **k: captured.append((model, i, o, k.get("provider")))

ck("install() wires the stubbed SDK", vertex_adapter.install(force=True) is True)
ck("generate_content patched", getattr(Models.generate_content, "_spend_gated", False))
ck("embed_content patched (sync + async)",
   getattr(Models.embed_content, "_spend_gated", False) and getattr(AsyncModels.embed_content, "_spend_gated", False))

m = Models()
m.embed_content(model="text-embedding-004", contents=["a", "b"])
ck("embed_content usage captured from per-embedding statistics (700+300, out=0)",
   captured and captured[-1] == ("text-embedding-004", 1000, 0, "google"))

import asyncio
asyncio.run(AsyncModels().embed_content(model="text-embedding-004", contents="x"))
ck("async embed_content captured via usage_metadata fallback (55, out=0)",
   captured[-1] == ("text-embedding-004", 55, 0, "google"))

m.generate_content(model="gemini-2.0-flash")
ck("generate_content capture unchanged (100 in / 40 out)",
   captured[-1] == ("gemini-2.0-flash", 100, 40, "google"))

ck("install() is idempotent", vertex_adapter.install(force=True) is True and
   getattr(Models.embed_content, "_spend_gated", False))

print(("[OK]" if not fails else "[FAIL]") + " vertex embed capture: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
