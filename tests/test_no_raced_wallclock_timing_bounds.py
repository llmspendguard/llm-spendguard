"""Ratchet raw wall-clock upper bounds; the reviewed allowlist may only shrink.

This is syntax parsing, not a judgement about a test's purpose. Only clock
subtractions (including assignment aliases) and elapsed/duration/took variables
are recognized. Deadline forcing exempts only the enclosing lexical scope.
"""

import ast
from pathlib import Path


TIMING_RATCHET_TEST_GLOB = "*.py"
TIMING_RATCHET_CLOCK_NAMES = frozenset({"monotonic", "perf_counter"})
TIMING_RATCHET_DELTA_NAMES = frozenset({"elapsed", "duration", "took"})
TIMING_RATCHET_DEFERRED_REASON = (
    "Known dispatch/vendor/rate-limit timing family; outside ledger-read-expiry migration."
)
# Baseline debt, never an invitation to add new exceptions: this may ONLY shrink.
TIMING_RATCHET_ALLOWLIST = {
    "test_dispatch_no_429_storm.py": TIMING_RATCHET_DEFERRED_REASON,
    "test_dispatch_tpm_bucket.py": TIMING_RATCHET_DEFERRED_REASON,
    "test_rate_limit_selfcalibrate.py": TIMING_RATCHET_DEFERRED_REASON,
    "test_governed_admission.py": TIMING_RATCHET_DEFERRED_REASON,
    "test_lane_shed_to_metered.py": TIMING_RATCHET_DEFERRED_REASON,
    "test_codex_daemon_worker_pool.py": TIMING_RATCHET_DEFERRED_REASON,
    "test_delegate_timeout_kill.py": "Provider CLI cancellation deadline; separate delegate/vendor family, not yet migrated.",
    "test_dispatch_governor_bounds_concurrency.py": "Dispatch concurrency queue refusal deadline; separate dispatch family, not yet migrated.",
    "test_lane_hedging.py": "Provider lane hedge promptness; separate dispatch/vendor family, not yet migrated.",
    "test_rate_reservation_deadline.py": "Rate-window reservation expiry; separate rate-limit family, not yet migrated.",
    "test_reliability_concurrent_probes.py": "Concurrent provider probe promptness; separate vendor family, not yet migrated.",
    "test_report_collector_deadline.py": "Remote provider report collection watchdog; separate vendor family, not yet migrated.",
    "test_storm_coalescer.py": "Dispatch coalescing and drain deadlines; separate dispatch family, not yet migrated.",
    "test_storm_coalescer_realtime_callback_deadline.py": "Dispatch realtime callback watchdog; separate dispatch/vendor family, not yet migrated.",
    "test_storm_submit.py": "Dispatch overflow collection deadline; separate dispatch family, not yet migrated.",
    "test_vendor_call.py": "Vendor transport abandonment deadline; separate vendor family, not yet migrated.",
}
TIMING_RATCHET_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _timing_ratchet_scope_nodes(scope):
    """Walk one lexical scope without mixing another test's assignments into it."""
    pending = list(scope.body)
    while pending:
        node = pending.pop()
        if isinstance(node, TIMING_RATCHET_SCOPE_TYPES):
            continue
        yield node
        pending.extend(ast.iter_child_nodes(node))


def _timing_ratchet_assignment_bindings(nodes):
    bindings = {}
    for node in sorted(nodes, key=lambda item: getattr(item, "lineno", 0)):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                bindings[target.id] = value
    return bindings


def _timing_ratchet_is_clock_call(node):
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    return (
        isinstance(function, ast.Attribute)
        and isinstance(function.value, ast.Name)
        and function.value.id == "time"
        and function.attr in TIMING_RATCHET_CLOCK_NAMES
    )


def _timing_ratchet_expression_has_clock(node, bindings, seen=frozenset()):
    if _timing_ratchet_is_clock_call(node):
        return True
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
        return _timing_ratchet_expression_has_clock(
            bindings[node.id], bindings, seen | {node.id},
        )
    return any(
        _timing_ratchet_expression_has_clock(child, bindings, seen)
        for child in ast.iter_child_nodes(node)
    )


def _timing_ratchet_is_delta(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Name):
        if node.id in bindings and node.id not in seen:
            return _timing_ratchet_is_delta(bindings[node.id], bindings, seen | {node.id})
        if TIMING_RATCHET_DELTA_NAMES.intersection(node.id.lower().split("_")):
            return True
    return (
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub)
        and _timing_ratchet_expression_has_clock(node, bindings)
    )


def _timing_ratchet_bound_has_scale(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Name):
        if "scale" in node.id.lower():
            return True
        if node.id in bindings and node.id not in seen:
            return _timing_ratchet_bound_has_scale(
                bindings[node.id], bindings, seen | {node.id},
            )
    return any(
        _timing_ratchet_bound_has_scale(child, bindings, seen)
        for child in ast.iter_child_nodes(node)
    )


def _timing_ratchet_forces_deadline(nodes, before_line):
    for node in nodes:
        if getattr(node, "lineno", before_line) >= before_line:
            continue
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if _timing_ratchet_is_clock_call(value) and any(
            isinstance(target, ast.Attribute) and target.attr == "deadline_at"
            for target in targets
        ):
            return True
    return False


def _timing_ratchet_raced_lines(tree):
    module_nodes = list(_timing_ratchet_scope_nodes(tree))
    global_bindings = _timing_ratchet_assignment_bindings(module_nodes)
    scopes = [tree] + [
        node for node in ast.walk(tree) if isinstance(node, TIMING_RATCHET_SCOPE_TYPES)
    ]
    raced_lines = set()
    # Control-flow timeout/pacing predicates are not upper-bound test assertions.
    control_predicates = {
        id(child)
        for control in ast.walk(tree) if isinstance(control, (ast.If, ast.While, ast.IfExp))
        for child in ast.walk(control.test)
    }
    control_predicates.update(
        id(child)
        for comprehension in ast.walk(tree) if isinstance(comprehension, ast.comprehension)
        for predicate in comprehension.ifs for child in ast.walk(predicate)
    )
    for scope in scopes:
        nodes = list(_timing_ratchet_scope_nodes(scope))
        bindings = dict(global_bindings)
        bindings.update(_timing_ratchet_assignment_bindings(nodes))
        for node in nodes:
            if not isinstance(node, ast.Compare) or id(node) in control_predicates:
                continue
            if _timing_ratchet_forces_deadline(nodes, node.lineno):
                continue
            left = node.left
            for operator, right in zip(node.ops, node.comparators):
                delta, bound = left, right
                if isinstance(operator, (ast.Gt, ast.GtE)):
                    delta, bound = right, left
                if isinstance(operator, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                    if _timing_ratchet_is_delta(delta, bindings) and not (
                        _timing_ratchet_bound_has_scale(bound, bindings)
                    ):
                        raced_lines.add(node.lineno)
                left = right
    return sorted(raced_lines)


def test_no_raced_wallclock_timing_bounds():
    test_directory = Path(__file__).resolve().parent
    paths = sorted(test_directory.glob(TIMING_RATCHET_TEST_GLOB))
    failures = []
    unreviewed = []
    allowlisted = 0
    clean = 0
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeError) as error:
            unreviewed.append(f"{path.name}: {error}")
            continue
        raced_lines = _timing_ratchet_raced_lines(tree)
        if path.name in TIMING_RATCHET_ALLOWLIST:
            assert TIMING_RATCHET_ALLOWLIST[path.name].strip(), path.name
            allowlisted += 1
        elif raced_lines:
            failures.extend(f"{path.name}:{line}" for line in raced_lines)
        else:
            clean += 1
    coverage = (
        f"Timing ratchet: {len(paths)} files scanned; {clean} clean; "
        f"{allowlisted} allow-listed; {len(unreviewed)} unreviewed; "
        f"{len(failures)} unlisted raced bounds"
    )
    print(coverage)
    assert not unreviewed, coverage + "\n" + "\n".join(unreviewed)
    assert not failures, coverage + "\n" + "\n".join(failures)


if __name__ == "__main__":
    test_no_raced_wallclock_timing_bounds()
