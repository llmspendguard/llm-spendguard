"""Verify the ledger-write decorator providers are wired by the package import.

The two decorators live in a dependency-free leaf (store.money_ledger.ledger_write_decorators) and reach the real
write machinery through providers injected by ledger_writer at import. This test asserts the wiring is in place on
`import spendguard`, that the decorators are the same objects re-exported from ledger_writer (backward-compat), that
serialize_ledger_function routes its call through the injected run-ledger-write provider, and that an unset provider
fails loud. The end-to-end leg uses a passthrough stub provider so it exercises the decorator's resolve→invoke path
without touching the real ledger/home machinery.
"""
import os
import sys

os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import spendguard  # noqa: E402,F401
from spendguard.store.money_ledger import ledger_write_decorators  # noqa: E402
from spendguard.store.money_ledger.ledger_write_decorators import (  # noqa: E402
    best_effort_ledger_function as leaf_best_effort_ledger_function,
    serialize_ledger_function as leaf_serialize_ledger_function,
)
from spendguard.store.money_ledger.ledger_writer import (  # noqa: E402
    best_effort_ledger_function as writer_best_effort_ledger_function,
    serialize_ledger_function as writer_serialize_ledger_function,
)


def main():
    # All three providers are wired by `import spendguard` (ledger_writer is the wirer).
    assert ledger_write_decorators._run_ledger_write_provider is not None
    assert ledger_write_decorators._decorator_warn_once_provider is not None
    assert ledger_write_decorators._decorator_is_deliberate_stop_provider is not None

    # The decorators are re-exported from ledger_writer and are the SAME objects as the leaf's.
    assert leaf_serialize_ledger_function is writer_serialize_ledger_function
    assert leaf_best_effort_ledger_function is writer_best_effort_ledger_function

    # End-to-end wiring WITHOUT the real ledger/home machinery: a passthrough stub runs the thunk, exactly as
    # run_ledger_write would for a trivial write, so the decorator's resolve→invoke path is exercised in isolation.
    saved_run_provider = ledger_write_decorators._run_ledger_write_provider
    try:
        ledger_write_decorators.set_run_ledger_write_provider(lambda thunk: thunk())
        expected_result = object()

        @leaf_serialize_ledger_function
        def return_expected_ledger_result():
            return expected_result

        assert return_expected_ledger_result() is expected_result
    finally:
        ledger_write_decorators.set_run_ledger_write_provider(saved_run_provider)

    # An unset provider fails loud rather than silently skipping the write.
    saved_run_provider = ledger_write_decorators._run_ledger_write_provider
    try:
        ledger_write_decorators.set_run_ledger_write_provider(None)
        try:
            ledger_write_decorators._resolve_run_ledger_write()
        except RuntimeError as provider_error:
            assert "provider is not set" in str(provider_error)
        else:
            raise AssertionError("unset run-ledger-write provider did not fail loud")
    finally:
        ledger_write_decorators.set_run_ledger_write_provider(saved_run_provider)


if __name__ == "__main__":
    main()
