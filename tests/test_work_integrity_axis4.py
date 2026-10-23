"""Axis-4 absence gate for submitted-work ownership and terminal-disposition edges.

Inventory includes low-level submit/collect clients: they return handles/results to their
owner, not an exactly-once promise for arbitrary repeated calls. Durable offloads use
batch_tracker; whole_job explicitly reports asynchronous ownership and persist gaps.
Static review is an absence gate, complemented by concurrent chaos, not a liveness proof.
"""
import ast
import collections
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORK_EDGES = {"submit_chat_tasks", "submit_message_batch", "submit_offload", "collect_chat_tasks",
              "collect_message_batch", "collect_batched", "mark_batched", "_persist_pending",
              "hold_free_recovery", "record_close", "requeue_from_batch"}
# Exact reviewed ownership transfers. Counts cover multiple branches, not just unique callees.
WORK_REVIEWED = {
    ("scripts/reliability/batch_loop_live_proof.py", "estimate_only", "submit.submit_chat_tasks"): 1,
    ("scripts/reliability/batch_loop_live_proof.py", "submit_and_track", "submit.submit_chat_tasks"): 1,
    ("scripts/reliability/batch_loop_live_proof.py", "_await_and_collect", "callio.collect_chat_tasks"): 1,
    ("src/spendguard/adapters.py", "_call_once", "lane_queue.hold_free_recovery"): 1,
    ("src/spendguard/adapters.py", "call", "_route_lq2.record_close"): 1,
    ("src/spendguard/batch_tracker.py", "submit_offload", "lane_queue.mark_batched"): 2,
    ("src/spendguard/batch_tracker.py", "_fail_over", "lane_queue.requeue_from_batch"): 1,
    ("src/spendguard/batch_tracker.py", "poll", "lane_queue.collect_batched"): 1,
    ("src/spendguard/batch_tracker.py", "submit_offload", "submit_offload"): 1,
    ("src/spendguard/batch_tracker.py", "submit_offload", "_submit.submit_message_batch"): 1,
    ("src/spendguard/batch_tracker.py", "submit_offload", "_submit.submit_chat_tasks"): 1,
    ("src/spendguard/lane_balance.py", "batch_submit", "_sm.submit_chat_tasks"): 1,
    ("src/spendguard/lane_queue.py", "hold_free_recovery", "record_close"): 1,
    ("src/spendguard/lane_queue.py", "collect_batched", "callio.collect_message_batch"): 1,
    ("src/spendguard/lane_queue.py", "collect_batched", "callio.collect_chat_tasks"): 1,
    ("src/spendguard/lane_queue.py", "_drain_locked", "batch_tracker.submit_offload"): 1,
    ("src/spendguard/storm_submit.py", "_run_durable_batch", "_bt.submit_offload"): 1,
    ("src/spendguard/storm_submit.py", "_run_durable_batch", "_lq.collect_batched"): 1,
    ("src/spendguard/submit.py", "submit_chat_tasks", "submit_chat_tasks"): 1,
    ("src/spendguard/submit.py", "submit_message_batch", "submit_message_batch"): 1,
    ("src/spendguard/whole_job.py", "_run_batch_group", "_submit.submit_chat_tasks"): 1,
    ("src/spendguard/whole_job.py", "collect_jobs", "callio.collect_chat_tasks"): 1,
    ("src/spendguard/whole_job.py", "run_jobs", "_persist_pending"): 1,
}
STATE_WRITES_REVIEWED = {
    ("src/spendguard/batch_tracker.py", "_set_status"): 1,
    ("src/spendguard/lane_queue.py", "dedup_pending_against_done"): 1,
    ("src/spendguard/lane_queue.py", "settle"): 2,
    ("src/spendguard/lane_queue.py", "lease"): 2,
    ("src/spendguard/lane_queue.py", "_reclaim_locked"): 2,
    ("src/spendguard/lane_queue.py", "mark_batched"): 1,
    ("src/spendguard/lane_queue.py", "requeue_from_batch"): 1,
}


def inventory_work_ownership_edges(sources):
    found, writes, insufficient = collections.Counter(), collections.Counter(), []
    for path, source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            insufficient.append((str(path), "unparseable"))
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = ast.unparse(node.func)
            scope = node
            while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents[scope]
            name = getattr(scope, "name", "module")
            if callee.split(".")[-1] in WORK_EDGES:
                found[(str(path), name, callee)] += 1
            if callee.split(".")[-1] in ("execute", "executemany", "executescript") and node.args:
                sql = " ".join(n.value for n in ast.walk(node.args[0])
                               if isinstance(n, ast.Constant) and isinstance(n.value, str))
                if any(table in sql for table in ("UPDATE lane_queue SET", "UPDATE batch_jobs SET")):
                    if "state" in sql or "status" in sql:
                        writes[(str(path), name)] += 1
    return found, writes, insufficient


sources = [(p.relative_to(ROOT), p.read_text()) for directory in (ROOT / "src", ROOT / "scripts")
           for p in sorted(directory.rglob("*.py"))]
edges, writes, insufficient = inventory_work_ownership_edges(sources)
unreviewed = edges - collections.Counter(WORK_REVIEWED)
removed = collections.Counter(WORK_REVIEWED) - edges
denominator = sum(edges.values())
print(f"Axis-4 work-disposition coverage: {denominator - sum(unreviewed.values())}/{denominator} edges "
      f"across {len(sources)} files; UNREVIEWED={sum(unreviewed.values())} INSUFFICIENT={len(insufficient)}")
assert not insufficient, insufficient
assert not unreviewed, unreviewed
assert not removed, removed
write_unreviewed = writes - collections.Counter(STATE_WRITES_REVIEWED)
write_removed = collections.Counter(STATE_WRITES_REVIEWED) - writes
print(f"Axis-4 durable-state coverage: {sum(writes.values()) - sum(write_unreviewed.values())}/{sum(writes.values())} "
      f"writes; UNREVIEWED={sum(write_unreviewed.values())} INSUFFICIENT={len(insufficient)}")
assert not write_unreviewed, write_unreviewed
assert not write_removed, write_removed
injected, _, _ = inventory_work_ownership_edges([
    (pathlib.Path("new_edge.py"), "def bypass():\n    submit.submit_chat_tasks(tasks, model)\n")])
assert injected - collections.Counter(WORK_REVIEWED), "new ownership edge was silently accepted"
_, new_write, _ = inventory_work_ownership_edges([
    (pathlib.Path("new_writer.py"), 'def bypass():\n    c.execute("UPDATE lane_queue SET state=done")\n')])
assert new_write - collections.Counter(STATE_WRITES_REVIEWED)
_, _, unreadable = inventory_work_ownership_edges([(pathlib.Path("broken.py"), "def (")])
assert unreadable, "unparseable input was silently accepted"

# Review commit order and evidence, rather than accepting a proof imported but never used.
queue_tree = ast.parse((ROOT / "src/spendguard/lane_queue.py").read_text())
tracker_tree = ast.parse((ROOT / "src/spendguard/batch_tracker.py").read_text())
job_tree = ast.parse((ROOT / "src/spendguard/whole_job.py").read_text())
functions = {n.name: n for tree in (queue_tree, tracker_tree, job_tree) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)}
poll = ast.unparse(functions["poll"])
assert poll.index("assert_batch_terminal_manifest(bid)") < poll.index("_set_status(bid, 'settled')")
assert "task_ids=row_ids" in ast.unparse(functions["submit_offload"])
for name in ("run_jobs", "collect_jobs", "collect_batched", "hold_free_recovery", "assert_batch_terminal_manifest"):
    assert "assert_work_partition" in ast.unparse(functions[name]), name
hold = ast.unparse(functions["hold_free_recovery"])
assert hold.index("record_close(rid, result)") < hold.index("assert_work_partition")
job = ast.unparse(functions["run_jobs"])
assert job.index("assert_work_partition") < job.index("'named_gaps': named_gaps")
print("Axis-4: submitted custom-id partition and completed-or-named-gap commit assertions reviewed")
