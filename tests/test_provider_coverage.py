"""Provider breadth — guards what the gate covers beyond the direct OpenAI/Anthropic SDKs:
  • Azure OpenAI is gated FOR FREE — `AzureOpenAI` reuses the same `openai.resources` classes the gate patches, so
    its `.create` IS the gated method. Locked here so it can't silently regress.
  • LiteLLM coverage — `record_litellm` (the success-callback) records ANY provider LiteLLM normalizes into the same
    ledger, but SKIPS openai/azure (already captured by the SDK gate) so nothing is double-counted; fail-open;
    `install()` wires it idempotently. Offline, mocked, zero spend."""
import os, sys, tempfile, types

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-prov-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

import spendguard
from spendguard import gate, litellm_adapter

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

# ── 1. Azure OpenAI is gated for free (same resource classes) ──
gate.install()
import openai
import openai.resources.chat.completions as cc
ck("OpenAI Completions.create patched", getattr(cc.Completions.create, "_spend_gated", False) is True)
az = openai.AzureOpenAI(api_key="x", api_version="2024-02-01", azure_endpoint="https://x.openai.azure.com")
ck("AzureOpenAI .create IS the gated method (free coverage)", getattr(type(az.chat.completions).create, "_spend_gated", False) is True)
azc = openai.AsyncAzureOpenAI(api_key="x", api_version="2024-02-01", azure_endpoint="https://x.openai.azure.com")
ck("AsyncAzureOpenAI .create gated", getattr(type(azc.chat.completions).create, "_spend_gated", False) is True)

# ── 2. LiteLLM record_litellm: capture non-SDK providers, skip SDK-gated, fail-open ──
REC = []
gate._record_rt = lambda model, kw, in_tok, out_tok, *a, **k: REC.append((model, in_tok, out_tok))

class _U:
    def __init__(self): self.prompt_tokens = 100; self.completion_tokens = 42
class _Resp:
    def __init__(self, model, usage=None): self.model = model; self.usage = usage if usage is not None else _U()

litellm_adapter.record_litellm({"model": "bedrock/claude-3", "custom_llm_provider": "bedrock"}, _Resp("bedrock/claude-3"))
ck("Bedrock (non-SDK) recorded with usage", REC and REC[-1] == ("bedrock/claude-3", 100, 42))

REC.clear()
litellm_adapter.record_litellm({"model": "gpt-5.5", "custom_llm_provider": "openai"}, _Resp("gpt-5.5"))
ck("openai-via-LiteLLM SKIPPED (SDK gate already counts it — no double count)", REC == [])
litellm_adapter.record_litellm({"model": "azure/gpt", "custom_llm_provider": "azure"}, _Resp("azure/gpt"))
ck("azure-via-LiteLLM SKIPPED", REC == [])

REC.clear()
litellm_adapter.record_litellm({"model": "vertex_ai/gemini", "custom_llm_provider": "vertex_ai"},
                               {"model": "vertex_ai/gemini", "usage": {"prompt_tokens": 5, "completion_tokens": 7}})
ck("Vertex dict-shaped usage recorded", REC and REC[-1] == ("vertex_ai/gemini", 5, 7))

REC.clear()
litellm_adapter.record_litellm({"model": "cohere/x", "custom_llm_provider": "cohere"},
                               types.SimpleNamespace(model="cohere/x", usage=None))
ck("no-usage event not recorded", REC == [])

raised = False
try:
    litellm_adapter.record_litellm({"model": "x", "custom_llm_provider": "cohere"}, object())  # odd response
    litellm_adapter.record_litellm(None, None)                                                 # garbage in
except Exception:
    raised = True
ck("fail-open: a bad response never raises into LiteLLM", not raised)

# ── 3. install() wires the callback idempotently (fake litellm module) ──
fake = types.ModuleType("litellm"); fake.success_callback = []
sys.modules["litellm"] = fake
try:
    ck("install(force=False) wires when litellm is present", litellm_adapter.install() is True and litellm_adapter.record_litellm in fake.success_callback)
    litellm_adapter.install(); litellm_adapter.install()
    ck("install is idempotent (no duplicate callback)", fake.success_callback.count(litellm_adapter.record_litellm) == 1)
    ck("public spendguard.install_litellm() returns True", spendguard.install_litellm() is True)
finally:
    del sys.modules["litellm"]

# no litellm present → install(force=False) is a no-op (False), never raises
ck("install(force=False) → False when litellm absent", litellm_adapter.install(force=False) is False)

print(("[OK]" if not fails else "[FAIL]") + " provider-coverage: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
