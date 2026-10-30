"""OS-detached entry point for the existing governed delegate executor."""
import json
import sys
from pathlib import Path

import spendguard
spendguard.require()

from . import delegate_jobs, delegate_router, execution_journal


def run_detached_delegate_request(request_path):
    """Execute the complete persisted request without reclassifying or rerouting it."""
    with Path(request_path).open(encoding="utf-8") as request_stream:
        request = json.load(request_stream)
    job_id = request["args"][0]
    execution_id = delegate_jobs.read_delegate_job(job_id)["execution_id"]
    execution_journal.adopt_durable_execution_owner(execution_id)
    delegate_router._run_delegate_job_to_completion(*request["args"], **request["kwargs"])


if __name__ == "__main__":
    run_detached_delegate_request(sys.argv[1])
