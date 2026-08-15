"""`spendguard serve` — the cross-LLM ask surface over localhost HTTP, so ANY tool or language (not just Python)
can run an honest, gated, governed cross-LLM query through spendguard.

  POST /ask      {prompt, vendors?, n?, schema?, system?, mode?, budget_usd?, deadline_s?, require?, purpose?}
                 → AskResult.as_dict() (answers carry text; a failed vendor carries its kind, never text)
  GET  /health   → {ok, version}
  GET  /metadata → model-metadata backbone health (the LiteLLM limits cache + measured-cap drift)

Every /ask runs through spendguard.ask → the dispatch governor (bounded concurrency / queue) + estimate-first
budget admission + the Result contract that makes a failure impossible to read as an answer.

SECURITY. This endpoint SPENDS money and rides your gate/keys, so it is localhost-only by default. Binding a
network-exposed host REQUIRES a Bearer token (SPENDGUARD_SERVE_TOKEN) or it refuses to start — an open one is a
spend-and-exfiltrate hole.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_MAX_BODY = 2_000_000            # 2 MB request-body cap — a prompt is text, not a file upload


def _requires_token(host):
    """A non-loopback bind is network-exposed, so it MUST carry a Bearer token: this endpoint spends money and
    uses your keys, and an unauthenticated one on a reachable interface is a spend-and-exfiltrate hole."""
    return host not in _LOOPBACK


class _AskHTTPHandler(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        tok = getattr(self.server, "sg_token", None)
        if not tok:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {tok}"

    def log_message(self, *a):   # quiet: never log request lines — a prompt could ride the path/query
        pass

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized — Authorization: Bearer <SPENDGUARD_SERVE_TOKEN>"})
        if self.path == "/health":
            import spendguard
            return self._send(200, {"ok": True, "version": spendguard.__version__})
        if self.path == "/metadata":
            from . import metadata_audit
            return self._send(200, metadata_audit.backbone_health())
        return self._send(404, {"error": f"no route {self.path} — GET /health · /metadata · POST /ask"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if self.path != "/ask":
            return self._send(404, {"error": f"no route {self.path} — POST /ask"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > _MAX_BODY:
                return self._send(413, {"error": f"body too large (> {_MAX_BODY} bytes)"})
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad JSON body: {e}"})
        if not req.get("prompt"):
            return self._send(400, {"error": "'prompt' is required"})
        import spendguard
        try:
            r = spendguard.ask(
                req["prompt"], vendors=req.get("vendors"), n=req.get("n"), schema=req.get("schema"),
                system=req.get("system"), purpose=req.get("purpose", "serve:ask"),
                deadline_s=req.get("deadline_s"), budget_usd=req.get("budget_usd"),
                mode=req.get("mode", "all"), require=req.get("require"))
        except spendguard.BudgetRefused as e:
            return self._send(402, {"error": str(e), "estimate": e.estimate, "budget": e.budget, "detail": e.detail})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})
        return self._send(200, r.as_dict())


def make_server(host=DEFAULT_HOST, port=DEFAULT_PORT, token=None):
    """Build (but do not start) the HTTP server. Refuses a network-exposed host without a token. Exposed for
    tests, which drive it on an ephemeral port in a thread; `serve()` wraps it for the CLI."""
    if _requires_token(host) and not token:
        raise RuntimeError(f"refusing to bind a network-exposed host ({host}) without a token — this endpoint "
                           f"spends money and uses your keys. Set SPENDGUARD_SERVE_TOKEN, or bind 127.0.0.1.")
    httpd = ThreadingHTTPServer((host, port), _AskHTTPHandler)
    httpd.sg_token = token
    return httpd


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT, token=None):
    """Start the localhost cross-LLM ask server (blocking; Ctrl-C to stop)."""
    import spendguard
    spendguard.require()         # the server SPENDS — fail closed if this interpreter is not gated
    httpd = make_server(host, port, token)
    print(f"spendguard ask server on http://{host}:{port}  (POST /ask · GET /health · /metadata)"
          + ("  [Bearer token required]" if token else "  [localhost, no auth]"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def cmd(argv=None):
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="spendguard serve",
                                 description="Cross-LLM ask surface over localhost HTTP (POST /ask).")
    ap.add_argument("--host", default=DEFAULT_HOST, help="bind host (default 127.0.0.1; a non-loopback host needs a token)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    a = ap.parse_args(argv)
    try:
        serve(host=a.host, port=a.port, token=os.environ.get("SPENDGUARD_SERVE_TOKEN"))
    except RuntimeError as e:
        print(f"serve: {e}", file=sys.stderr)
        return 1
    return 0
