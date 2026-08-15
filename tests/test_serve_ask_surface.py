"""`spendguard serve` — the cross-LLM ask surface over localhost HTTP is honest and safe by default:

  * POST /ask returns the SAME honest coverage spendguard.ask gives in-process — partial coverage is usable, and
    a failed vendor is serialized with its kind + error, NEVER with text (no false success on the wire).
  * a network-exposed bind without a token REFUSES to start (this endpoint spends money + uses your keys).
  * a token-protected server rejects a request with no/bad Bearer.

Offline, isolated home; adapters.call is mocked, the server runs on an ephemeral port in a thread.
"""
import os
import sys
import tempfile
import threading
import json
import urllib.request
import urllib.error

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-serve-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import serve, adapters   # noqa: E402

fails = 0


def ck(label, cond, extra=""):
    global fails
    if not cond:
        fails += 1
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  — ' + extra) if extra and not cond else ''}")


_OK = {"text": '{"ok": 1}', "finish_reason": "stop", "in_tok": 10, "out_tok": 5, "cost": 0.01, "error": None}
_BROKE = {"text": None, "error": "APIConnectionError: reset", "error_type": "APIConnectionError"}
_MIX = {"anthropic": _OK, "openai": _OK, "moonshot": _BROKE, "zai": _OK}
adapters.call = lambda model, prompt, **kw: dict(_MIX[model.split(":", 1)[0]])

PANEL = ["anthropic:claude-opus-4-8", "openai:gpt-5.5", "moonshot:kimi-k3", "zai:glm-5.3"]


def _req(port, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ── the localhost server: honest /ask + /health ─────────────────────────────────────────────────────────────
httpd = serve.make_server("127.0.0.1", 0, None)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
try:
    s, h = _req(port, "GET", "/health")
    ck("GET /health → 200 ok", s == 200 and h.get("ok") is True)

    s, a = _req(port, "POST", "/ask", {"prompt": "hi", "vendors": PANEL, "deadline_s": 30})
    ck("POST /ask → 200 with honest partial coverage (3/4, complete False)", s == 200 and a["n_ok"] == 3 and a["complete"] is False)
    moon = [x for x in a["results"] if x["vendor"] == "moonshot"][0]
    ck("a failed vendor is serialized with kind+error, NEVER text (no false success on the wire)",
       "text" not in moon and moon["kind"] == "transport_error")
    oks = [x for x in a["results"] if x["kind"] == "ok"]
    ck("the OK vendors carry their answer text", all(x.get("text") == '{"ok": 1}' for x in oks) and len(oks) == 3)

    s, _ = _req(port, "POST", "/ask", {})
    ck("POST /ask without a prompt → 400", s == 400)
    s, _ = _req(port, "GET", "/nope")
    ck("an unknown route → 404", s == 404)
finally:
    httpd.shutdown()
    httpd.server_close()


# ── safety: a network-exposed host needs a token, and the token is enforced ──────────────────────────────────
refused = False
try:
    serve.make_server("0.0.0.0", 0, None)
except RuntimeError:
    refused = True
ck("a network-exposed host (0.0.0.0) without a token REFUSES to start", refused)

httpd2 = serve.make_server("127.0.0.1", 0, "sekret")
port2 = httpd2.server_address[1]
threading.Thread(target=httpd2.serve_forever, daemon=True).start()
try:
    s_noauth, _ = _req(port2, "GET", "/health")
    s_auth, _ = _req(port2, "GET", "/health", headers={"Authorization": "Bearer sekret"})
    ck("token server: no/bad Bearer → 401", s_noauth == 401)
    ck("token server: correct Bearer → 200", s_auth == 200)
finally:
    httpd2.shutdown()
    httpd2.server_close()

print(f"\n{'[FAIL]' if fails else 'OK'} test_serve_ask_surface: {fails} failure(s)")
sys.exit(1 if fails else 0)
