"""Raw stdlib-urllib capture — the last un-patched HTTP client, made visible. Script-style, offline (localhost only).

http_capture patched httpx + requests but NOT urllib.request, so a daemon making raw `urllib` provider calls
(namer/deps/semantic-index-style) called spendguard.require() (gate enforcing for the SDKs) yet its actual API
POSTs slipped past metering entirely. This guards the fix — patching OpenerDirector.open (the ONE chokepoint every
urlopen()/build_opener().open() funnels through) — and every safety property it must hold:

  • a billable POST to a known provider host is CAPTURED into the same realtime ledger …
  • … reached via ANY caller style (urllib.request.urlopen, `from urllib.request import urlopen`, build_opener);
  • the read-once HTTPResponse body is RE-SERVED so the caller still gets it;
  • a GET (list/reconcile — never bills) records nothing; SDK-originated + reading_history + off-knob suppressed;
  • a stream (SSE), an oversized body, or an unknown-length body is NOT drained — logged VISIBLE, never $0-silent;
  • install() is idempotent (no double-capture), and urlopen itself is left un-patched (patching both double-counts).

Recorder + logger are stubbed, so nothing touches a real ledger (zero spend). The server is a localhost stub.
"""
import os
import sys
import tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-urllib-")
    # Re-exec under the isolated home. Contain the spawn: resolve THIS file's OWN path (never an externally
    # supplied sys.argv) and require it to sit under this test directory before spawning — the agent-safety rule.
    _self = os.path.realpath(__file__)
    _root = os.path.realpath(os.path.dirname(__file__)) + os.sep
    if not _self.startswith(_root):
        raise SystemExit("refusing to re-exec a path outside the test directory: %s" % _self)
    os.execv(sys.executable, [sys.executable, _self])

import json
import threading
import urllib.request
from urllib.request import urlopen as bound_urlopen        # the `from … import` binding a urlopen-patch would MISS
from http.server import BaseHTTPRequestHandler, HTTPServer

from spendguard import gate as spend_gate
from spendguard import http_capture, budget

fails = []


def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


# stub the recorder + logger so nothing hits a real ledger (offline, zero spend)
recorded, logged = [], []
spend_gate._record_rt = lambda model, kw, i, o, **k: recorded.append((model, i, o, k.get("provider")))
spend_gate._log = lambda rec: logged.append(rec)

USAGE = {"model": "gpt-4o-mini", "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/stream":                    # a streaming response — carries NO Content-Length; never drained
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()                           # (HTTP/1.0 default → connection close delimits the body)
            self.wfile.write(b"data: {}\n\n")
            return
        b = json.dumps(USAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        b = json.dumps({"data": []}).encode()            # a list/reconcile read — carries no usage, never bills
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


srv = HTTPServer(("127.0.0.1", 0), H)
BASE = "http://127.0.0.1:%d" % srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(url, opener=None):
    req = urllib.request.Request(url, data=b"{}", method="POST")
    r = opener.open(req) if opener else urllib.request.urlopen(req)
    return json.loads(r.read())                          # caller reads the RE-SERVED body


# ── the patch is installed on the ONE chokepoint, and NOT on urlopen (double-patch → double-count) ──
ck("install() wires urllib via OpenerDirector.open",
   http_capture.install() is True and getattr(urllib.request.OpenerDirector.open, "_spend_gated", False))
ck("urlopen itself is left un-patched (patching both would double-count)",
   not getattr(urllib.request.urlopen, "_spend_gated", False))

# treat the localhost stub as a provider host so the capture path engages
http_capture.PROVIDER_HOSTS = dict(http_capture.PROVIDER_HOSTS, **{"127.0.0.1": "openai"})

# ── a billable POST is captured, and the read-once body is re-served intact ──
n = len(recorded)
body = post(BASE + "/v1/chat/completions")
ck("raw urllib POST to a provider host is CAPTURED (100/50, openai)",
   len(recorded) == n + 1 and recorded[-1] == ("gpt-4o-mini", 100, 50, "openai"))
ck("caller still receives the FULL body (read-once re-served)", body.get("usage", {}).get("prompt_tokens") == 100)

# ── coverage: every urllib caller style funnels through OpenerDirector.open ──
n = len(recorded)
r = bound_urlopen(urllib.request.Request(BASE + "/v1/embeddings", data=b"{}", method="POST"))
r.read()
ck("`from urllib.request import urlopen` caller captured (urlopen-patch would miss this)", len(recorded) == n + 1)

n = len(recorded)
post(BASE + "/v1/chat/completions", opener=urllib.request.build_opener())
ck("build_opener().open() caller captured (urlopen-patch would miss this)", len(recorded) == n + 1)

# ── a GET (list/reconcile) records nothing AND logs nothing (POST-only, before any log) ──
n, l = len(recorded), len(logged)
urllib.request.urlopen(BASE + "/v1/batches").read()
ck("urllib GET to a provider host records nothing (reconcile/list reads never bill)",
   len(recorded) == n and len(logged) == l)

# ── a stream (no Content-Length) is NOT drained: logged visible, and the caller still gets its stream ──
n, l = len(recorded), len(logged)
r = urllib.request.urlopen(urllib.request.Request(BASE + "/v1/stream", data=b"{}", method="POST"))
sse = r.read()
ck("streaming POST is not recorded as usage", len(recorded) == n)
ck("streaming POST logged VISIBLE (unmetered_unbuffered), not $0-silent",
   len(logged) == l + 1 and logged[-1].get("decision") == "recorded_unmetered_unbuffered")
ck("streaming caller still receives its body (capture never drained it)", sse == b"data: {}\n\n")

# ── an oversized body (Content-Length > cap) is NOT drained: logged visible, caller body intact ──
save_cap = http_capture._MAX_CAPTURE_BYTES
http_capture._MAX_CAPTURE_BYTES = 5                      # tiny → a normal response now exceeds the cap
n, l = len(recorded), len(logged)
big = post(BASE + "/v1/chat/completions")
ck("oversized (Content-Length > cap) not recorded (no OOM read)", len(recorded) == n)
ck("oversized logged VISIBLE (unmetered_oversized)",
   len(logged) == l + 1 and logged[-1].get("decision") == "recorded_unmetered_oversized")
ck("oversized: caller body untouched (capture never drained it)", big.get("usage", {}).get("prompt_tokens") == 100)
http_capture._MAX_CAPTURE_BYTES = save_cap

# ── suppressions: reading_history (a provider read, not spend), SDK-originated, and the off knob ──
n = len(recorded)
with budget.reading_history("test-reconcile-read"):
    urllib.request.urlopen(urllib.request.Request(BASE + "/v1/chat/completions", data=b"{}", method="POST")).read()
ck("a POST inside reading_history() is NOT recorded (history read, not spend)", len(recorded) == n)

http_capture.install()
http_capture.install()                                  # idempotent — must not wrap twice
n = len(recorded)
post(BASE + "/v1/chat/completions")
ck("install() is idempotent — a POST is captured ONCE, never multiplied", len(recorded) == n + 1)

tok = http_capture.in_sdk_call.set(True)
n = len(recorded)
post(BASE + "/v1/chat/completions")
http_capture.in_sdk_call.reset(tok)
ck("SDK-originated traffic suppressed (in_sdk_call) — no double count", len(recorded) == n)

os.environ["SPENDGUARD_HTTP_CAPTURE"] = "off"
n = len(recorded)
post(BASE + "/v1/chat/completions")
del os.environ["SPENDGUARD_HTTP_CAPTURE"]
ck("knob SPENDGUARD_HTTP_CAPTURE=off disables urllib capture", len(recorded) == n)

srv.shutdown()
print(("[OK]" if not fails else "[FAIL]") + " urllib capture: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
