"""Background delegate admission refuses without reading process-global stdin."""
import builtins
import os
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-background-admission-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from spendguard.analyze.measurement import guard  # noqa: E402
from spendguard.manage.admission import gate  # noqa: E402
from spendguard.manage.token_operations import execution_journal  # noqa: E402


background_prompt_calls = []
background_worker_errors = []
background_dispatch_identifier = uuid.uuid4().hex
BACKGROUND_ADMISSION_JOIN_TIMEOUT_SECONDS = 2.0


class InteractiveProcessStdin:
    def isatty(self):
        return True


def reject_background_input(*args, **kwargs):
    background_prompt_calls.append((args, kwargs))
    raise AssertionError("background delegate attempted to read stdin")


def exercise_background_admission():
    try:
        with execution_journal.durable_execution_scope(background_dispatch_identifier), \
                patch.object(sys, "stdin", InteractiveProcessStdin()), \
                patch.object(builtins, "input", reject_background_input), \
                patch.object(gate, "_cap", return_value=0.0), \
                patch.object(gate, "_allow", return_value=False), \
                patch.object(gate, "_refuse_metered_active", return_value=False), \
                patch.object(gate, "_calibrate_est", return_value=None), \
                patch.object(gate, "_log"), \
                patch.object(guard, "record_saving"):
            try:
                gate._decide({"provider": "test-provider", "model": "test-model", "requests": 1,
                              "in_tok": 1, "out_tok": 1, "out_basis": "measured", "cost": 1.0})
            except gate.SpendGateRefused:
                return
            raise AssertionError("background admission did not fail closed")
    except Exception as error:
        background_worker_errors.append(error)


background_admission_worker = threading.Thread(target=exercise_background_admission)
background_admission_worker.start()
background_admission_worker.join(timeout=BACKGROUND_ADMISSION_JOIN_TIMEOUT_SECONDS)

assert not background_admission_worker.is_alive(), "background admission worker remained blocked"
assert not background_worker_errors, background_worker_errors
assert not background_prompt_calls, background_prompt_calls
print("1 passed: background delegate admission refuses without reading interactive process stdin")
