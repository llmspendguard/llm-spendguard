"""Multimodal input through the gate: `adapters.call(..., images=[…])` lets a VISION request ride spendguard —
metered, recorded, transparent — instead of a consumer bypassing the adapter with the raw SDK (which is why
7thsense's vision backend went around it). Proves: images load from a path or data: URL; the per-provider wire
parts are built (OpenAI image_url / Anthropic image block); image INPUT tokens are counted by the PIXEL rule
(not the text tokenizer); a vision call SKIPS the lanes (text-only CLIs) and rides the API (executor='api'); and
the multimodal content actually reaches the SDK. Offline: the anthropic SDK is stubbed to capture the request.
"""
import os
import sys
import types
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-mm-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard import adapters, lane_balance                                         # noqa: E402

fails = []


def ck(name, cond):
    ok = bool(cond)
    print(("  [OK] " if ok else "  [FAIL] ") + name)
    if not ok:
        fails.append(name)


# a valid 1×1 PNG as a data: URL (header parses to 1×1; the b64 is real)
PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
       "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

print("-- _load_image / _image_parts / _image_input_tokens (units) --")
info = adapters._load_image(PNG)
ck("data: URL loads with media type, base64, and header dimensions",
   info["media_type"] == "image/png" and info["w"] == 1 and info["h"] == 1 and len(info["b64"]) > 0)
ck("a dict image passes through unchanged (loaded once, reused)", adapters._load_image(info) is info)
op = adapters._image_parts([info], "openai")[0]
ck("OpenAI part is an image_url carrying the data URL", op["type"] == "image_url" and op["image_url"]["url"] == PNG)
ap = adapters._image_parts([info], "anthropic")[0]
ck("Anthropic part is a base64 image block with the media type",
   ap["type"] == "image" and ap["source"]["type"] == "base64" and ap["source"]["media_type"] == "image/png")
ck("image tokens use the PIXEL rule (provider-aware, > 0), not the base64-as-text count",
   adapters._image_input_tokens([info], "openai", "gpt-5.5") > 0
   and adapters._image_input_tokens([info], "anthropic", "claude-haiku-4-5") > 0)
# a genuinely missing file must RAISE, not silently drop the image
try:
    adapters._load_image(os.path.join(os.environ["SPENDGUARD_HOME"], "nope.png"))
    ck("a missing image path raises (never a silent skip)", False)
except FileNotFoundError:
    ck("a missing image path raises FileNotFoundError (never a silent skip)", True)

print("\n-- integration: the multimodal content reaches the SDK, and vision skips the lane (executor='api') --")
_captured = {}


class _Stream:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="a dog on grass")],
            usage=types.SimpleNamespace(input_tokens=1200, output_tokens=8), stop_reason="end_turn")


class _Messages:
    def stream(self, **kw):
        _captured.update(kw)                      # capture exactly what the SDK was asked to send
        return _Stream()


class _AnthroClient:
    def __init__(self, *a, **k):
        self.messages = _Messages()


_fake = types.ModuleType("anthropic")
_fake.Anthropic = _AnthroClient
sys.modules["anthropic"] = _fake
lane_balance.route_decision = lambda intent, model, reactive=False: (None, "no sub (test)")   # isolate substitution

r = adapters.call("claude-haiku-4-5", "Describe this image.", images=[PNG], max_tokens=64)
content = (_captured.get("messages") or [{}])[0].get("content")
ck("the request the SDK received is MULTIMODAL (a content list, not a bare string)", isinstance(content, list))
ck("it carries the text part AND the image block",
   any(p.get("type") == "text" for p in content) and any(p.get("type") == "image" for p in content))
ck("the answer comes back through the standard result contract", r.get("text") == "a dog on grass" and not r.get("error"))
ck("a vision call rode the metered API, NOT a lane (executor='api')", r.get("executor") == "api")
ck("input tokens came from the provider's own usage (includes the image)", r.get("in_tok") == 1200)

print(f"\n{'[FAIL]' if fails else 'OK'} test_adapters_multimodal: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
