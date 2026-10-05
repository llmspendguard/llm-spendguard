"""Subprocess for the SIGKILL crash-resume test (NOT a test). Runs durable_batch_executor's first two steps —
enqueue leased rows + batch_tracker.submit_offload (the mock batch is now recorded durably in SPENDGUARD_HOME) —
writes the row ids + batch id to a file, then SIGKILLs ITSELF *before* collecting. The parent then reconciles the
persisted rows; nothing in RAM survives, so anything the parent recovers came from the durable store.
"""
import json
import os
import signal
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "src"))
sys.path.insert(0, _HERE)

import spendguard  # noqa: E402
spendguard.require = lambda: None
import _durable_mock_provider as M  # noqa: E402
from spendguard import batch_tracker as bt, lane_queue as lq  # noqa: E402


def main():
    M.install()
    n, out = int(sys.argv[1]), sys.argv[2]
    intent, model = "acc:3-kill", "openai:gpt-5.5"
    prompts = ["do CANARY-%d" % i for i in range(n)]
    row_ids = lq._enqueue_leased(intent, prompts, sla_class="batch")
    rows = [{"id": rid, "task": p} for rid, p in zip(row_ids, prompts)]
    off = bt.submit_offload(intent, rows, model)                 # creates the (mock, durable) batch + marks rows queued_batch
    with open(out, "w") as fh:
        json.dump({"row_ids": row_ids, "batch_id": off.get("batch_id"), "error": off.get("error")}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)                         # HARD crash BEFORE collect — durable store must carry it


if __name__ == "__main__":
    main()
