"""Bounded termination for process groups owned by spendguard session leaders."""
import os
import signal
import time

PROCESS_GROUP_LIVENESS_PROBE_SIGNAL = 0
PROCESS_GROUP_FORCE_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def reap_process_group_bounded(process_group_id, terminate_grace_s, force_kill_grace_s,
                               poll_interval_s, warning_context, *, emit_warning,
                               force_immediately=False):
    """Terminate an owned process group and return only after it is gone or a named bound expires.

    The caller must establish ownership before passing a group id.  A failed liveness probe is treated as alive:
    inability to prove that every descendant exited must remain visible to the operator.
    """
    signals_and_bounds = (
        ((PROCESS_GROUP_FORCE_KILL_SIGNAL, force_kill_grace_s),) if force_immediately else
        ((signal.SIGTERM, terminate_grace_s), (PROCESS_GROUP_FORCE_KILL_SIGNAL, force_kill_grace_s))
    )
    for termination_signal, grace_s in signals_and_bounds:
        try:
            os.killpg(process_group_id, termination_signal)
        except ProcessLookupError:
            return True
        except Exception as error:
            emit_warning(
                f"[spendguard] {warning_context}: signalling process group {process_group_id} "
                f"with {termination_signal} failed: {error}; a descendant may still be writing")
            return False
        deadline = time.monotonic() + grace_s
        while True:
            try:
                os.killpg(process_group_id, PROCESS_GROUP_LIVENESS_PROBE_SIGNAL)
            except ProcessLookupError:
                return True
            except Exception as error:
                emit_warning(
                    f"[spendguard] {warning_context}: checking process group {process_group_id} failed: "
                    f"{error}; a descendant may still be writing")
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_interval_s, remaining))
    emit_warning(
        f"[spendguard] {warning_context}: process group {process_group_id} is still alive after the bounded "
        "SIGKILL wait; a descendant may still be writing")
    return False
