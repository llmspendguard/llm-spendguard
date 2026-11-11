"""Usage-shaped provider reliability bank.

The bank is derived from retained, complete call_io evidence and the provider/lane
registries.  ``estimate`` is always zero-spend.  ``run`` defaults to replay-only;
metered work requires both ``--execute`` and an estimate within ``--budget-usd``.
"""
import spendguard

import argparse
import json
import math
from collections import defaultdict
from dataclasses import dataclass

from ... import config, pricing
from ...egress.share import _scrub_text
from ...generate.execution import adapters
from ...generate.execution.schema import SCHEMA_JSON_OBJECT, schema_capability
from ...generate.work_integrity.output_contract import check_item
from ...store.call_corpus import call_corpus_accessors
from ...store.reference_data import lane_catalog, model_catalog


DEFAULT_STABILITY_RUNS = 3
DEFAULT_BUDGET_USD = 1.0
DEFAULT_REPLAY_SAMPLE_CELLS = 6
DEFAULT_ESTIMATED_OUTPUT_TOKENS = 500
ACTUAL_SPEND_UNAVAILABLE = 0.0
MINIMUM_TOKEN_COUNT = 1
PATH_CHAT = "chat"
PATH_JSON_STRICT = "json-strict"
PATH_JSON_OBJECT = "json-object"
PATH_EMBED = "embed"
PATH_BATCH = "batch"
PATH_VISION = "vision"
USAGE_BANK_PATHS = (PATH_CHAT, PATH_JSON_STRICT, PATH_JSON_OBJECT, PATH_EMBED, PATH_BATCH, PATH_VISION)
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_UNREVIEWED = "UNREVIEWED"


@dataclass(frozen=True, order=True)
class UsageBankCell:
    provider: str
    path: str
    effort: str
    model: str


@dataclass(frozen=True)
class UsageBankSample:
    cell: UsageBankCell
    prompt: object
    output: object
    system: object
    schema: object
    input_tokens: int
    output_tokens: int


def scrub_usage_bank_value(value, intent=None):
    """Recursively scrub every string while preserving the complete structure."""
    if isinstance(value, str):
        return _scrub_text(value, intent)
    if isinstance(value, list):
        return [scrub_usage_bank_value(item, intent) for item in value]
    if isinstance(value, tuple):
        return tuple(scrub_usage_bank_value(item, intent) for item in value)
    if isinstance(value, dict):
        return {key: scrub_usage_bank_value(item, intent) for key, item in value.items()}
    return value


def _decode_schema(whole_schema):
    if not whole_schema:
        return None
    decoded = json.loads(whole_schema) if isinstance(whole_schema, str) else whole_schema
    if isinstance(decoded, dict) and decoded.get("type") == "json_schema":
        return decoded["json_schema"]["schema"]
    if isinstance(decoded, dict) and decoded.get("type") == "json_object":
        return {"type": "object"}
    return decoded


def _catalog_mode(provider, model):
    record = model_catalog.model_record(model) or {}
    if record.get("provider") not in (None, provider):
        return None
    capabilities = record.get("capabilities") or {}
    return capabilities.get("mode")


def _usage_path(provider, model, batch, schema):
    mode = _catalog_mode(provider, model)
    if mode == "embedding":
        return PATH_EMBED
    if mode == "vision":
        return PATH_VISION
    if batch:
        return PATH_BATCH
    if schema:
        return PATH_JSON_STRICT if schema_capability(provider) != SCHEMA_JSON_OBJECT else PATH_JSON_OBJECT
    return PATH_CHAT


def read_complete_usage_bank_samples():
    """Read WHOLE replay bodies through call_io's sanctioned pooled-ledger pattern."""
    ensure_schema = call_corpus_accessors.resolve__ensure_callio_schema
    with config.ledger_op("callio", ensure_schema) as connection:
        rows = connection.execute(
            "SELECT intent,provider,model,batch,prompt,output,in_tok,out_tok,system,req_schema,effort "
            "FROM call_io WHERE prompt IS NOT NULL AND length(prompt)>0 "
            "AND COALESCE(truncated,0)=0 AND shape_complete=1"
        ).fetchall()
    samples = []
    for intent, provider, model, batch, prompt, output, in_tok, out_tok, system, encoded_schema, effort in rows:
        schema = _decode_schema(encoded_schema)
        cell = UsageBankCell(
            provider=provider,
            path=_usage_path(provider, model, batch, schema),
            effort=effort or "none",
            model=model,
        )
        samples.append(UsageBankSample(
            cell=cell,
            prompt=scrub_usage_bank_value(prompt, intent),
            output=scrub_usage_bank_value(output, intent),
            system=scrub_usage_bank_value(system, intent),
            schema=scrub_usage_bank_value(schema, intent),
            input_tokens=max(int(in_tok or MINIMUM_TOKEN_COUNT), MINIMUM_TOKEN_COUNT),
            output_tokens=max(int(out_tok or DEFAULT_ESTIMATED_OUTPUT_TOKENS), MINIMUM_TOKEN_COUNT),
        ))
    return samples


def representative_usage_bank_samples(samples):
    """Return median and p95 token-size samples for every observed cell."""
    grouped = defaultdict(list)
    for sample in samples:
        grouped[sample.cell].append(sample)
    representatives = {}
    for cell, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: row.input_tokens + row.output_tokens)
        indexes = {len(ordered) // 2, max(0, math.ceil(len(ordered) * 0.95) - 1)}
        representatives[cell] = [ordered[index] for index in sorted(indexes)]
    return representatives


def _cheapest_catalog_model(provider, path):
    candidates = []
    for model, record in model_catalog.all_records().items():
        if record.get("provider") != provider:
            continue
        mode = ((record.get("capabilities") or {}).get("mode"))
        if path == PATH_EMBED and mode != "embedding":
            continue
        if path != PATH_EMBED and mode == "embedding":
            continue
        try:
            rate = pricing.price(model, provider=provider)
        except (KeyError, ValueError):
            continue
        price_key = "batch_out" if path == PATH_BATCH else "out"
        if rate.get(price_key) is not None:
            candidates.append((rate[price_key], model))
    return min(candidates)[1] if candidates else None


def estimate_usage_bank(samples, stability_runs=DEFAULT_STABILITY_RUNS):
    """Price every observed metered cell without opening a provider call."""
    representatives = representative_usage_bank_samples(samples)
    by_provider = defaultdict(float)
    rows = []
    for cell, cell_samples in sorted(representatives.items()):
        model = _cheapest_catalog_model(cell.provider, cell.path)
        if model is None:
            rows.append({"cell": cell, "model": None, "probes": 0, "cost_usd": None})
            continue
        anchor = cell_samples[-1]
        cost_fn = pricing.batch_cost if cell.path == PATH_BATCH else pricing.realtime_cost
        one_cost = cost_fn(model, anchor.input_tokens, anchor.output_tokens, provider=cell.provider)
        cost = one_cost * stability_runs
        by_provider[cell.provider] += cost
        rows.append({"cell": cell, "model": model, "probes": stability_runs, "cost_usd": cost})
    return {
        "cells": len(representatives),
        "lanes": lane_catalog.catalog(),
        "metered_probes": sum(row["probes"] for row in rows),
        "providers_usd": dict(sorted(by_provider.items())),
        "total_usd": sum(by_provider.values()),
        "rows": rows,
    }


def replay_usage_bank(samples, cell_limit=None):
    representatives = representative_usage_bank_samples(samples)
    selected = sorted(representatives.items())
    if cell_limit is not None:
        selected = selected[:cell_limit]
    results = []
    for cell, cell_samples in selected:
        checks = []
        for sample in cell_samples:
            if sample.schema is None:
                checks.append((None, "no retained output contract"))
            else:
                ok, _salvaged, reason = check_item(sample.output, sample.schema)
                checks.append((ok, reason))
        decided = [ok for ok, _reason in checks if ok is not None]
        status = STATUS_UNREVIEWED if not decided else STATUS_PASS if all(decided) else STATUS_FAIL
        results.append({"cell": cell, "status": status, "checks": checks})
    return {"covered": len(results), "denominator": len(representatives), "results": results}


def enforce_usage_bank_execution_gate(estimate, execute, budget_usd):
    if not execute:
        raise RuntimeError("metered pass refused: explicit --execute is required")
    if budget_usd is None:
        raise RuntimeError("metered pass refused: --budget-usd is required")
    if estimate["total_usd"] > budget_usd:
        raise RuntimeError(
            f"metered pass refused: estimate ${estimate['total_usd']:.6f} exceeds budget ${budget_usd:.6f}"
        )


def execute_usage_bank_metered(samples, execute=False, budget_usd=None, stability_runs=DEFAULT_STABILITY_RUNS):
    """Run eligible cells while enforcing the budget before and after every call.

    Batch, embedding, and vision cells stay UNREVIEWED until their native submitter
    has complete retained inputs; silently substituting chat would test a different path.
    """
    spendguard.require()
    estimate = estimate_usage_bank(samples, stability_runs)
    enforce_usage_bank_execution_gate(estimate, execute, budget_usd)
    representatives = representative_usage_bank_samples(samples)
    estimate_rows = {row["cell"]: row for row in estimate["rows"]}
    actual_spend = ACTUAL_SPEND_UNAVAILABLE
    results = []
    unsupported_paths = {PATH_BATCH, PATH_EMBED, PATH_VISION}
    for cell, cell_samples in sorted(representatives.items()):
        estimate_row = estimate_rows[cell]
        if cell.path in unsupported_paths or estimate_row["model"] is None:
            results.append({"cell": cell, "status": STATUS_UNREVIEWED,
                            "reason": "native path or priced model unavailable", "runs": []})
            continue
        projected_cell_cost = estimate_row["cost_usd"] or ACTUAL_SPEND_UNAVAILABLE
        if actual_spend + projected_cell_cost > budget_usd:
            raise RuntimeError("metered pass stopped before call: remaining budget cannot cover the cell estimate")
        run_rows = []
        for run_index in range(stability_runs):
            sample = cell_samples[run_index % len(cell_samples)]
            response = adapters.call(
                f"{cell.provider}:{estimate_row['model']}", sample.prompt,
                system=sample.system or None, schema=sample.schema,
                max_tokens=max(sample.output_tokens, MINIMUM_TOKEN_COUNT),
                effort=None if cell.effort == "none" else cell.effort,
                metered_only=True, no_substitution=True, measurement=True,
                sig="spendguard:usage-bank",
            )
            response_cost = float(response.get("cost") or ACTUAL_SPEND_UNAVAILABLE)
            actual_spend += response_cost
            if actual_spend > budget_usd:
                raise RuntimeError("metered pass exceeded --budget-usd on actual cumulative spend")
            if response.get("error"):
                run_rows.append({"ok": False, "cost_usd": response_cost, "reason": response.get("error")})
                continue
            ok, _salvaged, reason = check_item(response.get("text"), sample.schema) if sample.schema else (True, False, "")
            run_rows.append({"ok": ok, "cost_usd": response_cost, "reason": reason})
        outcomes = [row["ok"] for row in run_rows]
        status = STATUS_PASS if outcomes and all(outcomes) else STATUS_FAIL
        results.append({"cell": cell, "status": status, "stable": len(set(outcomes)) == 1, "runs": run_rows})
    return {"estimate": estimate, "actual_api_usd": actual_spend, "results": results}


def _print_estimate(estimate):
    print(f"cells={estimate['cells']} metered_probes={estimate['metered_probes']}")
    for provider, cost in estimate["providers_usd"].items():
        print(f"{provider}: ${cost:.6f}")
    print(f"Total real-$ estimate: ${estimate['total_usd']:.6f} :: Est sub value $0.000000")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spendguard usage-bank")
    subparsers = parser.add_subparsers(dest="command", required=True)
    estimate_parser = subparsers.add_parser("estimate")
    estimate_parser.add_argument("--runs", type=int, default=DEFAULT_STABILITY_RUNS)
    estimate_parser.add_argument("--budget-usd", type=float, default=DEFAULT_BUDGET_USD)
    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("--cell-limit", type=int, default=DEFAULT_REPLAY_SAMPLE_CELLS)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--runs", type=int, default=DEFAULT_STABILITY_RUNS)
    execute_parser.add_argument("--budget-usd", type=float, required=True)
    execute_parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    samples = read_complete_usage_bank_samples()
    if args.command == "estimate":
        estimate = estimate_usage_bank(samples, args.runs)
        _print_estimate(estimate)
        if estimate["total_usd"] > args.budget_usd:
            print(f"REFUSED: estimate exceeds --budget-usd ${args.budget_usd:.6f}")
            return 2
        return 0
    if args.command == "replay":
        replay = replay_usage_bank(samples, args.cell_limit)
        for result in replay["results"]:
            print(f"{result['cell'].provider}/{result['cell'].path}/{result['cell'].effort}: {result['status']}")
        print(f"coverage={replay['covered']}/{replay['denominator']}")
        return 0
    estimate = estimate_usage_bank(samples, args.runs)
    _print_estimate(estimate)
    execute_usage_bank_metered(samples, args.execute, args.budget_usd, args.runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
