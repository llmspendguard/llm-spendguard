"""One structural resilience rule for lane fans and provider batch submissions."""
import os
import sys

from . import config


class BulkResilienceRefused(Exception):
    """A large submission lacks both real chunks and a durable checkpoint."""

    def __init__(self, message, *, n_units, threshold, chunked, checkpointed, where):
        super().__init__(message)
        self.n_units = n_units
        self.threshold = threshold
        self.chunked = chunked
        self.checkpointed = checkpointed
        self.where = where


def _resilience_min_units(default=1000):
    """Configured bulk.resilience_min_units; zero disables the structural gate."""
    try:
        v = config._cfg_get("bulk", "resilience_min_units", None)
        v = os.environ.get("SPENDGUARD_BULK_RESILIENCE_MIN_UNITS", v)
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def require_resilient(n_units, *, chunked, checkpointed, force, where, chunk_size=None):
    """Refuse a large indivisible run. Unknown shape fails closed; force is logged."""
    minimum = _resilience_min_units()
    if force:
        if where != "lane":
            print(f"[spendguard] resilience force=True override at {where}: {n_units} requests", file=sys.stderr)
        return
    if minimum <= 0:
        return
    if n_units is None or chunked is None or checkpointed is None:
        raise BulkResilienceRefused(
            f"REFUSED: resilience state unknown at {where}; pass force=True to override and own the risk.",
            n_units=n_units, threshold=minimum, chunked=chunked, checkpointed=checkpointed, where=where)
    try:
        count = int(n_units)
    except (TypeError, ValueError) as exc:
        raise BulkResilienceRefused(
            f"REFUSED: resilience count unknown at {where}; pass force=True to override and own the risk.",
            n_units=n_units, threshold=minimum, chunked=chunked, checkpointed=checkpointed, where=where) from exc
    if count < minimum or (chunked and checkpointed):
        return
    if where == "lane":
        gaps = []
        if not checkpointed:
            gaps.append("no checkpoint — a crash or transient stall loses the whole run (pass checkpoint=<jsonl>)")
        if not chunked:
            gaps.append(f"not chunked — chunk_size={chunk_size} >= {count} units, so one bad unit or a "
                        "momentary full-lane pass can wedge everything (lower chunk_size)")
        message = (f"REFUSED: {count} units (>= bulk.resilience_min_units={minimum}) as a single shot without "
                   "resilience — " + "; ".join(gaps) + ". This is the chunk-never-single-shot rule: a large job "
                   "must checkpoint and chunk so a transient no-progress pass cannot kill it. Fix the above, or pass "
                   "force=True to override and own the risk.")
    else:
        # The provider door sees REQUESTS per batch. Logical units packed into requests are upstream's concern.
        message = (f"REFUSED: {count} requests (>= bulk.resilience_min_units={minimum}) at {where}: "
                   "a provider batch settles all-or-nothing, so a straggler strands every request. "
                   "Lower shard_size to create multiple batches and route through the checkpointed offload path "
                   "that durably marks each shard. Packed logical units inside requests are invisible at this door. "
                   "Pass force=True to override and own the risk.")
    raise BulkResilienceRefused(message, n_units=count, threshold=minimum,
                                chunked=chunked, checkpointed=checkpointed, where=where)
