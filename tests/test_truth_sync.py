"""Provider-truth sync (truth.py) — day totals from the report's own fetchers; keys never leave the
machine; push honors visibility and tolerates a server without /v1/truth. Offline, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-truth-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import truth, report, saas
from spendguard import reconcile_anthropic as anth

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

report.openai_by_day = lambda: ({"2026-07-01": 12.5, "2026-07-02": 0.0}, 0)
anth.cost_by_day = lambda since=None: ({"2026-07-01": 3.25, "2026-07-03": 8.0}, {})
report.gpu_by_day = lambda since: ({"2026-07-02": 4.0}, None)   # REAL shape: (by_day, error)

rs = truth.rows(since="2026-07-01")
ck("all three sources merged", {r["provider"] for r in rs} == {"openai", "anthropic", "vastai"})
ck("zero-$ days dropped", not any(r["usd"] == 0 for r in rs))
ck("since respected + day/provider/usd shape", all(r["day"] >= "2026-07-01" and set(r) == {"day", "provider", "usd"} for r in rs))
ck("totals faithful", abs(sum(r["usd"] for r in rs) - 27.75) < 1e-9)

sent = {}
saas.conn = lambda: {"visibility": "org"}
saas._request = lambda m, p, payload=None: sent.update(m=m, p=p, payload=payload) or {"accepted": len(payload["truth"])}
res = truth.push(since="2026-07-01")
ck("push posts /v1/truth with the rows", sent["m"] == "POST" and sent["p"] == "/v1/truth" and len(sent["payload"]["truth"]) == 4)
ck("push returns server result", res == {"accepted": 4})

saas.conn = lambda: {"visibility": "private"}
ck("visibility=private → nothing leaves", "skipped" in truth.push())

saas.conn = lambda: {"visibility": "org"}
def _404(m, p, payload=None):
    raise RuntimeError("HTTP 404 not found")
saas._request = _404
ck("server without /v1/truth → friendly skip, no raise", "skipped" in truth.push(since="2026-07-01"))

import inspect
from spendguard import cli
ck("CLI wired: `spendguard truth`", '"truth"' in inspect.getsource(cli.main))

print(("[OK]" if not fails else "[FAIL]") + " truth-sync: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
