"""Axis-4: inventory every paid-routing edge across package and scripts, fail closed on new shapes.

The review denominator is routing edges, not all calls (embeddings, explicit paid probes and
capability-only API work are distinct). Dynamic **kwargs are exercised by chaos, not claimed
statically proven. Changed forwarding expressions or new edges require review here.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
KEYS = {"metered_only", "_skip_lane", "shed"}
# Exact reviewed edges: file, enclosing function, callee, paid-routing expressions.
REVIEWED = {
    ("reliability.py", "_probe_metered_one", "adapters.call", "metered_only=True"): "explicit paid health probe",
    ("storm_submit.py", "_run_governed_realtime", "adapters.call", "metered_only=True"): "explicit paid storm",
    ("vendor_call.py", "call", "dispatch.admit", "shed=False"): "no saturation-to-paid bypass",
    ("vendor_call.py", "call", "_attempt", "metered_only=_shed_metered"): "admission cannot shed",
    ("vendor_call.py", "_run", "adapters.call", "metered_only=metered_only"): "caller-paid flag forwarded",
    ("vendor_call.py", "discover_efforts", "adapters.call", "metered_only=True"): "explicit paid measurement",
    ("lane_balance.py", "_run_task_on_api", "adapters.call", "metered_only=metered_only"): "pinned opt-in paid fan",
    ("adapters.py", "call", "dispatch.admit", "shed=False"): "no saturation-to-paid bypass",
    ("adapters.py", "call", "call", "metered_only=metered_only"): "retry preserves explicit caller choice",
    ("adapters.py", "_call_once", "_call_once", "_skip_lane=True"): "recovery-proof guarded transition",
    ("adapters.py", "call", "_call_guarded", "metered_only=metered_only"): "explicit paid choice forwarded",
    ("adapters.py", "call", "_call_once", "_skip_lane=metered_only"): "explicit paid choice forwarded",
    ("thinking_budget_matrix.py", "_safe_call", "adapters.call", "metered_only=True"): "paid measurement",
    ("metered_intent_repro.py", "main", "lane_balance.bulk_delegate", "metered_only=True"): "paid opt-in repro",
    ("consumer_provider_smoke.py", "_run_strict", "adapters.call", "metered_only=True"): "paid capability probe",
    ("consumer_provider_smoke.py", "_run_text", "adapters.call", "metered_only=metered_only"): "smoke caller choice",
    ("consumer_provider_smoke.py", "measure", "adapters.call", "metered_only=True"): "paid measurement",
    ("consumer_provider_smoke.py", "run_live", "_run_text", "metered_only=True"): "explicit paid live opt-in",
    ("consumer_provider_smoke.py", "run_live", "_run_text", "metered_only=False"): "lane fidelity probe",
    ("live_validate_no_429_storm.py", "_run_governed_door", "lane_balance.bulk_delegate", "metered_only=True"): "paid storm test",
    ("measure_value_judge_run.py", "_caged_call", "adapters.call", "metered_only=True"): "explicit paid judge",
    ("panel_review.py", "run_panel", "lane_balance.bulk_delegate", "metered_only=True"): "explicit pinned metered conformance",
}


def inventory_lane_paid_edges(sources):
    found, insufficient = [], []
    for path, source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            insufficient.append((str(path), "unparseable"))
            continue
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            flags = [k for k in node.keywords if k.arg in KEYS]
            if not flags:
                continue
            scope = node
            while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents[scope]
            key = (path.name, getattr(scope, "name", "module"), ast.unparse(node.func),
                   ";".join(k.arg + "=" + ast.unparse(k.value) for k in flags))
            found.append((key, str(path), node.lineno))
    return found, insufficient


sources = [(p, p.read_text()) for directory in (ROOT / "src", ROOT / "scripts")
           for p in sorted(directory.rglob("*.py"))]
edges, insufficient = inventory_lane_paid_edges(sources)
unreviewed = [e for e in edges if e[0] not in REVIEWED]
reviewed = len(edges) - len(unreviewed)
print(f"Axis-4 paid-routing coverage: {reviewed}/{len(edges)} edges across {len(sources)} files; "
      f"UNREVIEWED={len(unreviewed)} INSUFFICIENT={len(insufficient)}")
assert not insufficient, insufficient
assert not unreviewed, unreviewed
assert len(edges) == len(REVIEWED), "reviewed edge removed/duplicated; re-review the call graph"

# Inspect the actual ladder and the two dispatch entrances, not a one-file keyword claim.
adapter_tree = ast.parse((ROOT / "src/spendguard/adapters.py").read_text())
once = next(n for n in ast.walk(adapter_tree) if isinstance(n, ast.FunctionDef) and n.name == "_call_once")
names = [(ast.unparse(n.func), n.lineno) for n in ast.walk(once) if isinstance(n, ast.Call)]
line = dict(names)
assert line["lane_recovery.retry_free_lane"] < line["_shed_free_recovery"]
assert line["lane_queue.hold_free_recovery"] < line["_recovery.authorize_metered_recovery"] < line["_call_once"]
assert "_recovery.assert_metered_recovery" in ast.unparse(once)
assert "_recovery_proof.assert_metered_recovery" in ast.unparse(once)

# Anti-vacuity: a new bypass anywhere in the scan is unreviewed, never silently clean.
injected, _ = inventory_lane_paid_edges([(pathlib.Path("new_hook.py"),
                                         "def hook():\n    adapters.call('m', 'p', metered_only=True)\n")])
assert injected and injected[0][0] not in REVIEWED

# Also inventory direct SDK sends: these are explicit transport/capability experiments rather than
# lane-eligible fallovers. The shared adapters' aliased create/stream sends are covered as sinks too.
SDK_REVIEWED = {
    ("submit.py", "_preflight_first_request", "client.chat.completions.create"): 1,
    ("submit.py", "_preflight_first_request", "client.messages.create"): 1,
    ("experiment.py", "_call", "anthropic.Anthropic(api_key=key).messages.create"): 1,
    ("experiment.py", "_call", "oc.chat.completions.create"): 2,
    ("refresh.py", "_llm_lookup", "client.messages.create"): 1,  # web-search tool, absent on prompt lanes
    ("cachetest.py", "_run_calls", "c.messages.create"): 1,
    ("cachetest.py", "_run_calls", "c.chat.completions.create"): 1,
    ("streaming_transport_probe.py", "one", "c.chat.completions.create"): 2,
    ("concurrency_probe.py", "_raw_one", "_client.chat.completions.create"): 1,
    ("adapters.py", "_astream", "c.messages.stream"): 1,
    ("adapters.py", "_anth_msg", "c.messages.stream"): 1,
    ("adapters.py", "_bounded_create", "_raw_create"): 1,
    ("adapters.py", "_worker", "_raw_create"): 1,
}
sdk_found = {}
for path, source in sources:
    tree = ast.parse(source)
    parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        if callee != "_raw_create" and not any(s in callee for s in (
                ".chat.completions.create", ".messages.create", ".responses.create", ".messages.stream")):
            continue
        scope = node
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        key = (path.name, getattr(scope, "name", "module"), callee)
        sdk_found[key] = sdk_found.get(key, 0) + 1
assert sdk_found == SDK_REVIEWED, {"UNREVIEWED": set(sdk_found) - set(SDK_REVIEWED), "actual": sdk_found}
print(f"Axis-4 SDK sink coverage: {sum(sdk_found.values())}/{sum(SDK_REVIEWED.values())}; UNREVIEWED=0 INSUFFICIENT=0")
print("GREEN Axis-4: reviewed paid transitions; unknown edges fail closed")
