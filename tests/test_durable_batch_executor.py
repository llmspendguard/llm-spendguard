"""3 — durable, crash-resumable, EXACTLY-ONCE batch (storm_submit.durable_batch_executor on lane_queue +
batch_tracker.submit_offload). Two proofs, offline ($0, file-backed mock Batch API):
  A. happy path — the executor submits once, collects by id, returns {custom_id: result}; the rows settle 'done'.
  B. SIGKILL crash-resume (the un-fakeable G1 test) — a subprocess enqueues + offloads then kill -9's ITSELF before
     collecting; the parent reconciles the PERSISTED rows (collect_batched) and recovers every result, with the batch
     created EXACTLY ONCE across the crash (no double-spend). Graceful shutdown would prove nothing; this is kill -9.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="sg-3-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import _durable_mock_provider as M  # noqa: E402
from spendguard import lane_queue as lq, storm_submit  # noqa: E402

fails = []


def verify_condition(name, cond, extra=""):
    print(("  [OK]   " if cond else "  [RED]  ") + name + (("  — " + extra) if extra and not cond else ""))
    if not cond:
        fails.append(name)


# ── A — happy path: durable_batch_executor end-to-end (enqueue -> offload -> collect -> {cid: result}) ────────
M.install()
ex = storm_submit.durable_batch_executor("openai:gpt-5.5", "acc:3", poll_interval_s=0.01, max_poll_s=5)
res = ex([("7", "do CANARY-7"), ("9", "do CANARY-9")])
verify_condition("A: every item resolved by id with its canary (collected from the batch)",
                 res.get("7", {}).get("text") == "ok CANARY-7" and res.get("9", {}).get("text") == "ok CANARY-9",
                 extra="res=%s" % {k: res.get(k, {}).get("text") for k in ("7", "9")})
verify_condition("A: the batch was created exactly once", M.creates() == 1, extra="creates=%d" % M.creates())

# ── B — SIGKILL crash-resume: worker enqueues+offloads then kill -9; parent reconciles the persisted rows ─────
N = 6
out = os.path.join(os.environ["SPENDGUARD_HOME"], "worker_out.json")
env = dict(os.environ)
creates_before = M.creates()                                     # 1 from (A)
proc = subprocess.run([sys.executable, os.path.join(_HERE, "_durable_kill_worker.py"), str(N), out], env=env)
killed = proc.returncode in (-9, 137)                            # SIGKILL
worker = json.load(open(out)) if os.path.exists(out) else {}
row_ids = worker.get("row_ids") or []
verify_condition("B: the worker hard-crashed (kill -9) AFTER durably enqueuing+offloading",
                 killed and len(row_ids) == N and worker.get("batch_id"),
                 extra="rc=%s row_ids=%d batch_id=%s" % (proc.returncode, len(row_ids), worker.get("batch_id")))
verify_condition("B: the worker created exactly ONE new batch (its offload)", M.creates() == creates_before + 1,
                 extra="creates=%d (want %d)" % (M.creates(), creates_before + 1))

# parent = the 'restart': reconcile the persisted queued_batch rows (collect_batched), never re-submitting
creates_at_recover = M.creates()
t0 = time.monotonic()
while time.monotonic() - t0 < 10:
    lq.collect_batched(model="openai:gpt-5.5")
    rr = lq.row_results(row_ids)
    if row_ids and all(rr.get(rid, {}).get("state") == "done" for rid in row_ids):
        break
    time.sleep(0.05)
rr = lq.row_results(row_ids)
recovered = sum(1 for rid in row_ids if rr.get(rid, {}).get("state") == "done" and rr[rid]["result"].get("text"))
verify_condition("B: ALL N requests recovered from the durable store after the crash (no dropped request)",
                 recovered == N, extra="recovered=%d/%d states=%s" % (recovered, N, {rid: rr.get(rid, {}).get("state") for rid in row_ids}))
verify_condition("B: EXACTLY-ONCE — no second batch created during recovery (no double-spend)",
                 M.creates() == creates_at_recover, extra="creates now=%d (was %d at recovery start)" % (M.creates(), creates_at_recover))

print("\n%s: test_durable_batch_executor — %d checks RED" % ("ALL GREEN" if not fails else "RED", len(fails)))
sys.exit(1 if fails else 0)
