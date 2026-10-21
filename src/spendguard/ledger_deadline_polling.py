"""Measured SQLite polling cadence shared by bounded ledger read owners."""

# Begin finely without assuming a machine's VM throughput. Thereafter target one
# check per subdivision of the original budget (not a geometric tail of checks
# as the remaining budget approaches zero). No seconds/opcode rate is assumed.
LEDGER_DEADLINE_INITIAL_VM_INTERVAL = 100
LEDGER_DEADLINE_SUBDIVISIONS = 64
LEDGER_DEADLINE_INTERVAL_GROWTH_FACTOR = 2
LEDGER_DEADLINE_MINIMUM_VM_INTERVAL = 1
# sqlite3's progress-handler instruction argument is a signed C int.
LEDGER_DEADLINE_SQLITE_INTERVAL_MAX = (1 << 31) - 1


class LedgerDeadlinePolling:
    """Learn elapsed time per interval; shrink immediately and grow cautiously.

    Owners must check the absolute deadline before advancing and retain their
    typed timeout handling. SQLite cannot interrupt a single slow VM instruction
    or user function; this controller bounds polling work, not native op latency.
    """

    def __init__(self, deadline_at, started_at):
        self.interval = LEDGER_DEADLINE_INITIAL_VM_INTERVAL
        self.sampled_at = started_at
        self.target_poll_seconds = (deadline_at - started_at) / LEDGER_DEADLINE_SUBDIVISIONS

    def advance_ledger_deadline_poll(self, now):
        measured_seconds = now - self.sampled_at
        self.sampled_at = now
        if measured_seconds > 0:
            measured_interval = self.interval * self.target_poll_seconds / measured_seconds
            self.interval = max(LEDGER_DEADLINE_MINIMUM_VM_INTERVAL, int(min(
                measured_interval,
                self.interval * LEDGER_DEADLINE_INTERVAL_GROWTH_FACTOR,
                LEDGER_DEADLINE_SQLITE_INTERVAL_MAX,
            )))
        return self.interval
