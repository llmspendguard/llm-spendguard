#!/usr/bin/env python
"""What model does the z.ai GLM Coding Plan ACTUALLY serve for each requested id?

The gap this answers: spendguard's ledger records the model it REQUESTED (adapters records `raw`), and zai_exec.
run_prompt discards the response's own `model` field — so neither the ledger nor stdout can tell you whether the
plan honored `glm-5.3` or silently served an older flagship (glm-4.6). This asks the endpoint directly: for each
requested id it prints the `model` the RESPONSE reports (the anthropic-shape response echoes what actually ran),
so a server-side alias is visible. Read-only, $0 on the flat plan (raw urllib, exactly like the lane — never the
metered API). Run under the gated venv.

  ./.venv.nosync/bin/python scripts/probe/zai_served_model.py
"""
import json
import sys
import time
import urllib.error
import urllib.request

import spendguard  # noqa: F401
spendguard.require()
from spendguard import zai_exec, config   # reuse the lane's OWN key/endpoint/version — no second source of truth

REQUESTED = ["glm-5.3", "glm-5.2", "glm-4.6"]      # newest → fallback → the id warden saw in stdout
_PROMPT = "Reply with exactly the word: OK"


def _probe(model):
    key = zai_exec._key()
    if not key:
        return {"requested": model, "error": f"no z.ai key ({zai_exec.KEY_ENV} or ZAI_API_KEY)"}
    body = {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": _PROMPT}]}
    req = urllib.request.Request(
        zai_exec._base_url().rstrip("/") + "/v1/messages", data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": key, "anthropic-version": zai_exec._ANTHROPIC_VERSION,
                 "content-type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, context=config.ssl_context(), timeout=60) as resp:
            d = json.loads(resp.read())
    except urllib.error.HTTPError as he:
        detail = ""
        try:
            detail = he.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        return {"requested": model, "error": f"HTTP {he.code}: {detail}", "latency": round(time.time() - t0, 2)}
    except Exception as e:
        return {"requested": model, "error": f"{type(e).__name__}: {str(e)[:120]}", "latency": round(time.time() - t0, 2)}
    text = "".join(b.get("text", "") for b in (d.get("content") or []) if b.get("type") == "text")
    return {"requested": model, "served": d.get("model"), "text": text.strip()[:40],
            "latency": round(time.time() - t0, 2)}


def main():
    print(f"z.ai coding plan @ {zai_exec._base_url()}  —  requested vs SERVED (response.model is what actually ran)\n")
    rows = [_probe(m) for m in REQUESTED]
    for r in rows:
        if r.get("error"):
            print(f"  {r['requested']:<10} → ERROR: {r['error']}")
        else:
            served = r.get("served")
            flag = "  ✓ honored" if served == r["requested"] else f"  ⚠ ALIASED → {served}"
            print(f"  {r['requested']:<10} → served={served!r:<12} {flag}   ({r['latency']}s, said {r['text']!r})")
    print("\nRule: if a requested id shows 'ALIASED', the plan silently served a different model — pin the id the "
          "plan actually honors, and know the ledger will still label rows by the REQUESTED id.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
