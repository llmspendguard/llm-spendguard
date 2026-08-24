"""`spendguard mcp` — the model-advisor over the Model Context Protocol (stdio), so ANY MCP client (Claude Code,
Claude Desktop, an IDE agent) can ask spendguard, from inside a repo, "which models are the most cost-effective
for THIS kind of job, at the quality it needs?" — answered from the caller's OWN measured usage, not a vendor's
marketing.

Tools (P1 — read-only, $0, from data spendguard already has):
  • spendguard_advise    {intent?, plan?, as_of?}  → per-(vendor:model) cost×quality ranking for an intent
                          (jobs · $total · $/M-out · good% · $/good), best-first, with the pick and caveats.
  • spendguard_models    {}                        → the actionable model catalogue (curated + your verified
                          prices), each with its per-1M rates and provider.

Later phases add spendguard_recommend (agentic top-K on the cost×quality frontier + an intent quality-bar) and
spendguard_bakeoff (gated, estimate-first head-to-head over a task sample that fills quality for untried models).

Transport: newline-delimited JSON-RPC 2.0 over stdio — stdlib only (no MCP SDK dependency), matching spendguard's
portable, self-contained surfaces (see serve.py). `handle(request)` is a pure function (request dict → response
dict|None) so the protocol is unit-tested without a live pipe. Read-only tools do NOT spend, so this server does
not require the gate; a spending tool (bakeoff) will `require()` before it runs.
"""
import json
import sys

PROTOCOL_VERSION = "2024-11-05"     # echoed back to the client if it names one; this is the safe default


# ── tool implementations (each returns a JSON-able dict; the dispatcher wraps it in MCP content) ──

def _tool_advise(args):
    """Cost×quality ranking for an intent, from the local `calls` corpus — wraps advise.ranked (the same
    computation the `advise` CLI prints), so the MCP answer and the CLI can never disagree."""
    from . import advise
    r = advise.ranked(intent=args.get("intent"), as_of=args.get("as_of"))
    caveats = []
    if not r["labeled"]:
        caveats.append("no quality labels yet for this scope — ranked by COST only; run `spendguard reconstruct` "
                       "(or a bakeoff) to earn good% / $-per-good.")
    caveats.append(f"{sum(m['jobs'] for m in r['models'])} jobs; confounds possible — a head-to-head bakeoff on a "
                   "fixed sample confirms it (history proposes, a bakeoff disposes).")
    plan = args.get("plan")
    if plan and r["models"]:
        pick = next((m for m in r["models"] if m["id"] == plan or m["model"] == plan), None)
        if pick and r["pick"] and pick["id"] != r["pick"]:
            best = r["models"][0]
            if r["labeled"] and pick["per_good"] is not None and best["per_good"]:
                caveats.append(f"your plan {pick['id']}: {(pick['per_good']-best['per_good'])/best['per_good']*100:.0f}% "
                               f"costlier per good result than {best['id']}.")
    return {"scope": r["scope"], "ranked_by": r["metric"], "pick": r["pick"], "models": r["models"], "caveats": caveats}


def _catalogue():
    """The ACTIONABLE model catalogue: spendguard's curated prices (_FALLBACK) + the caller's own verified prices
    (prices.json), each with per-1M rates + provider. The full provider universe (hundreds of ids) is enumerated
    on demand by the bakeoff, never dumped here — this is the 'known, priced options' set."""
    import os
    from . import pricing
    seen, out = set(), []

    def add_priced_model(model, rates, source):
        prov = (rates or {}).get("provider") or pricing.PROVIDERS.get(model) or pricing.PROVIDERS.get(pricing.normalize(model))
        key = f"{prov}:{model}"
        if key in seen:
            return
        seen.add(key)
        try:
            p = pricing.price(model)
        except Exception:
            p = rates or {}
        out.append(dict(id=key, model=model, provider=prov,
                        in_usd_per_m=p.get("in_"), out_usd_per_m=p.get("out"), source=source))

    for m, r in getattr(pricing, "_FALLBACK", {}).items():
        add_priced_model(m, r, "curated")
    try:
        path = pricing.user_prices_path()
        if os.path.exists(path):
            data = json.loads(open(path).read())
            for prov, pv in (data.get("providers") or {}).items():
                for m, r in (pv.get("models") or {}).items():
                    add_priced_model(m, {**(r or {}), "provider": prov}, "verified")
    except Exception:
        pass
    return sorted(out, key=lambda d: (d["provider"] or "~", d["model"]))


def _tool_models(args):
    cat = _catalogue()
    return {"count": len(cat), "models": cat,
            "note": "curated + your verified prices. Untried models beyond this are enumerated + measured by a bakeoff."}


# name → (description, JSON-Schema for arguments, handler)
_TOOLS = {
    "spendguard_advise": (
        "Rank the models you have ALREADY used for a job-type ('intent') by cost-effectiveness at the quality it "
        "held: $/good-result where quality is labeled, else $/M output. Returns the ranked models, the pick, and "
        "caveats. Data-driven from your own local ledger; $0, no spend.",
        {"type": "object", "properties": {
            "intent": {"type": "string", "description": "the job-type to rank for (e.g. 'loinc-typing'); omit for all intents"},
            "plan": {"type": "string", "description": "a model you're about to use ('vendor:model' or bare) — shows the delta vs the pick"},
            "as_of": {"type": "string", "description": "YYYY-MM-DD — replay the ranking as of a past date (backtest)"}},
         "additionalProperties": False},
        _tool_advise),
    "spendguard_models": (
        "List the actionable model catalogue spendguard knows: curated + your own verified prices, each with its "
        "per-1M input/output rates and provider. $0.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_models),
}


# ── JSON-RPC / MCP protocol ──

def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _tool_result(payload, is_error=False):
    """An MCP tools/call result: the JSON payload as a text content block (universally readable) AND as
    structuredContent (for clients that consume it typed). isError flags a tool-level failure without breaking
    the protocol frame."""
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "structuredContent": payload if isinstance(payload, dict) else {"result": payload},
            "isError": bool(is_error)}


def handle(req):
    """One JSON-RPC request dict → its response dict, or None for a notification (no id → no reply). Pure and
    side-effect-free apart from the tool call itself, so the whole protocol is testable without a live pipe."""
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0":
        return _err(req.get("id") if isinstance(req, dict) else None, -32600, "invalid JSON-RPC 2.0 request")
    method, rid = req.get("method"), req.get("id")
    is_notification = "id" not in req
    if method == "initialize":
        import spendguard
        client_ver = (req.get("params") or {}).get("protocolVersion")
        return _ok(rid, {"protocolVersion": client_ver or PROTOCOL_VERSION,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "spendguard", "version": getattr(spendguard, "__version__", "0")}})
    if method in ("notifications/initialized", "initialized"):
        return None                                    # a notification — acknowledged by silence
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _fn) in _TOOLS.items()]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        entry = _TOOLS.get(name)
        if not entry:
            return _err(rid, -32602, f"unknown tool {name!r} — have: {', '.join(_TOOLS)}")
        _d, _s, fn = entry
        try:
            payload = fn(params.get("arguments") or {})
            return _ok(rid, _tool_result(payload))
        except Exception as e:
            # a TOOL failure is reported as an isError result (the model can read it), not a transport error
            return _ok(rid, _tool_result({"error": f"{type(e).__name__}: {e}"}, is_error=True))
    if is_notification:
        return None                                    # unknown notification → ignore, never reply
    return _err(rid, -32601, f"method not found: {method}")


def serve_stdio(inp=None, outp=None):
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout — the MCP stdio transport. Blocking;
    ends on EOF. A malformed line gets a parse-error reply and the loop continues (one bad frame never kills the
    server)."""
    inp = inp or sys.stdin
    outp = outp or sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            outp.write(json.dumps(_err(None, -32700, f"parse error: {e}")) + "\n"); outp.flush()
            continue
        resp = handle(req)
        if resp is not None:
            outp.write(json.dumps(resp, default=str) + "\n"); outp.flush()


def cmd(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard mcp",
                                 description="Model-advisor over MCP (stdio JSON-RPC). Point an MCP client at "
                                             "`spendguard mcp`.")
    ap.parse_args(argv)
    serve_stdio()
    return 0
