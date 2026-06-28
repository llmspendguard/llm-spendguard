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
gate._record_rt = (lambda model, kw, in_tok, out_tok, cached=0, latency=None, output=None, finish=None,
                   cost=None, provider=None: REC.append((model, in_tok, out_tok, provider)))

class _U:
    def __init__(self): self.prompt_tokens = 100; self.completion_tokens = 42
class _Resp:
    def __init__(self, model, usage=None): self.model = model; self.usage = usage if usage is not None else _U()

litellm_adapter.record_litellm({"model": "bedrock/claude-3", "custom_llm_provider": "bedrock", "response_cost": 0.01}, _Resp("bedrock/claude-3"))
ck("Bedrock (non-SDK) recorded w/ usage + provider label", REC and REC[-1] == ("bedrock/claude-3", 100, 42, "bedrock"))

REC.clear()
litellm_adapter.record_litellm({"model": "gpt-5.5", "custom_llm_provider": "openai"}, _Resp("gpt-5.5"))
ck("openai-via-LiteLLM SKIPPED (SDK gate already counts it — no double count)", REC == [])
litellm_adapter.record_litellm({"model": "azure/gpt", "custom_llm_provider": "azure"}, _Resp("azure/gpt"))
ck("azure-via-LiteLLM SKIPPED", REC == [])

REC.clear()
litellm_adapter.record_litellm({"model": "vertex_ai/gemini", "custom_llm_provider": "vertex_ai"},
                               {"model": "vertex_ai/gemini", "usage": {"prompt_tokens": 5, "completion_tokens": 7}})
ck("Vertex dict-shaped usage recorded", REC and REC[-1] == ("vertex_ai/gemini", 5, 7, "vertex_ai"))

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

# ── 4. Bedrock adapter (mock botocore) — captures bedrock-runtime, passes everything else through, fail-open ──
fake_bc = types.ModuleType("botocore.client")
class BaseClient:
    def __init__(self, svc): self.meta = types.SimpleNamespace(service_model=types.SimpleNamespace(service_name=svc))
    def _make_api_call(self, op, params): return self._resp     # stand-in for the real AWS call
fake_bc.BaseClient = BaseClient
fake_botocore = types.ModuleType("botocore"); fake_botocore.client = fake_bc
sys.modules["botocore"] = fake_botocore; sys.modules["botocore.client"] = fake_bc
try:
    from spendguard import bedrock_adapter
    ck("install_bedrock wires (force)", bedrock_adapter.install(force=True) is True)
    ck("BaseClient._make_api_call patched", getattr(BaseClient._make_api_call, "_spend_gated", False) is True)
    br = BaseClient("bedrock-runtime")
    REC.clear()
    br._resp = {"usage": {"inputTokens": 200, "outputTokens": 55}}
    out = br._make_api_call("Converse", {"modelId": "anthropic.claude-3-5-sonnet"})
    ck("bedrock Converse recorded (200/55, provider=bedrock)", REC and REC[-1] == ("anthropic.claude-3-5-sonnet", 200, 55, "bedrock"))
    ck("bedrock passthrough returns the real response", out == {"usage": {"inputTokens": 200, "outputTokens": 55}})
    REC.clear()
    br._resp = {"ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": "30", "x-amzn-bedrock-output-token-count": "12"}}, "body": "…"}
    br._make_api_call("InvokeModel", {"modelId": "amazon.nova-pro-v1:0"})
    ck("bedrock InvokeModel recorded from headers (30/12)", REC and REC[-1] == ("amazon.nova-pro-v1:0", 30, 12, "bedrock"))
    REC.clear()
    s3 = BaseClient("s3"); s3._resp = {"x": 1}
    r = s3._make_api_call("GetObject", {"Bucket": "b"})
    ck("non-bedrock boto3 call passes through, NOT recorded", REC == [] and r == {"x": 1})
finally:
    for k in ("botocore", "botocore.client"):
        sys.modules.pop(k, None)

# ── 5. Vertex adapter (mock google.genai) — captures generate_content usage, labelled provider=google ──
fake_models = types.ModuleType("google.genai.models")
class Models:
    def generate_content(self, **kw): return self._resp
class AsyncModels:
    async def generate_content(self, **kw): return self._resp
fake_models.Models = Models; fake_models.AsyncModels = AsyncModels
fake_genai = types.ModuleType("google.genai"); fake_genai.models = fake_models
fake_google = types.ModuleType("google"); fake_google.genai = fake_genai
sys.modules["google"] = fake_google; sys.modules["google.genai"] = fake_genai; sys.modules["google.genai.models"] = fake_models
try:
    from spendguard import vertex_adapter
    ck("install_vertex wires (force)", vertex_adapter.install(force=True) is True)
    ck("genai generate_content patched", getattr(Models.generate_content, "_spend_gated", False) is True)
    REC.clear()
    m = Models()
    m._resp = types.SimpleNamespace(usage_metadata=types.SimpleNamespace(prompt_token_count=300, candidates_token_count=80), model_version="gemini-2.5-pro")
    out = m.generate_content(model="gemini-2.5-pro", contents="hi")
    ck("vertex recorded (300/80, provider=google)", REC and REC[-1] == ("gemini-2.5-pro", 300, 80, "google"))
    ck("vertex passthrough returns the real response", out is m._resp)
finally:
    for k in ("google", "google.genai", "google.genai.models"):
        sys.modules.pop(k, None)

print(("[OK]" if not fails else "[FAIL]") + " provider-coverage: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
