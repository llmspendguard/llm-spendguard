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


def _tool_recommend(args):
    """Agentic top-K on the cost×quality frontier for an intent, with an intent-set quality bar. This tool
    SPENDS a small, meta-capped LLM synthesis, so it fails closed if the interpreter is not gated, estimates
    first, and refuses when the estimate exceeds `budget_usd`."""
    import spendguard
    spendguard.require()                    # this tool spends → fail closed if the gate is not enforcing here
    from . import advisor
    intent, k, qbar = args.get("intent"), int(args.get("k") or 5), args.get("quality_bar")
    est = advisor.recommend_models(intent=intent, k=k, quality_bar=qbar, run=False)
    if est.get("requests", 0) == 0:
        return est                          # no evidence for this intent — nothing to rank, $0
    budget = args.get("budget_usd")
    if budget is not None and est.get("cost", 0.0) > float(budget):
        return {**est, "refused": True,
                "note": f"estimate ~${est['cost']:.4f} exceeds budget_usd ${float(budget):.4f} — not run. Raise budget_usd."}
    return advisor.recommend_models(intent=intent, k=k, quality_bar=qbar, run=True)


def _tool_bakeoff(args):
    """Measure cost×quality for a candidate slate on a sample of an intent's tasks, so untried models earn a
    $/good. A bakeoff makes REAL, metered workload calls — so it fails closed if not gated, and (crucially) it
    only RUNS when the caller passes an explicit `budget_usd`; without one it returns the ESTIMATE only, never
    spending on its own."""
    import spendguard
    spendguard.require()                    # real spend → fail closed if the gate is not enforcing here
    from . import bakeoff as _bk
    kw = dict(intent=args.get("intent"), candidates=args.get("candidates"),
              prompts=args.get("prompts"), sample_n=int(args.get("sample_n") or 5))
    budget = args.get("budget_usd")
    if budget is None:                       # no budget → NEVER auto-spends; a bakeoff bills real workload $
        est = _bk.bakeoff(run=False, **kw)
        if not est.get("error"):
            est["note"] = ("estimate only — pass budget_usd to actually RUN the bakeoff (it makes real, metered "
                           "calls, preferring $0 lanes where available).")
        return est
    return _bk.bakeoff(run=True, budget_usd=float(budget), **kw)


# ── Claude Code spend + compaction tools (read-only, $0). spendguard's own functions PRINT human text, which would
#    corrupt JSON-RPC on stdout — so any handler that calls one wraps it in redirect_stdout(). ─────────────────────
def _cc_db():
    import sqlite3
    from . import config
    return sqlite3.connect(config.db_path())


def _cc_titles():
    try:
        from . import claudecode
        return claudecode._sidebar_titles()
    except Exception:
        return {}


def _cc_label(conv, titles):
    return titles.get(conv) or (conv or "?")[:40]


def _tool_spend_overview(_args):
    con = _cc_db()
    try:
        est = con.execute("SELECT COALESCE(SUM(CAST(est_chat_usd AS REAL)),0) FROM spend_events WHERE source='claude-code'").fetchone()[0]
        over = con.execute("SELECT COALESCE(SUM(CAST(realtime_usd AS REAL)),0) FROM spend_events WHERE source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%'").fetchone()[0]
        sub = con.execute("SELECT COALESCE(SUM(CAST(subscription_usd AS REAL)),0) FROM spend_events WHERE source='anthropic-invoice'").fetchone()[0]
        api = con.execute("SELECT COALESCE(SUM(CAST(realtime_usd AS REAL)),0) FROM spend_events WHERE source='anthropic-invoice-api'").fetchone()[0]
    finally:
        con.close()
    return {"note": "Real $ (money out the door) and est-value (plan-covered usage worth) are SEPARATE axes — never summed.",
            "real_usd": {"subscription_base": round(sub, 2), "claude_code_overage": round(over, 2),
                         "api_credits": round(api, 2), "total_real": round(sub + over + api, 2)},
            "est_value_usd": {"claude_code_plan_covered": round(est, 2)}}


def _tool_overage_status(_args):
    import contextlib
    import io
    import time
    from . import claudecode
    with contextlib.redirect_stdout(io.StringIO()):
        windows, _anchor = claudecode._overage_windows(claudecode._overage_events())
    now = time.time()
    con = _cc_db()
    try:
        month = con.execute("SELECT substr(occurred_at,1,7) mo, ROUND(SUM(CAST(realtime_usd AS REAL)),2) FROM spend_events "
                            "WHERE source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%' GROUP BY mo ORDER BY mo DESC").fetchall()
    finally:
        con.close()
    return {"on_overage_now": any(b <= now < r for (b, r) in windows),
            "meaning": "true = the weekly plan cap is hit and this account is paying per-token right now",
            "observed_overage_windows": len(windows), "real_overage_by_month_usd": {m: v for m, v in month[:6]}}


def _tool_top_conversations(args):
    import contextlib
    import io
    by = (args.get("by") or "est").lower()
    limit = int(args.get("limit") or 10)
    titles = _cc_titles()
    if by in ("overage", "real"):
        from . import claudecode
        with contextlib.redirect_stdout(io.StringIO()):
            real = claudecode.attribute_overage(top=10 ** 6)
        rows = sorted((real or {}).items(), key=lambda x: -x[1])[:limit]
        return {"ranked_by": "real Claude Code overage $ (reconciled to invoices)",
                "conversations": [{"title": _cc_label(c, titles), "overage_usd": round(v, 2)} for c, v in rows]}
    con = _cc_db()
    try:
        rows = con.execute("SELECT conv_id, ROUND(SUM(CAST(est_chat_usd AS REAL)),2) v, COUNT(*) n, "
                          "MAX(COALESCE(in_tok,0)+COALESCE(cache_read_tok,0)+COALESCE(cache_write_tok,0)) mx "
                          "FROM spend_events WHERE source='claude-code' GROUP BY conv_id ORDER BY v DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return {"ranked_by": "est-value $ (what the usage is worth at API rates)",
            "conversations": [{"title": _cc_label(c, titles), "est_value_usd": v, "turns": n, "max_context_tokens": mx}
                              for c, v, n, mx in rows]}


def _tool_conversation_cost(args):
    conv = str(args.get("conversation_id") or "").strip()
    if not conv:
        return {"error": "pass conversation_id (a transcript uuid — from spendguard_top_conversations)"}
    con = _cc_db()
    try:
        est = con.execute("SELECT COALESCE(SUM(CAST(est_chat_usd AS REAL)),0), COUNT(*) FROM spend_events "
                         "WHERE source='claude-code' AND conv_id=?", (conv,)).fetchone()
        over = con.execute("SELECT COALESCE(SUM(CAST(realtime_usd AS REAL)),0) FROM spend_events "
                          "WHERE source='claude-code-overflow' AND conv_id=?", (conv,)).fetchone()[0]
    finally:
        con.close()
    return {"conversation_id": conv, "title": _cc_label(conv, _cc_titles()),
            "est_value_usd_plan_covered": round(est[0], 2), "turns": est[1],
            "observed_overage_usd_upper_bound": round(over, 2),
            "note": "est-value and overage are separate axes; overage here is the observable upper bound"}


def _tool_compaction_candidates(args):
    import contextlib
    import io
    from . import claudecode, compaction
    limit = int(args.get("limit") or 10)
    with contextlib.redirect_stdout(io.StringIO()):
        cands, (k, _kn), stats = claudecode.compaction_candidates()
        snippet = compaction.compact_snippet()
    titles = _cc_titles()
    return {"measured_compaction_ratio_k": k, "scanned": stats.get("examined"), "flagged": stats.get("flagged"),
            "candidates": [{"title": _cc_label(c["conv_id"], titles), "current_context_tokens": c["current"], "turns": c["turns"],
                            "reread_usd_per_turn": round(c["recurring_read_usd_per_turn"], 4) if c.get("recurring_read_usd_per_turn") is not None else None,
                            "compacting_saves_usd_per_turn": round(c["saved_usd_per_turn"], 4) if c.get("saved_usd_per_turn") is not None else None}
                           for c in cands[:limit]],
            "effective_compact_command": snippet,
            "note": "for a tailored, conversation-specific decision run `spendguard claude-code compact --tailor` (gated)"}


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
    "spendguard_recommend": (
        "Recommend the best K models for a job-type ('intent') on the cost×quality frontier: the reasoner infers "
        "how much precision the job needs (or honors your quality_bar) and ranks the CHEAPEST models that MEET "
        "that bar, each with the measured $/good. Ranks from your OWN evidence (a bakeoff adds untried models). "
        "SPENDS a small, meta-capped LLM call — estimates first and refuses over budget_usd.",
        {"type": "object", "properties": {
            "intent": {"type": "string", "description": "the job-type to recommend models for"},
            "k": {"type": "integer", "description": "how many models to return (default 5)"},
            "quality_bar": {"type": "string", "description": "optional: force the bar — 'precision-critical' | 'balanced' | 'cost-first'"},
            "budget_usd": {"type": "number", "description": "optional: refuse if the estimated LLM cost exceeds this"}},
         "additionalProperties": False},
        _tool_recommend),
    "spendguard_bakeoff": (
        "Measure cost×quality for a SLATE of candidate models on a sample of a job-type's ('intent') real tasks "
        "— judges each output and RECORDS the result, so untried models earn a $/good and appear in "
        "spendguard_advise / spendguard_recommend afterwards. Makes REAL metered calls (preferring $0 lanes): "
        "with NO budget_usd it returns the ESTIMATE only; pass budget_usd to actually run (it refuses over it).",
        {"type": "object", "properties": {
            "intent": {"type": "string", "description": "the job-type to bake off for"},
            "candidates": {"type": "array", "items": {"type": "string"},
                           "description": "the slate to test, as 'vendor:model' ids (spendguard_recommend suggests one)"},
            "prompts": {"type": "array", "items": {"type": "string"},
                        "description": "optional: representative tasks to replay; omit to auto-sample the intent's recorded prompts"},
            "sample_n": {"type": "integer", "description": "how many recorded prompts to replay when auto-sampling (default 5)"},
            "budget_usd": {"type": "number", "description": "run only if the estimate fits this; OMIT to get just the estimate"}},
         "additionalProperties": False},
        _tool_bakeoff),
    "spendguard_spend_overview": (
        "The headline for THIS account: REAL $ out the door (subscription base + Claude Code overage + API "
        "credits) shown SEPARATELY from est-value (plan-covered Claude Code usage, what it's worth at API "
        "rates). The two axes are never summed. $0, read-only.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_spend_overview),
    "spendguard_overage_status": (
        "Are we on PAID overage right now — i.e. the weekly subscription cap is hit and this account is billing "
        "per-token — from observable transcript signals, plus reconciled real overage $ by month. $0, read-only.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_overage_status),
    "spendguard_top_conversations": (
        "Rank Claude Code conversations by est-value (default) or by real overage $ (by='overage', reconciled to "
        "invoices), each labeled with its sidebar title. Answers 'what used the tokens'. $0, read-only.",
        {"type": "object", "properties": {
            "by": {"type": "string", "enum": ["est", "overage"], "description": "'est' = plan-covered est-value (default); 'overage' = real paid overage $"},
            "limit": {"type": "integer", "description": "how many to return (default 10)"}},
         "additionalProperties": False},
        _tool_top_conversations),
    "spendguard_conversation_cost": (
        "Cost of ONE conversation by its transcript id: plan-covered est-value + observed overage upper bound "
        "(the two axes kept separate). Get ids from spendguard_top_conversations. $0, read-only.",
        {"type": "object", "properties": {
            "conversation_id": {"type": "string", "description": "the transcript uuid (conv_id) to price"}},
         "required": ["conversation_id"], "additionalProperties": False},
        _tool_conversation_cost),
    "spendguard_compaction_candidates": (
        "Open conversations that are expensive to keep alive — large re-read context billing every turn — with "
        "the $/turn cost, what compacting would save, and the ready-to-paste effective /compact command. "
        "Surfaces WHEN compacting a conversation is a good trade. $0, read-only.",
        {"type": "object", "properties": {
            "limit": {"type": "integer", "description": "how many candidates to return (default 10)"}},
         "additionalProperties": False},
        _tool_compaction_candidates),
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
                         "serverInfo": {"name": "spendguard", "version": getattr(spendguard, "__version__", "0")},
                         # `instructions` is the MCP-standard way a server tells the client how to use it — so the
                         # surface is self-documenting, not something a caller has to reverse-engineer.
                         "instructions": (
                             "spendguard's model-advisor, answered from THIS machine's own measured LLM usage.\n"
                             "• spendguard_advise(intent) — rank models you've USED for a job-type by $/good "
                             "(cost at the quality it held). $0.\n"
                             "• spendguard_models() — the priced catalogue. $0.\n"
                             "• spendguard_recommend(intent, k, quality_bar?, budget_usd?) — agentic top-K on the "
                             "cost×quality frontier; infers how much precision the job needs. SPENDS a small, "
                             "meta-capped LLM call — it estimates first and refuses over budget_usd.\n"
                             "• spendguard_bakeoff(intent, candidates, budget_usd?) — measure cost×quality for a "
                             "SLATE of untried models on a sample of the intent's tasks; records the result so "
                             "advise/recommend include them after. Makes REAL metered calls — returns an estimate "
                             "unless you pass budget_usd.\n"
                             "An 'intent' is a job-type label (e.g. 'loinc-typing', 'code-review') — the same tag "
                             "your calls are recorded under. Every result carries its own `note`/`caveats` "
                             "explaining coverage and what to run next.\n"
                             "\nSPEND & CONVERSATIONS (from this account's Claude Code transcripts + reconciled "
                             "invoices — all $0, read-only):\n"
                             "• spendguard_spend_overview() — REAL $ out the door vs est-value (plan-covered), the "
                             "two axes kept separate, never summed.\n"
                             "• spendguard_overage_status() — are we on PAID overage right now (weekly cap hit → "
                             "billing per-token), plus real overage $ by month.\n"
                             "• spendguard_top_conversations(by, limit) — what used the tokens, ranked by est-value "
                             "or by real overage $.\n"
                             "• spendguard_conversation_cost(conversation_id) — price one conversation.\n"
                             "• spendguard_compaction_candidates(limit) — conversations expensive to keep alive, "
                             "the $/turn re-read cost, and the ready-to-paste /compact command to save it.")})
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


def register_client(remove=False):
    """Register (or unregister) spendguard as a stdio MCP server in the Claude Code client config, so its tools
    are reachable from every repo. Mirrors exactly how symgrep / 7thsense / ccwatch are registered — a top-level
    `mcpServers` entry in ~/.claude.json — and both the executable path (via receipt._spendguard_bin, resolved
    from the running install, never hardcoded) and the write (via config.update_json: atomic + emacs-style backup
    + never silently clobbering an unparseable file) are borrowed from the code that already does this well."""
    import pathlib

    from . import config
    from .receipt import _spendguard_bin

    p = pathlib.Path.home() / ".claude.json"
    key = "spendguard"
    if remove and not p.exists():
        print(f"nothing to remove — {p} does not exist")
        return 0

    def mutate(cfg):
        servers = cfg.setdefault("mcpServers", {})
        if remove:
            servers.pop(key, None)
        else:
            # `spendguard mcp` runs the stdio server on the SAME (gated) interpreter that owns this executable,
            # so the spend-capable advisor tools stay under the gate.
            servers[key] = {"type": "stdio", "command": _spendguard_bin(), "args": ["mcp"], "env": {}}
        return cfg

    # update_json returns None when it DECLINES the write (e.g. the existing config won't parse — it is left
    # intact rather than clobbered). Report that honestly instead of claiming a success that didn't happen.
    written = config.update_json(p, mutate, reason=("unregister" if remove else "register") + " spendguard MCP server")
    if written is None:
        print(f"could NOT update {p} (it exists but does not parse as JSON — left untouched). "
              f"Fix or remove that file, then re-run.")
        return 1
    if remove:
        print(f"unregistered spendguard MCP server from {p}")
    else:
        print(f"registered spendguard MCP server in {p}  (command: {_spendguard_bin()} mcp)")
        print("  → restart Claude Code (or reconnect MCP) to pick up all 9 tools: model-advisor + spend/compaction")
    return 0


def cmd(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard mcp",
                                 description="spendguard's tools over MCP (stdio JSON-RPC): the model-advisor plus "
                                             "read-only spend & compaction queries. Point an MCP client at "
                                             "`spendguard mcp`, or run `spendguard install-mcp` to register it in "
                                             "Claude Code.")
    ap.parse_args(argv)
    serve_stdio()
    return 0
