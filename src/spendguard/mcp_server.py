"""`spendguard mcp` — the model-advisor over the Model Context Protocol (stdio), so ANY MCP client (Claude Code,
Claude Desktop, an IDE agent) can ask spendguard, from inside a repo, "which models are the most cost-effective
for THIS kind of job, at the quality it needs?" — answered from the caller's OWN measured usage, not a vendor's
marketing.

Tools include these two core families (every read-only tool is $0, from data spendguard already has):

MODEL-ADVISOR (rank / recommend / prove models for a job-type):
  • spendguard_advise    {intent?, plan?, as_of?}  → per-(vendor:model) cost×quality ranking for an intent
                          (jobs · $total · $/M-out · good% · $/good), best-first, with the pick and caveats. $0.
  • spendguard_models    {}                        → the actionable model catalogue (curated + your verified
                          prices), each with its per-1M rates and provider. $0.
  • spendguard_recommend {intent, k?, quality_bar?, budget_usd?} → agentic top-K on the cost×quality frontier +
                          an intent quality-bar. SPENDS a small meta-capped call; estimates first, refuses over budget.
  • spendguard_bakeoff   {intent, candidates, prompts?, sample_n?, budget_usd?} → gated, estimate-first head-to-head
                          over a task sample that fills quality for untried models. No budget_usd → estimate only.

SPEND & COMPACTION (read-only, $0, over this account's Claude Code transcripts + reconciled invoices):
  • spendguard_spend_overview        {}                → REAL $ out the door vs est-value (plan-covered), the two
                                     axes kept SEPARATE, never summed.
  • spendguard_overage_status        {}                → on PAID overage right now? (weekly cap hit → billing per
                                     token) + real overage $ by month.
  • spendguard_top_conversations     {by?, limit?}     → what used the tokens, ranked by est-value or real overage $.
  • spendguard_conversation_cost     {conversation_id} → price one conversation (est-value + overage upper bound).
  • spendguard_compaction_candidates {limit?}          → conversations expensive to keep alive + the /compact command.

Transport: newline-delimited JSON-RPC 2.0 over stdio — stdlib only (no MCP SDK dependency), matching spendguard's
portable, self-contained surfaces (see serve.py). `handle(request)` is a pure function (request dict → response
dict|None) so the protocol is unit-tested without a live pipe. Read-only tools do NOT spend, so this server does
not require the gate; a spending tool (bakeoff) will `require()` before it runs.
"""

from .capability_cli_parsing import add_registered_cli_argument
import json
import logging
import sys
import traceback
from contextlib import contextmanager
from http import HTTPStatus

PROTOCOL_VERSION = "2024-11-05"     # echoed back to the client if it names one; this is the safe default
LEDGER_READ_OPERATIONS = ("events", "event", "calls", "count", "aggregate",
                          "recorded-call-total", "conversation-spend-since", "metrics")
MCP_PEER_DISCONNECT_PEEK_BYTES = 1
MCP_PEER_OBSERVER_THREAD_NAME = "spendguard-mcp-ledger-abandonment"
JSON_RPC_ID_MEMBER = "id"
MCP_READ_ONLY_ATTESTATION_META_KEY = "spendguard/readOnlyAttestation"
MCP_READ_ONLY_ATTESTATION_HEADER = "X-Spendguard-Read-Only-Attestation"
MCP_READ_ONLY_ATTESTATION_HEADER_VALUE = "supported"
HTTP_CONTENT_TYPE_HEADER = "Content-Type"
HTTP_CONTENT_LENGTH_HEADER = "Content-Length"
JSON_CONTENT_TYPE = "application/json"


@contextmanager
def _mcp_http_request_ledger_lifetime(connection):
    """Interrupt request-owned ledger reads when the HTTP caller disconnects."""
    import select
    import socket
    import threading
    from . import config
    from .ledger_read import ledger_request_scope

    observer_stopped = threading.Event()
    with ledger_request_scope() as request_reads:
        def observe_mcp_peer_abandonment():
            while not observer_stopped.is_set():
                try:
                    readable, _, _ = select.select(
                        [connection], [], [], config.LEDGER_REQUEST_DISCONNECT_POLL_S)
                    if readable:
                        if not connection.recv(MCP_PEER_DISCONNECT_PEEK_BYTES, socket.MSG_PEEK):
                            request_reads.cancel_ledger_request()
                            return
                        observer_stopped.wait(config.LEDGER_REQUEST_DISCONNECT_POLL_S)
                except (OSError, ValueError, TypeError):
                    request_reads.cancel_ledger_request()
                    return

        observer = threading.Thread(target=observe_mcp_peer_abandonment,
                                    name=MCP_PEER_OBSERVER_THREAD_NAME, daemon=True)
        observer.start()
        try:
            yield
        finally:
            observer_stopped.set()
            observer.join()


def _tool_ledger_read(args):
    """Read the local ledger through the bounded library; never open or migrate it."""
    from . import ledger_read, receipt
    from .ledger import USD_COLS
    operation = args.get("operation", LEDGER_READ_OPERATIONS[0])
    path = args.get("path")
    if operation not in LEDGER_READ_OPERATIONS:
        raise ValueError(f"expected operation in {LEDGER_READ_OPERATIONS}")
    if operation == "recorded-call-total":
        return {"recorded_call_total": ledger_read.read_recorded_call_total(path=path)}
    if operation == "conversation-spend-since":
        if path is not None:
            raise ValueError("conversation-spend-since uses configured ledger; set SPENDGUARD_HOME instead of path")
        if not args.get("conv") or not args.get("since"):
            raise ValueError("conversation-spend-since requires conv and since")
        deadline_arguments = {} if args.get("deadline_s") is None else {"deadline_s": args["deadline_s"]}
        return {"conversation_spend_since": ledger_read.read_conversation_spend_since(
            args["since"], conv=args["conv"], **deadline_arguments)}
    if operation == "metrics":
        return ledger_read.ledger_read_metrics()
    if operation == "count":
        return {"count": ledger_read.read_filtered_ledger_count(
            table=args.get("table", ledger_read.LEDGER_EVENTS_TABLE),
            since=args.get("since"), until=args.get("until"), path=path)}
    if operation == "event":
        event = ledger_read.read_ledger_event(args["event_id"], path=path)
        rows = () if event is None else (event,)
    elif operation == "calls":
        rows = ledger_read.read_recent_calls(since=args.get("since"), until=args.get("until"), path=path)
    elif operation == "aggregate":
        rows = ledger_read.read_spend_aggregates(group_by=tuple(args.get("group_by") or ("provider", "day")),
            since=args.get("since"), until=args.get("until"), path=path)
    else:
        rows = ledger_read.read_spend_events(since=args.get("since"), until=args.get("until"), path=path)
    money_columns = frozenset(USD_COLS) | {"cost"}
    rendered = [{key: receipt._money(float(value)) if key in money_columns and value is not None else value
                 for key, value in row.items()} for row in rows]
    return {"complete": True, "returned": len(rendered), "rows": rendered,
            "note": "Actual money and estimated subscription value are separate fields; never summed."}


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
    result = {"scope": r["scope"], "ranked_by": r["metric"], "pick": r["pick"],
              "models": r["models"], "unmeasured_for_intent": r["unmeasured_for_intent"], "caveats": caveats}
    if args.get("intent"):
        result["routing"] = r["routing"]
    return result


def _catalogue():
    """Enumerate authored catalog models, resolving rates through the sole price API."""
    from . import model_catalog, pricing
    out = []
    for model, record in model_catalog.all_records().items():
        provider = record.get("provider")
        try:
            rates = pricing.price(model, provider=provider)
        except pricing.PriceUnknownError:
            pricing.note_unpriced(model)
            continue
        out.append(dict(id=f"{provider}:{model}", model=model, provider=provider,
                        in_usd_per_m=rates.get("in_"), out_usd_per_m=rates.get("out"), source="curated"))
    return sorted(out, key=lambda row: (row["provider"] or "~", row["model"]))


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
    from . import config
    with config.scoped_intent_cap((f"{advisor.META}:recommend", f"{advisor.META}:cold-prior"), budget):
        return advisor.recommend_models(intent=intent, k=k, quality_bar=qbar, run=True)


def _tool_bakeoff(args):
    """Measure cost×quality for a candidate slate on a sample of an intent's tasks, so untried models earn a
    $/good. A bakeoff makes REAL, metered workload calls — so it fails closed if not gated, and (crucially) it
    only RUNS when the caller passes an explicit `budget_usd`; without one it returns the ESTIMATE only, never
    spending on its own."""
    import spendguard
    spendguard.require()                    # real spend → fail closed if the gate is not enforcing here
    from . import bakeoff as _bk
    # `efforts` sweeps the reasoning ladder PER model (finds the cheapest (model, effort) that holds quality);
    # a list or a comma string, both accepted. Omitted → one arm per model at its default effort.
    _eff = args.get("efforts")
    if isinstance(_eff, str):
        _eff = [e for e in _eff.split(",") if e.strip()]
    kw = dict(intent=args.get("intent"), candidates=args.get("candidates"),
              prompts=args.get("prompts"), sample_n=int(args.get("sample_n") or 5), efforts=(_eff or None),
              requirement_aware=bool(args.get("requirement_aware")), adjudicator_model=args.get("adjudicator_model"),
              temperature=args.get("temperature"), top_p=args.get("top_p"), seed=args.get("seed"))
    budget = args.get("budget_usd")
    if budget is None:                       # no budget → NEVER auto-spends; a bakeoff bills real workload $
        est = _bk.bakeoff(run=False, **kw)
        if not est.get("error"):
            est["note"] = ("estimate only — pass budget_usd to actually RUN the bakeoff (it makes real, metered "
                           "calls, preferring $0 lanes where available).")
        return est
    return _bk.bakeoff(run=True, budget_usd=float(budget), **kw)


def _tool_stability(args):
    """Measure a model/lane's RUN-TO-RUN variance at PRODUCTION settings — the stability question temperature=0 cannot
    answer. Runs `k` replicates of each prompt on its normal path (the $0 lane at the plan's own sampling, NOT forced
    metered), parses each reply's outcome (by `marker`, e.g. 'DECISION:', when given — a fixed shape, not a judgement),
    and reports stable_frac + the per-label between-run rate band. Replicates spend subscription-plan usage ($0 billed
    on a $0 lane); fails closed if the gate is not enforcing."""
    import spendguard
    spendguard.require()
    from . import stability as _st, bakeoff as _bk
    prompts = args.get("prompts")
    if not prompts and args.get("intent"):
        prompts = _bk._sample_prompts(args["intent"], int(args.get("sample_n") or 5))
    if not prompts:
        return {"error": "no prompts — pass `prompts` or an `intent` that has recorded prompts"}
    marker = args.get("marker")
    _parse = (lambda t: t.split(marker, 1)[1].strip() if marker in t else t.strip()) if marker else None
    return _st.measure_stability(prompts, args.get("model"), k=int(args.get("k") or _st.DEFAULT_REPLICATES),
                                 intent=args.get("intent"), system=args.get("system"), parse=_parse)


_VISION_OUT_EST = 1024                        # conservative default OUTPUT tokens for a label envelope (override: max_out_tokens)
_VISION_TIMEOUT_S = 120                        # default per-call deadline so a hung request cannot wedge the tool (override: timeout_s)
_VISION_MAX_TOTAL_IMAGE_BYTES = 48 * 1024 * 1024   # bound TOTAL image bytes per call so a giant batch can't OOM the server


def _image_bytes(images):
    """Total raw bytes of the images (a data: URL by string length, a path by file size), or (None, reason) NAMING
    the first unreadable one. Sized WITHOUT loading pixels, so an oversized batch is refused before it can OOM."""
    import os
    total = 0
    for idx, im in enumerate(images):
        try:
            total += len(im) if (isinstance(im, str) and im.startswith("data:")) else os.path.getsize(im)
        except Exception as e:
            return None, f"image[{idx}] unreadable ({type(e).__name__}: {str(e)[:60]})"
    return total, None


def _vision_cache_paths(model, prompt, images, schema, system):
    """The DURABLE cache location for one vision request, keyed by CONTENT (sha256 of the exact request). A paid
    result is written here before it returns, so a crash after spend does not lose it and an identical re-request
    is served from disk for $0 instead of re-paying — the same idempotence the bulk fan gets from its checkpoint."""
    import hashlib
    import json as _json
    import os
    home = os.environ.get("SPENDGUARD_HOME") or os.path.expanduser("~/.spendguard")
    key = hashlib.sha256(("\x00".join([str(model), str(prompt), _json.dumps(images, sort_keys=True, default=str),
                                        _json.dumps(schema, sort_keys=True, default=str), str(system)]))
                         .encode("utf-8", "replace")).hexdigest()[:32]
    d = os.path.join(home, "vision_cache")
    return d, os.path.join(d, key + ".json")


def _tool_vision(args):
    """Run ONE metered VISION call (image(s) + prompt) through the governed adapter. The subscription lanes are
    text-only CLIs, so a vision request rides the metered API (executor='api'), pixel-priced, and is bounded by a
    per-call deadline so a hung request cannot wedge the tool. Estimate-first: with NO budget_usd it returns ONLY
    the estimate (0 spend); pass budget_usd to actually run (refused if the estimate exceeds it). DURABLE +
    idempotent: a paid result is cached by request content (atomic fsync), so a crash after spend does not lose it
    and an identical re-request is served for $0 (pass no_cache=true to force fresh). A dynamic-key MAP schema is
    refused BEFORE any spend (strict → {}); total image bytes are capped before load."""
    import spendguard
    spendguard.require()                     # real spend → fail closed if the gate is not enforcing here
    import json as _json
    import os
    from . import adapters, pricing
    model, prompt = args.get("model"), args.get("prompt")
    images = args.get("images") or []
    schema = args.get("schema") if isinstance(args.get("schema"), dict) else None
    system = args.get("system")
    if not model or not prompt:
        return {"error": "model and prompt are required"}
    if not images:
        return {"error": "images must be a non-empty list of file paths or data: URLs — a vision call needs an image"}
    # SCHEMA guard BEFORE any spend: a dynamic-key map collapses to {} under OpenAI strict mode
    if schema is not None:
        _v = adapters.strict_map_violation(schema)
        if _v and adapters.provider_for(model) == "openai":
            return {"error": f"schema at {_v} is a dynamic-key map; OpenAI strict mode would return {{}} — use an "
                             f"ARRAY of {{key,value}} objects (e.g. results:[{{id,label}}])"}
    # DURABLE cache: an identical prior request is served for $0 (idempotent), and this is where a paid result lands
    cache_dir, cache_path = _vision_cache_paths(model, prompt, images, schema, system)
    if not args.get("no_cache") and os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                return {**_json.load(f), "cached": True}
        except Exception:
            pass                             # a corrupt cache line must not block a real call — fall through and re-run
    # SIZE guard BEFORE load (bytes only, no pixels) so an oversized batch is a loud refusal, never an OOM
    total_bytes, bad = _image_bytes(images)
    if bad:
        return {"error": bad}
    if total_bytes > _VISION_MAX_TOTAL_IMAGE_BYTES:
        return {"error": f"images total {total_bytes // 1024}KB exceeds the {_VISION_MAX_TOTAL_IMAGE_BYTES // 1024}KB "
                         f"per-call cap — send fewer/smaller images or downscale"}
    # 0-spend ESTIMATE (pixel-priced input + a conservative output)
    try:
        prov = adapters.provider_for(model)
        raw = model.split(":", 1)[1] if ":" in model else model
        loaded = [adapters._load_image(i) for i in images]
    except Exception as e:
        return {"error": f"could not load an image ({type(e).__name__}: {str(e)[:80]})"}
    in_tok = adapters._image_input_tokens(loaded, prov, raw) + (len(prompt) + len(system or "")) // 4
    out_est = int(args.get("max_out_tokens") or _VISION_OUT_EST)
    _seen = pricing.UNPRICED_SEEN.get(model, 0)
    est = pricing.cost_or_unpriced(model, in_tok, out_est, batch=False, provider=prov)   # records model in UNPRICED_SEEN if no card
    budget = args.get("budget_usd")
    if budget is None:                       # no budget → NEVER auto-spends; a vision call bills the metered API
        return {"estimate_usd": round(est, 6), "in_tok_est": in_tok, "out_tok_est": out_est, "n_images": len(images),
                "note": "estimate only — pass budget_usd to actually RUN this metered vision call (executor='api')."}
    if pricing.UNPRICED_SEEN.get(model, 0) > _seen:   # cost_or_unpriced RECORDED it unpriced → its 0.0 is "no price", not free;
        # a $0 estimate would sail past ANY budget and then spend unbounded (unpriced != free). Consistent with the estimate.
        return {"error": f"refused: {model} is UNPRICED — cannot bound its cost against budget_usd (an unpriced model "
                         f"is not a $0 model)", "estimate_usd": round(est, 6), "unpriced": True}
    if est > float(budget):
        return {"error": f"refused: estimate ${est:.4f} exceeds budget_usd ${float(budget):.4f}", "estimate_usd": round(est, 6)}
    r = adapters.vision(model, prompt, images, schema=schema, system=system, sig="mcp-vision",
                        timeout_s=int(args.get("timeout_s") or _VISION_TIMEOUT_S))   # bounded: a hung call cannot wedge
    out = {"text": r.get("text"), "cost": r.get("cost"), "executor": r.get("executor"), "error": r.get("error"),
           "in_tok": r.get("in_tok"), "out_tok": r.get("out_tok"), "estimate_usd": round(est, 6)}
    if not r.get("error") and r.get("text") is not None:   # persist the PAID result so a crash cannot lose it
        try:
            os.makedirs(cache_dir, exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, "w") as f:
                # raw-write-ok: disposable content-keyed cache entry, write-once and atomic (os.replace below).
                # A cache file has NO prior version to preserve, so config.update_json's backup discipline is moot;
                # a stale/corrupt entry is self-healing (the reader falls through and re-runs).
                _json.dump(out, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, cache_path)       # atomic: a reader never sees a half-written cache entry
        except Exception:
            pass                              # a cache-write failure must not lose the in-hand paid result
    return out


# ── Claude Code spend + compaction tools (read-only, $0). spendguard's own functions PRINT human text, which would
#    corrupt JSON-RPC on stdout — so any handler that calls one wraps it in redirect_stdout(). ─────────────────────
SPEND_OVERVIEW_TOP_INTENT_LIMIT = 5


def _cc_titles():
    try:
        from . import claudecode
        return claudecode._sidebar_titles()
    except Exception:
        return {}


def _cc_label(conv, titles):
    return titles.get(conv) or (conv or "?")[:40]


def _tool_spend_overview(_args):
    from pathlib import Path
    from . import config, ledger_read
    path = config.db_path()
    if (not Path(path).exists()
            or not ledger_read.ledger_read_table_exists(ledger_read.LEDGER_EVENTS_TABLE, path)):
        est = over = sub = api = 0
    else:
        overview = ledger_read.read_cc_spend_overview(path)
        est, over, sub, api = (overview[key] for key in ("est", "over", "sub", "api"))
    # THIRD AXIS: guarded savings (avoided $) + the per-decision value proof. Kept separate — NEVER summed into
    # real-$ or est-value. `counterfactual` (best-value/advisor/compaction) is held apart from `certain` (measured).
    # No except-and-zero here: both readers create their tables if absent, so a fresh install returns real zeros;
    # a genuine failure (a corrupt savings/decisions db) must SURFACE, never be masked as "$0 saved, 0 decisions".
    from . import guard as _guard
    _sv = _guard.saved_since()
    _dec = _guard.decisions_summary(intent_limit=SPEND_OVERVIEW_TOP_INTENT_LIMIT)
    return {"note": "THREE separate axes, NEVER summed: real $ (money out the door), est-value (plan-covered usage "
                    "worth), and guarded savings (avoided $, with a per-decision proof).",
            "real_usd": {"subscription_base": round(sub, 2), "claude_code_overage": round(over, 2),
                         "api_credits": round(api, 2), "total_real": round(sub + over + api, 2)},
            "est_value_usd": {"claude_code_plan_covered": round(est, 2)},
            "saved_usd": {"total": _sv["total"], "certain": _sv["certain"], "counterfactual": _sv["counterfactual"],
                          "by_source": _sv["by_source"], "decisions": _dec["decisions"],
                          "top_intents_by_saving": _dec["by_intent"]}}


def _tool_overage_status(_args):
    from . import overage
    return overage.current_overage_status()


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
    from . import ledger_read
    rows = ledger_read.read_cc_top_conversations(limit=limit)
    return {"ranked_by": "est-value $ (what the usage is worth at API rates)",
            "conversations": [{"title": _cc_label(c, titles), "est_value_usd": v, "turns": n, "max_context_tokens": mx}
                              for c, v, n, mx in (row.values for row in rows)]}


def _tool_conversation_cost(args):
    conv = str(args.get("conversation_id") or "").strip()
    if not conv:
        return {"error": "pass conversation_id (a transcript uuid — from spendguard_top_conversations)"}
    from . import ledger_read
    costs = ledger_read.read_cc_conversation_cost(conv)
    return {"conversation_id": conv, "title": _cc_label(conv, _cc_titles()),
            "est_value_usd_plan_covered": round(costs["est"], 2), "turns": costs["n"],
            "observed_overage_usd_upper_bound": round(costs["over"], 2),
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


def _tool_dispatch_state(args):
    from . import dispatch, lane_queue
    if args.get("id") is not None:
        return lane_queue.queue_row_status(args["id"])
    st = dispatch.admission_state()
    return {"note": ("live ADMISSION + QUEUE + reasoning-cut state (the SAME dispatch.admission_state() the CLI "
                     "`spendguard dispatch` renders — api/MCP/cli parity): the governor's per-key concurrency/rpm/tpm, "
                     "the SELF-CALIBRATED per-vendor limits (learned from 429 + success headers), the durable queue "
                     "depth including PARKED (capacity backpressure), and the deadline-cancel counts (a reasoning call "
                     "cut mid-thought — billed by the provider, invisible to the local ledger). $0, read-only."),
            **st}


def _tool_config(_args):
    import os
    from . import config, config_schema
    knobs = []
    for s in config_schema.SETTINGS:
        if s.get("secret"):
            continue                                     # NEVER expose secrets (API keys etc.) over MCP
        section, key, store, env = s["section"], s["key"], s.get("store", ""), s.get("env")
        try:
            if env and os.getenv(env) is not None:
                cur = os.getenv(env)                     # env override (highest precedence), matching the runtime
            elif isinstance(store, str) and store.startswith("config.json:"):
                cur = config._cfg_get(section, key, s.get("default"))
            else:
                cur = s.get("default")
        except Exception:
            cur = s.get("default")
        knobs.append({"key": "%s.%s" % (section, key), "value": cur, "default": s.get("default"),
                      "env": env, "kind": s.get("kind"), "desc": s.get("desc")})
    return {"note": ("every spendguard config knob (from config_schema — the single source that also drives the CLI "
                     "`config` command and setup), with its CURRENT value, default, env var, kind and description. "
                     "Secrets are omitted. Read-only ($0); change one with the CLI `spendguard config set "
                     "<section.key> <value>` (setting over MCP is deliberately not exposed)."),
            "knobs": knobs}


HEALTH_DEFAULT_RUN = True
HEALTH_DEFAULT_TIMEOUT_S = 20


# name → (description, JSON-Schema for arguments, handler)
def _tool_health(args):
    from . import reliability
    run = args.get("run", HEALTH_DEFAULT_RUN)
    timeout_s = int(args.get("timeout_s") or HEALTH_DEFAULT_TIMEOUT_S)
    # DEGRADE, NEVER CRASH. The reachability surface is the thing you CALL to learn what is healthy, so it must not be
    # the thing that breaks: a code/attr error here (e.g. the skewed-install pricing.OUTPUT_FLOOR AttributeError) returns
    # a LABELLED internal-error result, not a raw exception that blanks the whole tool. sweep() is isolated below; the
    # remediate/persist steps are best-effort and never take down a sweep that already succeeded.
    try:
        res = reliability.sweep(run=run, timeout_s=timeout_s)
    except Exception as e:
        logging.getLogger(__name__).exception("Health-check sweep failed")
        return {"error": f"health-check internal error: {e!r}",
                "diagnostic": {"exception": repr(e), "traceback": traceback.format_exc()},
                "note": "the reachability surface hit an internal error and DEGRADED (it did not crash); fix the error "
                        "or update a skewed install, then re-run.",
                "lanes": {}, "metered": {}, "actions": [],
                "summary": {"lanes_up": 0, "lanes_total": 0, "metered_up": 0, "metered_total": 0, "down": []}}
    lanes_up = sum(1 for d in res["lanes"].values() if d.get("reachable"))
    met_up = sum(1 for d in res["metered"].values() if d.get("reachable"))
    down = ([f"lane:{k}" for k, d in res["lanes"].items() if not d.get("reachable")]
            + [f"metered:{k}" for k, d in res["metered"].items() if not d.get("reachable")])
    actions = None
    health_diagnostic = None
    try:
        if run and down and args.get("remediate", True):
            actions = reliability.remediate(res)          # agentic FIX per down resource (cached — $0 for a known failure)
        if run:
            reliability._persist_health(res, actions)     # a health check from ANY surface feeds the receipt alert + notifier
    except Exception as e:
        logging.getLogger(__name__).exception("Health-check remediation or persistence failed")
        health_diagnostic = {"exception": repr(e), "traceback": traceback.format_exc()}
        actions = actions or None                         # best-effort: a remediate/persist hiccup never blanks a good sweep
    # Same read-only cached ledger verdict as doctor: never turn health into a live billing pull.
    try:
        from . import ledger_sync
        cached_ledger_health = ledger_sync.cached_leak_line()
        ledger_health = ({"status": "unknown", "line": "leak status UNKNOWN — run `spendguard reconcile`",
                          "age_s": None} if cached_ledger_health is None else
                         {"status": "cached", "line": cached_ledger_health[0] or "nothing to compare",
                          "age_s": cached_ledger_health[1]})
    except Exception as exc:
        ledger_health = {"status": "unknown", "line": "leak status UNKNOWN — check could not run",
                         "age_s": None, "error": repr(exc)}
    return {**({"diagnostic": health_diagnostic} if health_diagnostic is not None else {}),
            "note": ("live reachability + the FIX for each down resource (agentic, cached) — lanes $0, the metered "
                     "pings cost ~a few $0.0001; each probe is bounded by timeout_s so a dead endpoint fails fast. "
                     "The result is also cached so a down lane surfaces in the receipt" if run
                     else "estimate only (run=false) — $0, no probe"),
            "lanes": res["lanes"], "metered": res["metered"], "ledger": ledger_health,
            "actions": [{"resource": a["resource"], "kind": a["kind"], "issue": a.get("issue"),
                         "fix": a.get("fix"), "command": a.get("command"),
                         **({"diagnostic": a["diagnostic"]} if "diagnostic" in a else {})} for a in (actions or [])],
            "summary": {"lanes_up": lanes_up, "lanes_total": len(res["lanes"]),
                        "metered_up": met_up, "metered_total": len(res["metered"]), "down": down}}


def _tool_savings(args):
    from . import guard
    s = guard.saved_since()
    ds = guard.decisions_summary()
    shed = guard.shed_summary()
    return {"note": ("what spendguard SAVED — a THIRD axis, kept SEPARATE from real-$ spend and est-value and NEVER "
                     "summed into either. 'certain' is measured (cache / cascade / gate-block avoided-spend); "
                     "'counterfactual' is the est saving from best-value / advisor model swaps, each priced vs what "
                     "the caller WOULD have run. 'shed' is substitutable work routed OFF a capped/overage/shed lane: "
                     "avoided_overage_usd is REAL money avoided (a plan on paid overage would have billed), "
                     "quota_preserved_usd is est-value moved to another free lane (NOT real money) — a separate axis, "
                     "never summed with avoided_overage. $0, read-only."),
            "certain_usd": s.get("certain"), "counterfactual_usd": s.get("counterfactual"),
            "total_guarded_usd": s.get("total"), "by_source": s.get("by_source"),
            "decisions": ds.get("decisions"), "saved_by_intent": ds.get("by_intent"),
            "shed": shed}


def _tool_version(args):
    """Which spendguard COMMIT this server process is running, the green pointer it should be on, and whether it
    is STALE — the $0 signal behind 'always serve the latest with the best'. A git-SHA compare + a small file
    read; a stale server hands off to a fresh process on its next request (auto-respawn)."""
    import os
    from importlib import metadata
    from pathlib import Path
    import spendguard
    from . import config, gate, release
    return {**release.release_status(), "pid": os.getpid(),
            "imported_version": spendguard.__version__,
            "metadata_version": metadata.version("llm-spendguard"),
            "gate_enforcing": bool(not config.disabled() and gate._enforcement_armed()),
            "ledger_path": str(Path(config.db_path()).resolve())}


def _tool_route_cost(args):
    """TRUE-cost batch-vs-lane-vs-combo routing split for an intent at volume N — wraps route_economics.report (the
    same computation the `route-cost` CLI prints), so the MCP answer and the CLI can never disagree. $0."""
    from . import route_economics
    return route_economics.route_report(args["intent"], int(args["n"]), in_tok=args.get("in_tok"), out_tok=args.get("out_tok"),
                                        lane=args.get("lane"), batch_model=args.get("batch_model"))


def _tool_route_realized(args):
    """REALIZED routing history from the calls ledger; read-only and $0, not a projection."""
    from . import route_realized
    return route_realized.route_realized_report(args["intent"], since=args.get("since"),
                                                  as_of=args.get("as_of"), limit=args.get("limit", 20))


def _tool_route_horizon(args):
    """HORIZON plan (item C) for a whole job SET — allocate groups across lane / batch / metered-realtime over the
    plan's reset-window horizon, honouring each group's URGENCY and the SHARED lane budget. Wraps
    route_horizon.horizon_report (the same computation the `route-horizon` CLI prints), so MCP and CLI never disagree. $0."""
    from . import route_horizon
    return route_horizon.horizon_report(args.get("groups") or [], lane=args.get("lane"),
                                        batch_model=args.get("batch_model"), realtime_model=args.get("realtime_model"))


def _tool_plan_queue(args):
    """The $0 PREDICTIVE planner + async BATCH-JOB tracker state — the MCP twin of `spendguard plan-queue`, wrapping
    the same queue_planner.tick() (forecast of which vendor is about to 429, which intents to OFFLOAD to the Batch API
    and how to chunk them, which to PACE) plus batch_tracker.status() (open/settled/failed async batch jobs). DECIDES
    only; the drain executes — so this is safe to call any time. $0, read-only arithmetic on live governor + queue state."""
    from . import queue_planner, batch_tracker
    t = queue_planner.tick()
    try:
        t["batch_jobs"] = batch_tracker.status()
    except Exception:
        t["batch_jobs"] = {}
    return t


def _tool_run_jobs(args):
    """The whole-job contract (whole_job): hand spendguard a SET of jobs + a GOAL and it plans (batch vs lane vs
    metered, capability-matched per schema), budget-gates, executes, and returns. SPEND-SAFE for an agent surface:
    with NO goal.budget_usd it returns the PLAN + estimate ONLY ($0, executed=False) — never an unbounded fan; pass
    goal.budget_usd to EXECUTE (the budget is the estimate-first cap: an over-estimate or an unpriceable group is
    refused before any spend)."""
    from . import whole_job
    jobs = args.get("jobs") or []
    goal = args.get("goal") if isinstance(args.get("goal"), dict) else {}
    if goal.get("budget_usd") is None:
        p = whole_job.plan_jobs(jobs, goal)
        return {"executed": False, "plan": p["plan"], "est_usd": p["est_usd"],
                "priced_est_usd": p["priced_est_usd"], "unpriced": p["unpriced"], "bad_jobs": p["bad_jobs"],
                "note": "estimate only ($0) — pass goal.budget_usd to EXECUTE (the budget is the cap)."}
    return {"executed": True, **whole_job.run_jobs(jobs, goal)}


def _tool_delegate(args):
    """Offload a SELF-CONTAINED subtask through the same two-hop engine as the delegate CLI."""
    from . import delegate_router
    task = args.get("task")
    files = args.get("files")
    provider = args.get("provider", "auto")
    execute = bool(args.get("execute", False))
    if not execute:
        return delegate_router.delegate_task(
            task, files=files, intent=args.get("intent"), provider=provider, execute=False, cwd=args.get("cwd"))
    classification = delegate_router.classify_task(task, files=files, provider=provider)
    if classification.get("kind") != "agentic":
        return delegate_router.delegate_task(
            task, files=files, intent=args.get("intent"), provider=provider, execute=True, cwd=args.get("cwd"))
    import os
    cwd = args.get("cwd")
    if cwd is None or not os.path.isabs(os.path.expanduser(os.fspath(cwd))):
        return delegate_router.delegate_task(
            task, files=files, intent=args.get("intent"), provider=provider, execute=True, cwd=cwd)
    from . import delegate_jobs
    fingerprint = delegate_router.delegate_route_fingerprint(task, files=files, provider=provider)
    job_id = delegate_jobs.find_inflight_delegate_job(fingerprint, cwd)
    if job_id is None:
        started = delegate_router.start_delegate_job(
            task, files=files, intent=args.get("intent"), provider=provider, cwd=cwd)
        if started.get("state") != "running":
            return started
        job_id = started["job_id"]
    return delegate_router.wait_for_delegate_job_handoff(job_id)


def _tool_delegate_status(args):
    """Read durable status for an agentic delegate job."""
    from . import delegate_jobs
    return delegate_jobs.read_delegate_job(args.get("job_id"))


def _tool_delegate_result(args):
    """Collect the complete durable result for an agentic delegate job."""
    from . import delegate_jobs
    return delegate_jobs.read_delegate_job_result(args.get("job_id"))


_TOOLS = {
    "spendguard_ledger_read": (
        "Read-only bounded typed ledger reads. Oversize, timeout and missing schema are loud errors; "
        "count returns the true total without materializing rows. No model calls.",
        {"type": "object", "properties": {
            "operation": {"type": "string", "enum": list(LEDGER_READ_OPERATIONS)},
            "table": {"type": "string", "enum": ["calls", "spend_events"]},
            "group_by": {"type": "array", "items": {"type": "string"}},
            "event_id": {"type": "string"}, "since": {"type": "string"},
            "conv": {"type": "string"}, "deadline_s": {"type": "number"},
            "until": {"type": "string"}, "path": {"type": "string"}},
         "additionalProperties": False},
        _tool_ledger_read),
    "spendguard_delegate": (
        "Offloads a SELF-CONTAINED AGENTIC workspace-write subtask to another provider plan; it does NOT reroute the "
        "calling agent's own turns. Dry-run is default; execute=true needs a trusted cwd. For a one-shot "
        "classification/gate/doctrine verdict use the one-shot router or spendguard ask with an intent. Codex is ~5s "
        "warm; >75s is cold codex exec only. Get model ids from advisor.lane_models/lanes --catalog, not guesses; "
        "see README.md's routing cheat-sheet. Agentic execute returns inline only if it finishes within the handoff "
        "window; otherwise it returns a job_id. Collect with spendguard_delegate_result; the detached run and its "
        "durable result survive a dropped request.",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": "the complete self-contained task"},
            "files": {"type": "array", "items": {"type": "string"},
                      "description": "optional supporting file paths; each is read in full"},
            "intent": {"type": "string", "description": "optional attribution/cost-control intent"},
            "provider": {"type": "string", "description": "configured plan or auto (default auto)"},
            "cwd": {"type": "string", "description": "absolute trusted target repo path; required for execute=true agentic tasks"},
            "execute": {"type": "boolean", "description": "run hop 2; default false (dry-run)"}},
         "required": ["task"], "additionalProperties": False},
        _tool_delegate),
    "spendguard_delegate_status": (
        "Read the durable state and metadata of a detached agentic delegate job by job_id. $0, read-only.",
        {"type": "object", "properties": {"job_id": {"type": "string"}},
         "required": ["job_id"], "additionalProperties": False},
        _tool_delegate_status),
    "spendguard_delegate_result": (
        "Collect the complete persisted result of a detached agentic delegate job by job_id. $0, read-only.",
        {"type": "object", "properties": {"job_id": {"type": "string"}},
         "required": ["job_id"], "additionalProperties": False},
        _tool_delegate_result),
    "spendguard_version": (
        "Which spendguard COMMIT this MCP server is running, the green pointer it should be on, and whether it is "
        "STALE (a newer gate-green commit was deployed). $0 — a git-SHA compare. If stale, the server hands off to "
        "a fresh process on its next request, so the tools self-heal to the latest code (see `spendguard deploy`).",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_version),
    "spendguard_health": (
        "Live reachability of every $0 subscription LANE + every metered PROVIDER, PLUS the exact FIX for anything "
        "down (which login/quota/API to fix) — a fast, BOUNDED health check. Each probe is time-bounded (timeout_s) "
        "so ONE hung endpoint cannot wedge the sweep. Lanes are $0; the metered pings cost ~a few $0.0001; the "
        "remediation is agentic + CACHED (only a NEW failure costs). Returns per-resource {reachable, executor, "
        "cost, latency, reason}, an `actions` list (issue/fix/command per down resource), and a summary. The result "
        "is cached so a down lane also surfaces in the receipt/notifier. Includes doctor's cached ledger leak "
        "status and age (unknown if absent); never pulls live billing. run=false → estimate only ($0).",
        {"type": "object", "properties": {
            "run": {"type": "boolean", "default": HEALTH_DEFAULT_RUN,
                    "description": "actually probe; false = $0 estimate only"},
            "remediate": {"type": "boolean", "description": "include the agentic FIX for each down resource (default true; cached)"},
            "timeout_s": {"type": "integer", "default": HEALTH_DEFAULT_TIMEOUT_S,
                          "description": "per-probe bound in seconds — a dead endpoint fails this fast"}},
         "additionalProperties": False},
        _tool_health),
    "spendguard_savings": (
        "What spendguard SAVED — the THIRD axis, kept SEPARATE from real-$ spend and est-value (never summed into "
        "either). Returns certain (measured: cache / cascade / gate-block avoided-spend) + counterfactual (est: "
        "best-value / advisor model swaps priced vs the counterfactual), broken out by source, plus the count of "
        "decisions booked and the saving per intent. This is the value-proof for the routing spendguard did. $0.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_savings),
    "spendguard_route_cost": (
        "TRUE-cost routing for a BULK job: for an intent at volume N, the $ of running it on a subscription LANE "
        "(priced at its true MARGINAL cost, never $0), the metered BATCH API, or a min-cost COMBO — plus the "
        "recommendation and a plain 'why'. Prices a $0 lane at its real amortized rate + reclaims capacity that would "
        "expire unused; the batch leg is ~half realtime, cap-free. $0, arithmetic on measured economics "
        "(lane_economics + pricing); degrades honestly when a lane cap is still estimating.",
        {"type": "object", "properties": {
            "intent": {"type": "string", "description": "the job-type label the calls are tagged under"},
            "n": {"type": "integer", "description": "number of calls (tasks) in the job"},
            "in_tok": {"type": "integer", "description": "input tokens per call (default: = out_tok)"},
            "out_tok": {"type": "integer", "description": "output tokens per call (default: measured p90)"},
            "lane": {"type": "string", "description": "force a lane (default: the first converged one)"},
            "batch_model": {"type": "string", "description": "metered model for the batch leg (default: config advisor.batch_model)"}},
         "required": ["intent", "n"], "additionalProperties": False},
        _tool_route_cost),
    "spendguard_route_realized": (
        "REALIZED routing history from the calls ledger for an intent: $0 subscription lanes vs metered API, "
        "billed cost, served-model drift, falloff reasons, and recent samples. $0 and read-only; unlike route_cost "
        "and route_horizon, this reports actual stored history rather than projecting a hypothetical job.",
        {"type": "object", "properties": {
            "intent": {"type": "string", "description": "the exact calls.intent label"},
            "since": {"type": "string", "description": "inclusive lower ts bound (ISO date/timestamp string)"},
            "as_of": {"type": "string", "description": "inclusive upper ts bound (ISO date/timestamp string)"},
            "limit": {"type": "integer", "description": "maximum recent sample rows (default 20)"}},
         "required": ["intent"], "additionalProperties": False},
        _tool_route_realized),
    "spendguard_route_horizon": (
        "HORIZON plan (item C) for a whole job SET: given groups [{intent, n, urgency?}], allocate each across the "
        "subscription LANE (~free/waste capacity that would expire at reset reclaimed FIRST), the metered BATCH API, "
        "and the metered REALTIME API over the plan's reset-window horizon — honouring caller-declared URGENCY (an "
        "urgent group's overflow → fast realtime; a deferrable group's → cheap cap-free batch; default deferrable) and "
        "the ONE SHARED, scarce lane budget (drawn URGENT-FIRST so groups never double-bank the free tokens). Returns a "
        "per-group allocation + total $ + plain 'why'. $0, arithmetic on measured economics; no forecast of unknown "
        "future work; degrades honestly (unpriced tokens said out loud, never invented at $0).",
        {"type": "object", "properties": {
            "groups": {"type": "array", "description": "the job groups to plan over the horizon",
                       "items": {"type": "object", "properties": {
                           "intent": {"type": "string", "description": "the job-type label the calls are tagged under"},
                           "n": {"type": "integer", "description": "number of calls in the group"},
                           "urgency": {"type": "string", "description": "urgent | deferrable (default deferrable)"},
                           "in_tok": {"type": "integer"}, "out_tok": {"type": "integer"}},
                           "required": ["intent", "n"]}},
            "lane": {"type": "string", "description": "force a lane (default: the first converged one)"},
            "batch_model": {"type": "string", "description": "metered model for the batch leg (default: config advisor.batch_model)"},
            "realtime_model": {"type": "string", "description": "metered model for urgent overflow (default: config advisor.model)"}},
         "required": ["groups"], "additionalProperties": False},
        _tool_route_horizon),
    "spendguard_plan_queue": (
        "The $0 PREDICTIVE queue planner + async batch-job state (the MCP twin of `spendguard plan-queue`): a forecast "
        "of which vendor is about to breach its tokens/minute ceiling (a 429 BEFORE it happens), which intents to "
        "OFFLOAD to the Batch API and how to chunk them, which to PACE — PLUS the tracked batch jobs (open/settled/"
        "failed). It DECIDES; the drain executes — so it never spends and never mutates the queue. $0, read-only.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_plan_queue),
    "spendguard_run_jobs": (
        "The WHOLE-JOB contract — hand spendguard a SET of jobs + a GOAL and it decides the plan (batch vs lane vs "
        "metered, capability-matched to each job's schema), enforces the budget, executes, and returns; you never "
        "hand-tune metered_only/batch/lanes/model per call. SPEND-SAFE: with NO goal.budget_usd it returns the PLAN + "
        "estimate ONLY ($0, executed=false) — never an unbounded fan; pass goal.budget_usd to EXECUTE (the budget is "
        "the estimate-first cap — an over-estimate, or a group whose cost is unknown, is refused BEFORE any spend). "
        "Returns {executed, results (ready, keyed by job id), pending (async batch handles — settle later), plan, "
        "receipt {est_usd, ran, batch_failures, refused_code}}. Batch groups are async (~half cost, ~24h).",
        {"type": "object", "properties": {
            "jobs": {"type": "array", "description": "the job set", "items": {
                "type": "object", "properties": {
                    "prompt": {"type": "string", "description": "the task prompt"},
                    "intent": {"type": "string", "description": "job-type label — the routing + attribution key (required)"},
                    "id": {"type": "string", "description": "stable id to key the result by (default task-<i>)"},
                    "system": {"type": "string", "description": "optional system prompt"},
                    "schema": {"type": "object", "description": "optional JSON schema — a strict one auto-routes to an enforcing path"}},
                "required": ["prompt", "intent"], "additionalProperties": False}},
            "goal": {"type": "object", "description": "budget_usd (estimate-first cap; required to EXECUTE), urgency "
                     "(auto|realtime|batch), quality_bar (best-value model pick when set)",
                     "properties": {"budget_usd": {"type": "number"}, "urgency": {"type": "string"},
                                    "quality_bar": {"type": "string"}},
                     "additionalProperties": False}},
         "required": ["jobs"], "additionalProperties": False},
        _tool_run_jobs),
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
        "with NO budget_usd it returns the ESTIMATE only; pass budget_usd to actually run (it refuses over it). "
        "requirement_aware=true judges against the PROMPT'S OWN extracted criteria, two-tier (screen → opus).",
        {"type": "object", "properties": {
            "intent": {"type": "string", "description": "the job-type to bake off for"},
            "candidates": {"type": "array", "items": {"type": "string"},
                           "description": "the slate to test, as 'vendor:model' ids (spendguard_recommend suggests one)"},
            "prompts": {"type": "array", "items": {"type": "string"},
                        "description": "optional: representative tasks to replay; omit to auto-sample the intent's recorded prompts"},
            "sample_n": {"type": "integer", "description": "how many recorded prompts to replay when auto-sampling (default 5)"},
            "efforts": {"type": "array", "items": {"type": "string"},
                        "description": "optional reasoning ladder to sweep PER model (['minimal','low','medium','high']) — the cheapest (model,effort) that holds"},
            "requirement_aware": {"type": "boolean",
                                  "description": "judge against the PROMPT'S OWN requirements, two-tier (screen → opus adjudicator)"},
            "adjudicator_model": {"type": "string",
                                  "description": "opus-tier adjudicator for requirement_aware (default config.advisor_adjudicator_model)"},
            "temperature": {"type": "number", "description": "DETERMINISM: run each arm as a measurement at this temperature (e.g. 0) — one deterministic pass per arm, FORCES the metered API"},
            "top_p": {"type": "number", "description": "DETERMINISM: nucleus top_p for the measurement (metered)"},
            "seed": {"type": "integer", "description": "DETERMINISM: sampling seed (OpenAI-only; dropped+recorded on vendors without a seed param)"},
            "budget_usd": {"type": "number", "description": "run only if the estimate fits this; OMIT to get just the estimate"}},
         "additionalProperties": False},
        _tool_bakeoff),
    "spendguard_stability": (
        "Measure a model/lane's RUN-TO-RUN variance at PRODUCTION settings (replicates) — the stability question "
        "temperature=0 CANNOT answer (temp=0 measures 'which arm is better'; this measures 'is the shipped config "
        "stable'). Runs k replicates of each prompt on its normal $0-lane path at the plan's own sampling, and reports "
        "stable_frac, the flipped prompts, and the per-label between-run rate band. Spends subscription-plan usage ($0 "
        "billed on a $0 lane). Pass `marker` (e.g. 'DECISION:') to score stability on the decision, not wording.",
        {"type": "object", "properties": {
            "model": {"type": "string", "description": "the model/lane to measure (e.g. gpt-6-luna)"},
            "intent": {"type": "string", "description": "job-type whose recorded prompts to replay (or pass `prompts`)"},
            "prompts": {"type": "array", "items": {"type": "string"}, "description": "explicit prompts to replay; overrides intent sampling"},
            "sample_n": {"type": "integer", "description": "how many recorded prompts to replay when sampling the intent (default 5)"},
            "k": {"type": "integer", "description": "replicates per prompt (default 5) — more tightens the variance estimate"},
            "marker": {"type": "string", "description": "parse each reply's OUTCOME as the text after this marker (e.g. 'DECISION:') — scores stability on the decision label"},
            "system": {"type": "string", "description": "optional system prompt sent with every replicate"}},
         "additionalProperties": False},
        _tool_stability),
    "spendguard_vision": (
        "Run ONE metered VISION call (image(s) + prompt) through spendguard's governed adapter. The subscription "
        "lanes are text-only CLIs, so vision rides the metered API (executor='api'), pixel-priced and bounded by a "
        "deadline. Estimate-first: with NO budget_usd it returns ONLY the estimate ($0); pass budget_usd to run "
        "(refused over it). A dynamic-key MAP schema is refused before any spend (strict mode empties it to {} — "
        "use an ARRAY of {key,value}); paid results are cached by request content, so an identical re-request is $0.",
        {"type": "object", "properties": {
            "model": {"type": "string", "description": "vision-capable model 'vendor:model' (e.g. 'openai:gpt-5-nano', 'gemini:gemini-3-flash')"},
            "prompt": {"type": "string", "description": "the instruction — what to label/extract from the image(s)"},
            "images": {"type": "array", "items": {"type": "string"},
                       "description": "non-empty list of file paths (server-side) or data: URLs"},
            "schema": {"type": "object", "description": "optional JSON Schema for structured output — use an ARRAY of {key,value}, NEVER a dynamic-key map"},
            "system": {"type": "string", "description": "optional system instruction"},
            "budget_usd": {"type": "number", "description": "run only if the estimate fits this; OMIT to get just the estimate (0 spend)"},
            "max_out_tokens": {"type": "integer", "description": "optional output-token estimate for the pre-spend cost estimate"},
            "no_cache": {"type": "boolean", "description": "force a fresh call instead of returning a cached identical result"},
            "timeout_s": {"type": "integer", "description": "optional per-call deadline in seconds (default 120)"}},
         "required": ["model", "prompt", "images"], "additionalProperties": False},
        _tool_vision),
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
    "spendguard_dispatch_state": (
        "Live ADMISSION + QUEUE + reasoning-cut state — 'is anything rate-limited / is the queue backing up / are we "
        "burning money on reasoning cut mid-thought'. Returns the governor's per-key concurrency/rpm/tpm + in-flight/"
        "waiting, the SELF-CALIBRATED per-vendor limits (learned from provider 429 + success headers), the durable "
        "queue depth incl. PARKED (capacity backpressure), and the deadline-cancel counts (billed by the provider, "
        "invisible to the local ledger). The same data the CLI `spendguard dispatch` prints (api/MCP/cli parity). $0.",
        {"type": "object", "properties": {
            "id": {"type": "integer", "description": "optional async queue row handle to query"}},
         "additionalProperties": False},
        _tool_dispatch_state),
    "spendguard_config": (
        "Every spendguard CONFIG knob (from config_schema — the single source that also drives the CLI `config` "
        "command), with its CURRENT value, default, env var, kind and description. Secrets omitted. Read-only ($0) — "
        "change one via the CLI `spendguard config set <section.key> <value>`. Closes the config-in-MCP parity gap.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        _tool_config),
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
        from . import release as _rel
        client_ver = (req.get("params") or {}).get("protocolVersion")
        _sv = _rel.served_sha() or {}
        _ver = getattr(spendguard, "__version__", "0") + (f"+{_sv['short']}" if _sv.get("short") else "")
        return _ok(rid, {"protocolVersion": client_ver or PROTOCOL_VERSION,
                         # listChanged: this server may send notifications/tools/list_changed (serve_stdio does,
                         # after its first substantive request) so a client re-fetches tools/list — the path by
                         # which a tool ADDED in an auto-respawn becomes visible without a manual reconnect.
                         "capabilities": {"tools": {"listChanged": True}},
                         "serverInfo": {"name": "spendguard", "version": _ver},
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
                             "• spendguard_savings() — the THIRD axis: what spendguard SAVED (certain measured + "
                             "counterfactual from best-value/advisor swaps), by source. Kept SEPARATE from real-$ and "
                             "est-value, never summed. $0.\n"
                             "• spendguard_route_realized(intent, since?, as_of?, limit?) — REALIZED calls-ledger "
                             "routing history; $0/read-only, distinct from route_cost/route_horizon projections.\n"
                             "• spendguard_health(run?, timeout_s?) — BOUNDED reachability of every $0 subscription "
                             "lane + every metered provider; one hung endpoint fails fast (timeout_s), never wedges. "
                             "Lanes $0, metered pings ~pennies.\n"
                             "• spendguard_delegate(task, files?, intent?, provider?, execute?) — offload a "
                             "SELF-CONTAINED subtask to another provider plan; dry-run by default. It does NOT "
                             "reroute this calling agent's own turns.\n"
                             "TO MAKE SPENDGUARD PICK FOR YOU on a real call (not just advise): the programmatic API "
                             "(adapters.call / vendor_call) takes reasoning='best-value' with an intent — it resolves "
                             "the cheapest (model, effort) whose measured quality holds for that intent, and books the "
                             "saving vs the counterfactual. Learn the per-intent effort with `spendguard effort-titrate "
                             "<intent>` (CLI, estimate-first).\n"
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
            from .ledger_read import ledger_request_scope
            with ledger_request_scope():
                payload = fn(params.get("arguments") or {})
            return _ok(rid, _tool_result(payload))
        except Exception as e:
            # a TOOL failure is reported as an isError result (the model can read it), not a transport error
            return _ok(rid, _tool_result({"error": f"{type(e).__name__}: {e}",
                                          "error_type": type(e).__name__}, is_error=True))
    if is_notification:
        return None                                    # unknown notification → ignore, never reply
    return _err(rid, -32601, f"method not found: {method}")


def _auto_respawn_enabled():
    """Should a STALE server exit between requests so the client respawns it on the latest gate-green code? Env
    SPENDGUARD_MCP_AUTO_RESPAWN wins, else config advisor.mcp_auto_respawn. Default ON — this is the whole point
    of 'always serve the latest with the best'. Turn it OFF to pin a process to its current version (e.g. to
    reproduce a bug against a known commit)."""
    import os
    from . import config
    v = os.getenv("SPENDGUARD_MCP_AUTO_RESPAWN")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off")
    return bool(config._cfg_get("advisor", "mcp_auto_respawn", True))


def _arm_and_verify_mcp_daemon_gate():
    """Deliberately import and gate core SDKs, independent of import-time auto-install policy."""
    import importlib
    from . import gate

    gate.install()
    for sdk_package in gate.core_gated_sdk_packages():
        try:
            importlib.import_module(sdk_package)
        except ImportError:
            continue
    if not gate._enforcement_armed():
        raise gate.SpendGateRefused(
            "spendguard MCP daemon startup refused: the spend gate is NOT enforcing in this interpreter. "
            f"Interpreter: {sys.executable}. Launch the daemon from a venv with llm-spendguard and the required "
            "LLM SDK extras installed, unset GATE_DISABLE, and run `spendguard doctor` until it reports "
            "`ENFORCING HERE: YES`. SPENDGUARD_NO_AUTOINSTALL may remain set; daemon startup arms explicitly."
        )


def serve_stdio(inp=None, outp=None):
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout — the MCP stdio transport. Blocking;
    ends on EOF. A malformed line gets a parse-error reply and the loop continues (one bad frame never kills the
    server).

    DRAIN-AND-CUTOVER (auto-respawn): after each response is flushed — so we are BETWEEN requests with nothing in
    flight — if a newer gate-green commit has been deployed AND a fresh process would actually serve it
    (release.should_respawn, which is loop-safe), the server EXITS. The MCP client respawns it on its next request,
    now running the latest code. Exiting here (never mid-request) means no request is dropped. This is what makes
    'always serve the latest' true for a long-running stdio server; disable with SPENDGUARD_MCP_AUTO_RESPAWN=0."""
    _arm_and_verify_mcp_daemon_gate()
    from . import fleet, release
    from .launchd_labels import MCP_STDIO_LAUNCHD_LABEL
    fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON,
                            supervision_mode=fleet.SUPERVISION_LAUNCHD,
                            supervision_ref=MCP_STDIO_LAUNCHD_LABEL)
    inp = inp or sys.stdin
    outp = outp or sys.stdout
    respawn_on_deploy = _auto_respawn_enabled()
    announced = False
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
        if respawn_on_deploy and release.should_respawn():
            st = release.release_status()
            sys.stderr.write("[spendguard] mcp: a newer green commit was deployed (%s vs served %s) — exiting to "
                             "hand off to a fresh process on the latest code.\n"
                             % ((st.get("green") or {}).get("short"), (st.get("served") or {}).get("short")))
            sys.stderr.flush()
            return
        # NUDGE THE CLIENT TO RE-LIST TOOLS ONCE — so a tool ADDED since it cached tools/list (e.g. because this
        # process just auto-respawned onto newer code) appears WITHOUT a manual reconnect. Sent only after the
        # first SUBSTANTIVE request, never during the initialize/initialized handshake, as an MCP
        # notifications/tools/list_changed (we advertised capabilities.tools.listChanged). Harmless if the client
        # re-lists on its own or ignores it. Skipped above when we are handing off — the fresh process announces.
        _method = req.get("method") if isinstance(req, dict) else None
        if not announced and _method not in (None, "initialize", "initialized", "notifications/initialized"):
            outp.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}) + "\n")
            outp.flush()
            announced = True


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
        print(f"  → restart Claude Code (or reconnect MCP) to pick up all {len(_TOOLS)} tools")
    return 0


def _prewarm_codex_daemon():
    try:
        import threading

        from . import codex_daemon, codex_exec

        if not codex_exec._daemon_enabled():
            return

        def _run():
            try:
                codex_daemon.ensure_running()
                print("[spendguard] codex app-server daemon pre-warmed", file=sys.stderr, flush=True)
            except Exception as err:
                print(f"[spendguard] codex daemon pre-warm failed (non-fatal): {str(err)[:80]}",
                      file=sys.stderr, flush=True)

        print("[spendguard] pre-warming codex app-server daemon (background)…", file=sys.stderr, flush=True)
        threading.Thread(target=_run, daemon=True).start()
    except Exception as err:
        print(f"[spendguard] codex daemon pre-warm failed (non-fatal): {str(err)[:80]}",
              file=sys.stderr, flush=True)


def build_mcp_http_handler():
    """The `POST /mcp` request handler for the warm HTTP daemon, as a module-level factory so the exact
    over-the-wire response SHAPE is testable without binding the real :4913 daemon or pre-warming codex. Routes
    every POST through the SAME handle() as the stdio transport (stdio + http stay in lockstep)."""
    _arm_and_verify_mcp_daemon_gate()
    import os as _os
    from http.server import BaseHTTPRequestHandler
    from . import release

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):            # quiet — no stderr access log per request
            pass

        def do_HEAD(self):
            """Advertise a mutation-free attestation protocol without dispatch or self-heal."""
            if self.path.rstrip("/") != "/mcp":
                self.send_response(HTTPStatus.NOT_FOUND)
            else:
                self.send_response(HTTPStatus.OK)
                self.send_header(MCP_READ_ONLY_ATTESTATION_HEADER, MCP_READ_ONLY_ATTESTATION_HEADER_VALUE)
            self.send_header(HTTP_CONTENT_LENGTH_HEADER, str(0))
            self.end_headers()

        def _send(self, body, code=HTTPStatus.OK):
            data = (body or "").encode("utf-8")
            self.send_response(code)
            self.send_header(HTTP_CONTENT_TYPE_HEADER, JSON_CONTENT_TYPE)
            self.send_header(HTTP_CONTENT_LENGTH_HEADER, str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _send_accepted(self):
            # A JSON-RPC NOTIFICATION (e.g. notifications/initialized) carries NOTHING to return. Per the MCP
            # Streamable HTTP transport, a POST that contains only notifications/responses MUST be answered with
            # `202 Accepted` and NO body — and crucially NOT a `200` with an empty `application/json` body. A
            # strict client (codex's app-server rmcp `StreamableHttpClientWorker`) that sees
            # `200 + Content-Type: application/json` will try to serde_json-parse the body; zero bytes then raise
            # "Deserialize error: EOF while parsing a value at line 1 column 0, when send initialized notification",
            # the rmcp transport worker quits fatally ("Transport channel closed"), and the app-server reconnects
            # in a loop (the warm-daemon churn). A bare 202 tells the client there is nothing to read.
            self.send_response(HTTPStatus.ACCEPTED)
            self.send_header(HTTP_CONTENT_LENGTH_HEADER, str(0))
            self.end_headers()

        def do_POST(self):
            if self.path.rstrip("/") != "/mcp":
                self._send(json.dumps(_err(None, -32601, "not found: POST /mcp")), HTTPStatus.NOT_FOUND)
                return
            try:
                n = int(self.headers.get(HTTP_CONTENT_LENGTH_HEADER) or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except Exception as e:
                self._send(json.dumps(_err(None, -32700, f"parse error: {e}")))
                return
            with _mcp_http_request_ledger_lifetime(self.connection):
                resp = handle(req)             # the ONE shared dispatch (stdio + http in lockstep)
            if isinstance(req, dict) and JSON_RPC_ID_MEMBER not in req:
                self._send_accepted()          # NEVER a 200 with an empty application/json body (kills strict rmcp)
            else:
                self._send(json.dumps(resp, default=str))
            try:                               # deploy self-heal — exit BETWEEN requests so the supervisor respawns
                request_params = req.get("params") if isinstance(req, dict) else None
                request_meta = request_params.get("_meta") if isinstance(request_params, dict) else None
                read_only_attestation = (isinstance(request_meta, dict)
                                         and request_meta.get(MCP_READ_ONLY_ATTESTATION_META_KEY) is True)
                if not read_only_attestation and _auto_respawn_enabled() and release.should_respawn():
                    self.wfile.flush()
                    _os._exit(0)
            except Exception:
                pass

    return _Handler


def serve_http(host="127.0.0.1", port=4913):
    """MCP over HTTP (stdlib, stateless POST /mcp → the SAME handle() the stdio transport uses) — one WARM daemon every
    session CONNECTS to via urllib instead of spawning a `spendguard mcp` stdio copy per session (the per-session
    spawn that accumulates). Dependency-free (http.server), matching spendguard's portable surfaces and ccwatch's
    in-board /mcp. Threaded so concurrent clients are independent; each request is self-contained (no session state).
    Carries the SAME deploy self-heal as serve_stdio: after a response is flushed (between requests, nothing in
    flight), if a newer green commit was deployed the process exits so the supervisor respawns it on the latest code."""
    from .mcp_fd_lifecycle import (mcp_descriptor_policy, raise_mcp_descriptor_soft_limit,
                                   enforce_mcp_descriptor_headroom, MCPDescriptorPressureError,
                                   service_mcp_spend_deadletters)
    target, fraction, poll_interval = mcp_descriptor_policy()
    raise_mcp_descriptor_soft_limit(target)
    service_mcp_spend_deadletters()
    handler_class = build_mcp_http_handler()
    from http.server import ThreadingHTTPServer
    from . import fleet
    from .launchd_labels import MCP_HTTP_LAUNCHD_LABEL
    fleet.register_consumer(role=fleet.CONSUMER_ROLE_DAEMON,
                            supervision_mode=fleet.SUPERVISION_LAUNCHD,
                            supervision_ref=MCP_HTTP_LAUNCHD_LABEL)
    _prewarm_codex_daemon()
    class DescriptorGuardedMCPHTTPServer(ThreadingHTTPServer):
        def service_actions(self):
            from . import release
            if _auto_respawn_enabled():
                release.exit_if_stale(logging.getLogger(__name__).warning)
            enforce_mcp_descriptor_headroom(fraction)
            service_mcp_spend_deadletters()

    with DescriptorGuardedMCPHTTPServer((host, port), handler_class) as server:
        try:
            server.serve_forever(poll_interval=poll_interval)
        except MCPDescriptorPressureError:
            logging.getLogger(__name__).exception("Stopping MCP HTTP daemon for supervised descriptor recovery")


def cmd(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard mcp",
                                 description="spendguard's tools over MCP: the model-advisor plus read-only spend & "
                                             "compaction queries. Default stdio JSON-RPC; --http serves one WARM "
                                             "daemon sessions connect to. Register with `spendguard install-mcp`.")
    add_registered_cli_argument(ap, 'mcp/mcp_server.cmd/ap/--http')
    import os as _os
    add_registered_cli_argument(ap, 'mcp/mcp_server.cmd/ap/--host', default=_os.environ.get('SPENDGUARD_MCP_HOST', '127.0.0.1'))
    add_registered_cli_argument(ap, 'mcp/mcp_server.cmd/ap/--port', type=int, default=int(_os.environ.get('SPENDGUARD_MCP_PORT', '4913')))   # family: 4900 ccwatch · 4910 7thsense · 4911 symgrep · 4912 honestreview · 4913 spendguard
    args = ap.parse_args(argv)
    if args.http:
        # WARM SHARED DAEMON = SINGLETON (one per port): a second start detects the incumbent (flock) and exits 0, so a
        # supervisor can (re)start it idempotently, never racing http.server into a bind "address already in use".
        from . import config
        try:
            _cm = config.single_instance_lock(f"spendguard-mcp-http-{args.port}")
        except Exception:
            _cm = None
        if _cm is not None:
            with _cm as _got:
                if not _got:
                    sys.stderr.write(f"spendguard mcp: a warm HTTP daemon is already serving on :{args.port} — not "
                                     f"starting a second (the warm daemon is a singleton; connect to the running one).\n")
                    return 0
                serve_http(args.host, args.port)
        else:
            serve_http(args.host, args.port)
        return 0
    serve_stdio()
    return 0
