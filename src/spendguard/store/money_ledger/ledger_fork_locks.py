"""Create process-owned ledger synchronization with its child reset attached."""
import os


def new_fork_reset_ledger_state(module_namespace, state_name, state_factory):
    """Replace the owning global in a child without touching inherited locks.

    Registration accompanies construction, so adding a ledger lock through this
    factory cannot omit its fork reset. Factories may also rebuild state owners
    whose worker references and lock-protected data must start fresh together.
    """
    initial_state = state_factory()

    def replace_registered_ledger_state_in_child():
        module_namespace[state_name] = state_factory()

    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=replace_registered_ledger_state_in_child)
    return initial_state
