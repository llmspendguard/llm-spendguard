"""Two-hop delegation: classify on a $0 lane, then execute on a configured subscription plan.

This cannot reroute the hosting coding agent's own conversational turns. It hands a self-contained sub-task to a
different provider plan. Meaning (task kind and provider suitability) is decided by the classifier model; source
code contains no keyword router. Provider/model choices are derived from advisor.lane_models and lane_registry.
"""
import importlib
import json
import os
import sqlite3

from . import config, guard, lane_balance, lane_registry, pricing, provider_tokens

CLASSIFY_INTENT = "spendguard:classify"
DEFAULT_DELEGATE_INTENT = "spendguard:delegate-task"
CLASSIFY_OUTPUT_TOKENS = 800

_CLASSIFY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": ["oneshot", "agentic", "needs_claude"]},
        "provider": {"type": ["string", "null"]},
        "self_contained": {"type": "boolean"},
        "why": {"type": "string"},
    },
    "required": ["kind", "provider", "self_contained", "why"], "nonempty": ["why"],
}

_CLASSIFY_SYSTEM = (
    "Classify a task for safe offloading from the current Claude coding session. Return only the requested JSON. "
    "oneshot means one independent answer/extraction/summary with no tools; agentic means a self-contained multi-step "
    "task that may edit the supplied workspace and run commands; needs_claude means it depends on live conversational "
    "context or state not included in the task/files. Choose provider only from the supplied configured plan names. "
    "Be conservative: non-self-contained work is needs_claude."
)


def _configured_plan_models():
    declared = config._cfg_get("advisor", "lane_models", {}) or {}
    return declared if isinstance(declared, dict) else {}


def _model_for_plan(plan, kind):
    # This is intentionally not lane_catalog.lane_model_for_tier: a plain-string delegation model serves both task
    # kinds, while the catalog resolver requires that string to be enrolled in advisor.tiers for the requested tier.
    declaration = _configured_plan_models().get(plan)
    if isinstance(declaration, dict):
        tier = "strong" if kind == "agentic" else "cheap"
        return declaration.get(tier) or next((v for v in declaration.values() if isinstance(v, str) and v), None)
    return declaration if isinstance(declaration, str) and declaration else None


def delegation_lanes_ready(route_kind="oneshot"):
    """Configured plans ready for exactly one delegation route kind.

    A one-shot delegates through ``lane_balance`` and therefore uses that lane's established reachability probe.
    An agentic task executes the provider module directly, so its readiness is the executor's own CLI/key
    availability plus its definitive auth status when it exposes one. In particular, Codex reuses
    ``codex_exec.available`` + ``codex_exec.auth_status``; a failed one-shot probe is irrelevant to that route.
    """
    if route_kind not in ("oneshot", "agentic"):
        raise ValueError(f"unsupported delegation route kind {route_kind!r}")
    configured = _configured_plan_models()
    ready = []
    if route_kind == "oneshot":
        from . import lanes, reliability
        status_by_lane = {row["lane"]: row for row in lanes.lanes_status()["lanes"]}
        health_down = {row["resource"] for row in reliability.health_reds() if row.get("kind") == "lane"}
        for plan in sorted(configured):
            row = status_by_lane.get(plan)
            if (row and row.get("auth") == "ok" and (row.get("cli") or not row.get("activate"))
                    and plan not in health_down):
                ready.append(plan)
    else:
        for plan in sorted(configured):
            spec = lane_registry.lane_spec(plan)
            if not spec:
                continue
            module = importlib.import_module(f".{spec['exec']}", __package__)
            try:
                executor_available = bool(module.available())
                auth = module.auth_status().get("authed") if hasattr(module, "auth_status") else None
            except Exception:
                executor_available, auth = False, None
            if executor_available and auth is True:
                ready.append(plan)
    return {"ready": ready,
            "unavailable": sorted(set(configured) - set(ready)),
            "not_configured": sorted(set(lane_registry.all_lanes()) - set(configured))}


def delegation_doctor_line():
    """Render the doctor summary for two-hop delegation readiness."""
    oneshot = delegation_lanes_ready("oneshot")
    agentic = delegation_lanes_ready("agentic")
    oneshot_ready = ", ".join(oneshot["ready"]) or "none"
    agentic_ready = ", ".join(agentic["ready"]) or "none"
    not_configured = ", ".join(oneshot["not_configured"]) or "none"
    return (f"delegation ready: oneshot {oneshot_ready} ({len(oneshot['ready'])}) · "
            f"agentic {agentic_ready} ({len(agentic['ready'])}) · not configured: {not_configured}")


def _resolve_plan_name(requested):
    plans = _configured_plan_models()
    if requested in plans:
        return requested
    matches = [lane for lane in plans if lane.split("-", 1)[0] == requested]
    return matches[0] if len(matches) == 1 else None


def _whole_file_evidence(paths):
    parts = []
    for raw in paths or []:
        path = os.path.abspath(os.path.expanduser(raw))
        with open(path, encoding="utf-8", errors="replace") as source:
            parts.append(f"\nFILE {path}\n{source.read()}\nEND FILE {path}")
    return "".join(parts)


def classify_task(task, files=None, provider="auto", exclude_plans=None):
    """Agentically classify the WHOLE task and WHOLE attached files on an existing $0 lane."""
    from . import gate
    gate.require()                         # fail closed: even this plan-routed LLM call runs only in an enforcing process
    plans = _configured_plan_models()
    if not plans:
        return {"error": "no configured plans (set advisor.lane_models)"}
    explicit = None if provider == "auto" else _resolve_plan_name(provider)
    if provider != "auto" and explicit is None:
        return {"error": f"provider {provider!r} is not a configured advisor.lane_models plan"}
    excluded = set(exclude_plans or ())
    menu = [explicit] if explicit else sorted(plan for plan in plans if plan not in excluded)
    if not menu:
        return {"error": "no eligible configured plans remain after exclusions"}
    prompt = (f"TASK (complete):\n{task}\n{_whole_file_evidence(files)}\n\n"
              f"CONFIGURED PLAN NAMES: {json.dumps(menu)}\n"
              f"Provider is {'fixed to ' + explicit if explicit else 'for you to choose from that exact list'}. "
              "Return kind, provider, self_contained, why.")
    from . import calls
    with calls.gate_internal():
        response = lane_balance.delegate(prompt, system=_CLASSIFY_SYSTEM, intent=CLASSIFY_INTENT,
                                         max_tokens=CLASSIFY_OUTPUT_TOKENS, schema=_CLASSIFY_SCHEMA)
    if response.get("error") or not response.get("text"):
        return {"error": response.get("error") or "classifier returned no result"}
    try:
        obj = json.loads(response["text"])
    except (TypeError, json.JSONDecodeError):
        return {"error": "classifier returned invalid JSON"}
    kinds = set(_CLASSIFY_SCHEMA["properties"]["kind"]["enum"])
    valid_provider = obj.get("provider") in menu if obj.get("provider") is not None else obj.get("kind") == "needs_claude"
    if (set(obj) != set(_CLASSIFY_SCHEMA["required"]) or obj.get("kind") not in kinds
            or not isinstance(obj.get("self_contained"), bool) or not isinstance(obj.get("why"), str)
            or not obj.get("why").strip() or not valid_provider):
        return {"error": "classifier result failed validation"}
    if explicit and obj["provider"] != explicit and obj["kind"] != "needs_claude":
        return {"error": "classifier changed the explicitly selected provider"}
    obj["classifier_lane"] = response.get("lane")
    obj["classifier_model"] = response.get("model")
    return obj


def _estimated_tokens(task, files, provider, model, intent):
    from . import bulkgate
    full = task + _whole_file_evidence(files)
    input_tokens = provider_tokens.count_text(full, provider=provider, model=model)
    measured = bulkgate.maxtokens(intent)
    output_tokens = measured.get("p99") or config._cfg_get("advisor", "route_est_out", None)
    if output_tokens is None:
        output_tokens = lane_balance.ROUTE_EST_OUT_DEFAULT
    return int(input_tokens), int(output_tokens), "measured p99" if measured.get("p99") else "config nominal"


def _measured_claude_overage_ratio():
    """Invoice overage $ / Claude plan est-value in the same invoiced months; None means insufficient evidence."""
    con = sqlite3.connect(config.db_path())
    try:
        invoice_rows = con.execute(
            "SELECT substr(occurred_at,1,7), SUM(CAST(realtime_usd AS REAL)) FROM spend_events "
            "WHERE source='anthropic-invoice' AND intent LIKE 'anthropic-invoice:cc-overage%' GROUP BY 1"
        ).fetchall()
        months = [month for month, dollars in invoice_rows if float(dollars or 0) > 0]
        if not months:
            return None
        marks = ",".join("?" for _ in months)
        value = con.execute(
            f"SELECT SUM(CAST(est_chat_usd AS REAL)) FROM spend_events WHERE source='claude-code' "
            f"AND substr(occurred_at,1,7) IN ({marks})", months).fetchone()[0]
    finally:
        con.close()
    billed = sum(float(dollars or 0) for _month, dollars in invoice_rows)
    return billed / float(value) if value and float(value) > 0 else None


def _estimate_route(task, files, classification, intent):
    plan = classification.get("provider")
    model = _model_for_plan(plan, classification["kind"])
    spec = lane_registry.lane_spec(plan)
    if not model or not spec:
        return {"error": f"configured plan {plan!r} has no runnable lane/model"}
    in_tok, out_tok, out_basis = _estimated_tokens(task, files, spec["provider"], model, intent)
    try:
        est_value = float(pricing.realtime_cost(model, in_tok, out_tok, provider=spec["provider"]) or 0)
    except (KeyError, TypeError, ValueError):
        return {"error": f"configured model {model!r} is unpriced; cannot produce an honest estimate"}
    ratio = _measured_claude_overage_ratio()
    return {"plan": plan, "model": model, "input_tokens": in_tok, "output_tokens": out_tok,
            "output_basis": out_basis, "real_api_usd": 0.0, "est_value_usd": est_value,
            "saved_overage_usd": (est_value * ratio if ratio is not None else None),
            "overage_basis": "ledger measured" if ratio is not None else "unavailable"}


def _route_agentic(plan, prompt, model, timeout):
    spec = lane_registry.lane_spec(plan)
    if not spec:
        return {"error": f"unknown configured plan {plan!r}"}
    module = importlib.import_module(f".{spec['exec']}", __package__)
    kwargs = {"model": model}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if spec["exec"] == "codex_exec":
        kwargs["sandbox"] = "workspace-write"
    return module.run_prompt(prompt, **kwargs)


def delegate_task(task, files=None, intent=None, provider="auto", execute=False, timeout=None):
    """Classify, estimate, and optionally execute one delegated task. Default is zero-execution dry-run."""
    intent = intent or DEFAULT_DELEGATE_INTENT
    resolved_provider = None if provider == "auto" else _resolve_plan_name(provider)
    classification = classify_task(task, files=files, provider=(resolved_provider or provider))
    if classification.get("error"):
        return {"status": "error", "classification": classification}
    if classification["kind"] == "needs_claude" or not classification["self_contained"]:
        return {"status": "refused", "classification": classification, "why": classification["why"]}
    readiness = delegation_lanes_ready(classification["kind"])
    ready = readiness["ready"]
    if classification.get("provider") not in ready:
        alternatives = ", ".join(ready)
        return {"status": "refused", "classification": classification,
                "why": f"classifier selected unavailable plan {classification.get('provider')!r}; "
                       f"ready alternatives: {alternatives}",
                "ready_alternatives": ready}
    estimate = _estimate_route(task, files, classification, intent)
    if estimate.get("error"):
        return {"status": "error", "classification": classification, "estimate": estimate}
    result = {"status": "estimate", "classification": classification, "estimate": estimate}
    if not execute:
        return result
    cap = config.intent_cap(intent)
    if cap is not None and estimate["real_api_usd"] > float(cap):
        return {**result, "status": "refused", "why": f"estimated real API cost exceeds intent cap for {intent}"}
    prompt = task + _whole_file_evidence(files)
    if classification["kind"] == "oneshot":
        execution = lane_balance.delegate(prompt, lanes=[estimate["plan"]], intent=intent)
    else:
        execution = _route_agentic(estimate["plan"], prompt, estimate["model"], timeout)
    if execution.get("error") or not execution.get("text"):
        return {**result, "status": "error", "execution": execution}
    actual_in = int(execution.get("in_tok") or estimate["input_tokens"])
    actual_out = int(execution.get("out_tok") or estimate["output_tokens"])
    spec = lane_registry.lane_spec(estimate["plan"])
    actual_value = float(pricing.realtime_cost(estimate["model"], actual_in, actual_out,
                                               provider=spec["provider"]) or 0)
    ratio = _measured_claude_overage_ratio()
    avoided = actual_value * ratio if ratio is not None else None
    actual_api = float(execution.get("cost") or 0)
    guard.record_decision(intent=intent, requested_model="claude-overage", chosen_model=estimate["model"],
                          counterfactual_usd=avoided or 0, actual_usd=actual_api,
                          saved_usd=max(0, (avoided or 0) - actual_api),
                          basis="delegate", why=classification["why"])
    guard.record_saving("delegate", max(0, (avoided or 0) - actual_api))
    receipt = {"plan": estimate["plan"], "model": estimate["model"], "real_api_usd": actual_api,
               "est_value_usd": actual_value, "saved_overage_usd": avoided}
    return {**result, "status": "executed", "execution": execution, "receipt": receipt}


def delegate_cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="spendguard delegate",
        description="Classify and offload one SELF-CONTAINED subtask to another configured provider plan. "
                    "Dry-run is the default: classify + estimate only, with est $ saved vs Claude overage.",
        epilog=("examples:\n"
                "  spendguard delegate 'summarize this design' --files docs/design.md\n"
                "  spendguard delegate 'fix and test this module' --provider codex --files src/app.py --yes\n"
                "  spendguard delegate 'analyze these logs' --intent incident-review --yes"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task", help="complete, self-contained task to classify and delegate")
    parser.add_argument("--files", help="comma-separated supporting files; every file is read in full")
    parser.add_argument("--intent", help="attribution/cost-control intent (default: spendguard:delegate-task)")
    parser.add_argument("--provider", default="auto",
                        help="configured plan to use, or auto for agentic selection among ready lanes (default: auto)")
    parser.add_argument("--estimate", action="store_true", help="force dry-run even when --yes is also present")
    parser.add_argument("--yes", action="store_true", help="execute hop 2 after classification and estimate")
    parser.add_argument("--timeout", type=float, help="optional execution timeout in seconds")
    args = parser.parse_args(argv)
    result = delegate_task(args.task, files=[p for p in (args.files or "").split(",") if p], intent=args.intent,
                           provider=args.provider, execute=args.yes and not args.estimate, timeout=args.timeout)
    classification = result.get("classification") or {}
    print(f"classification: {classification.get('kind', 'error')}"
          f"{(' → ' + str(classification.get('provider'))) if classification.get('provider') else ''}"
          f" — {classification.get('why') or classification.get('error', '')}")
    estimate = result.get("estimate") or {}
    if estimate and not estimate.get("error"):
        saved = (f"~${estimate['saved_overage_usd']:.4f}" if estimate.get("saved_overage_usd") is not None
                 else "unavailable (no measured ledger rate)")
        print(f"route: {estimate['plan']} ({estimate['model']})  ::  real API $0.0000  ::  "
              f"est-value ${estimate['est_value_usd']:.4f} on plan  ::  saved {saved} vs Claude overage")
    if result["status"] == "executed":
        print(result["execution"]["text"])
        receipt = result["receipt"]
        saved = (f"~${receipt['saved_overage_usd']:.4f}" if receipt["saved_overage_usd"] is not None
                 else "unavailable")
        print(f"delegated to {receipt['plan']} ({receipt['model']})  ::  real API ${receipt['real_api_usd']:.4f}  ::  "
              f"est-value ${receipt['est_value_usd']:.4f} on {receipt['plan']}  ::  saved {saved} vs Claude overage")
    elif result["status"] in ("refused", "error"):
        print(f"delegate {result['status']}: {result.get('why') or estimate.get('error') or classification.get('error')}")
    return 0 if result["status"] in ("estimate", "executed") else 2
