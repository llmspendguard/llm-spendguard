"""Import sink for work_integrity gate events without recreating the generate→admission cycle.

Gate wires its real ``_log`` function here when gate is imported.
"""

_gate_event_log_sink = None


def set_gate_event_sink(sink):
    """Injected once by gate at its import with gate._log."""
    global _gate_event_log_sink
    _gate_event_log_sink = sink


def emit_gate_event(record):
    if _gate_event_log_sink is None:
        raise RuntimeError("import spendguard before emitting a gate event so gate can wire its event sink")
    return _gate_event_log_sink(record)
