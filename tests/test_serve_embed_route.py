"""`spendguard serve` POST /embed — gated embeddings over HTTP, as honest + safe as /ask:

  * returns adapters.embed's contract on the wire (vectors ALIGNED to inputs, None where an item failed, failed[]),
    so a partial failure is usable and never a silently-short list;
  * a deliberate spend refusal (BudgetRefused / any SpendGateRefused cap breach) propagates to 402 — NEVER a false 200;
  * the host-local `checkpoint` path (a durability detail) is never leaked to the caller;
  * validation: 'texts' must be a non-empty list of strings, else 400; a wrong route is 404.

Offline + hermetic: isolated SPENDGUARD_HOME set before import (no re-exec); adapters.embed is mocked; the server runs
on an ephemeral port in a thread.
"""
import os
import sys
import tempfile
import threading
import json
import urllib.request
import urllib.error

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-serve-embed-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard                               # noqa: E402
from spendguard import serve, adapters, gate    # noqa: E402

fails = []


def ck(label, cond, extra=""):
    print(f"  [{'OK' if cond else 'FAIL'}] {label}" + (f"  — {extra}" if (extra and not cond) else ""))
    if not cond:
        fails.append(label)


def _req(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# a canned embed result shaped exactly like adapters.embed's contract — INCLUDING the host-local checkpoint we must NOT leak
def _fake_embed_ok(texts, **kw):
    return {"vectors": [[0.1, 0.2, 0.3] for _ in texts], "model": kw.get("model") or "text-embedding-3-small",
            "dims": 3, "n": len(texts), "checkpoint": "/Users/someone/.spendguard/embed_abc.jsonl",
            "failed": [], "error": None}


httpd = serve.make_server("127.0.0.1", 0, None)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
try:
    adapters.embed = _fake_embed_ok
    s, b = _req(port, "POST", "/embed", {"texts": ["a", "b"]})
    ck("POST /embed → 200 with vectors aligned to inputs", s == 200 and len(b.get("vectors", [])) == 2, f"status={s}")
    ck("the host-local checkpoint path is NEVER exposed to the caller", "checkpoint" not in b)
    ck("carries model + dims + n", b.get("model") == "text-embedding-3-small" and b.get("dims") == 3 and b.get("n") == 2)

    s, _ = _req(port, "POST", "/embed", {})
    ck("POST /embed without 'texts' → 400", s == 400)
    s, _ = _req(port, "POST", "/embed", {"texts": "not-a-list"})
    ck("POST /embed with a non-list 'texts' → 400", s == 400)
    s, _ = _req(port, "POST", "/embed", {"texts": []})
    ck("POST /embed with an empty list → 400", s == 400)
    s, _ = _req(port, "POST", "/embed", {"texts": [1, 2]})
    ck("POST /embed with non-string items → 400", s == 400)
    s, _ = _req(port, "POST", "/nope", {"texts": ["a"]})
    ck("an unknown POST route → 404", s == 404)

    def _refuse_budget(texts, **kw):
        raise spendguard.BudgetRefused(0.5, 0.1, {"openai": 0.5})
    adapters.embed = _refuse_budget
    s, b = _req(port, "POST", "/embed", {"texts": ["a"]})
    ck("a BudgetRefused → 402 carrying the estimate (never a false 200)", s == 402 and b.get("estimate") == 0.5, f"status={s}")

    def _refuse_cap(texts, **kw):
        raise gate.SpendGateRefused("daily cap breached")
    adapters.embed = _refuse_cap
    s, _ = _req(port, "POST", "/embed", {"texts": ["a"]})
    ck("a cap-breach spend stop → 402 (not 500, not a false 200)", s == 402)
finally:
    httpd.shutdown()
    httpd.server_close()


# a PARTIAL failure is returned honestly as 200: vectors aligned (None where failed), failed[] + error populated
httpd2 = serve.make_server("127.0.0.1", 0, None)
port2 = httpd2.server_address[1]
threading.Thread(target=httpd2.serve_forever, daemon=True).start()
try:
    def _partial(texts, **kw):
        return {"vectors": [[0.1], None], "model": "text-embedding-3-small", "dims": 1, "n": 2,
                "checkpoint": None, "failed": [{"i": 1, "reason": "input too big"}], "error": "1/2 inputs unembedded"}
    adapters.embed = _partial
    s, b = _req(port2, "POST", "/embed", {"texts": ["a", "b"]})
    ck("a partial failure is 200 with vectors aligned (None where failed) + failed[] + error",
       s == 200 and b["vectors"][1] is None and bool(b["failed"]) and bool(b["error"]), f"status={s} body={b}")
finally:
    httpd2.shutdown()
    httpd2.server_close()


# safety carried over from /ask: a network-exposed bind without a token refuses to start
refused = False
try:
    serve.make_server("0.0.0.0", 0, None)
except RuntimeError:
    refused = True
ck("a network-exposed host (0.0.0.0) without a token REFUSES to start", refused)

print(f"\n{'[FAIL]' if fails else 'OK'} test_serve_embed_route: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
