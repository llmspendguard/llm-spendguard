"""The MCP spendguard_vision tool: a governed, estimate-first, idempotent metered vision call for MCP consumers.

Vision must ride the metered API (lanes are text-only). This tool exposes that to non-Python consumers with the
same rails the labeler needed: estimate-first (no budget → 0 spend), a dynamic-key MAP schema refused BEFORE any
spend (strict mode empties it to {}), and a durable content-keyed cache so an identical re-request is $0. Offline:
spendguard.require / adapters.vision / pricing stubbed — no gate, no network, no LLM. Assertions are STRUCTURAL
(did it spend? via a captured-call list and a control), never substring-matching on error text.
"""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-mcpvis-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard
from spendguard import mcp_server, adapters, pricing

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


spendguard.require = lambda: None                         # offline: do not require a live gate
adapters._load_image = lambda i: {"w": 1, "h": 1, "media_type": "image/png", "b64": "x", "data_uri": str(i)}
adapters._image_input_tokens = lambda imgs, prov, raw: 10
pricing.cost_or_unpriced = lambda *a, **k: 0.005          # fixed estimate so budget comparisons are deterministic

_spent = []


def _fake_vision(model, prompt, images, **kw):
    _spent.append({"model": model, "images": images, **kw})
    return {"text": '[{"id":"a","label":"x"}]', "cost": 0.004, "executor": "api", "in_tok": 10, "out_tok": 5}


adapters.vision = _fake_vision
V = mcp_server._TOOLS.get("spendguard_vision", (None, None, None))[2]
IMG = ["data:image/png;base64,AAAA"]
M = "openai:gpt-5-nano"

ck("spendguard_vision is registered in the MCP tool set", "spendguard_vision" in mcp_server._TOOLS and V is not None)

# ── estimate-first: NO budget → returns the estimate and does NOT spend ──
_spent.clear()
r = V({"model": M, "prompt": "label", "images": IMG})
ck("no budget → estimate only, 0 spend (adapters.vision not called)", "estimate_usd" in r and r.get("note") and not _spent)

# ── budget >= estimate → runs, forwards images, returns text ──
_spent.clear()
r = V({"model": M, "prompt": "run me", "images": IMG, "budget_usd": 1.0})
ck("budget >= est → runs once and forwards images=", r.get("text") and len(_spent) == 1 and _spent[0]["images"] == IMG)

# ── estimate > budget → refused, no spend (structural: error present AND nothing spent) ──
_spent.clear()
r = V({"model": M, "prompt": "too pricey", "images": IMG, "budget_usd": 0.0001})
ck("est > budget → refused (error present, 0 spend)", r.get("error") is not None and not _spent)

# ── empty images → error, no spend ──
_spent.clear()
r = V({"model": M, "prompt": "p", "images": []})
ck("empty images → error, 0 spend", r.get("error") is not None and not _spent)

# ── dynamic-key MAP schema on OpenAI → refused BEFORE spend. Control: same request w/ a LIST schema DOES spend, so
#    the refusal is caused by the map, not by budget or anything else ──
MAP = {"type": "object", "properties": {"labels": {"type": "object", "additionalProperties": {"type": "string"}}}}
LIST = {"type": "object", "required": ["results"], "properties": {"results": {"type": "array", "items": {
    "type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}}}}
_spent.clear()
r_map = V({"model": M, "prompt": "schema", "images": IMG, "schema": MAP, "budget_usd": 1.0})
n_after_map = len(_spent)
r_list = V({"model": M, "prompt": "schema", "images": IMG, "schema": LIST, "budget_usd": 1.0, "no_cache": True})
ck("map schema → refused with 0 spend; the SAME request with a list schema DOES spend (map is the cause)",
   r_map.get("error") is not None and n_after_map == 0 and r_list.get("text") and len(_spent) == 1)

# ── DURABLE cache: an identical re-request is served for $0 (no second spend); no_cache forces a fresh call ──
_spent.clear()
a1 = V({"model": M, "prompt": "CACHE ME", "images": IMG, "budget_usd": 1.0})
n1 = len(_spent)
a2 = V({"model": M, "prompt": "CACHE ME", "images": IMG, "budget_usd": 1.0})
ck("identical re-request → served from cache (cached=True, no second spend)", a2.get("cached") is True and len(_spent) == n1)
a3 = V({"model": M, "prompt": "CACHE ME", "images": IMG, "budget_usd": 1.0, "no_cache": True})
ck("no_cache=true → forces a fresh call (spends again)", not a3.get("cached") and len(_spent) == n1 + 1)

print(("[OK]" if not fails else "[FAIL]") + " mcp vision tool: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
