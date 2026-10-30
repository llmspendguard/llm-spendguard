"""Cooperative worker timeline for testing the real adapter deadline wrappers.

The provider marks its completion time before yielding. The real worker target
runs, while Thread.join advances only the test's clock, never the host clock.
"""
from types import SimpleNamespace


class DeadlineProviderSuspended(BaseException):
    """Yield a synthetic provider that has not returned usage."""


class AdapterDeadlineTimeline:
    def __init__(self):
        self.now = 0.0
        self.pending_until = None
        self.joins = []
        self.closed_at = []
        self.workers = []

    def provider_delay(self, duration):
        if duration:
            self.pending_until = self.now + duration
            raise DeadlineProviderSuspended()

    def client_closed(self):
        self.closed_at.append(self.now)

    def thread_facade(self, original):
        timeline = self

        class ControlledDeadlineWorker:
            def __init__(self, *, target, daemon):
                self.target = target
                self.alive = False
                assert daemon, "deadline worker must not keep the process alive"
                timeline.workers.append(self)

            def start(self):
                timeline.pending_until = None
                self.target()
                # The production worker captures the yield in its error box.
                # It must remain alive in the model until cancellation: join
                # may not mistake that cooperative yield for a completion.
                self.alive = timeline.pending_until is not None

            def join(self, timeout=None):
                timeline.joins.append(timeout)
                assert timeout is not None, "deadline worker must use a bounded join"
                if self.alive:
                    timeline.now += timeout
                    assert timeline.now < timeline.pending_until, "test provider must outlive deadline"

            def is_alive(self):
                return self.alive

        return SimpleNamespace(**{name: getattr(original, name) for name in dir(original)
                                  if not name.startswith("__") and name != "Thread"},
                               Thread=ControlledDeadlineWorker)
