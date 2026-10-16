"""F3 guard placeholder: cross-process reuse needs an IPC transport, which the stdio daemon does not expose."""
import pytest


@pytest.mark.skip(reason=(
    "F3 deferred: codex app-server is owned through private parent stdin/stdout pipes; the existing host lock can "
    "dedupe spawning but another process cannot attach without a new socket/IPC broker"
))
def test_two_hosts_attempts_share_one_warm_daemon_without_signals():
    """Enable when a contained attach protocol exists; it must assert one spawn and no signal/kill calls."""
    raise AssertionError("deferred until the warm daemon exposes host-wide IPC")


if __name__ == "__main__":
    print("SKIP test_codex_daemon_host_singleton_deferred: private stdio has no cross-process attach transport")
