"""Eagerly import the execution home modules so their accessor-resolver forwarders register at startup.

This is the out-of-SCC trigger for the execution domain seam (ctx#34). The seam inverts every cross-domain
inbound import into `generate.execution.*` through the dep-free `execution_accessors` leaf: each home module
registers its forwarder (`execution_accessors.set_<sym>_resolver(...)`) at its own module end, which runs only
when the home is imported. The execution homes are lazy-by-design, so some module has to import them at startup
or the first external `resolve_<sym>` call raises "resolver not wired".

Why a separate module and not `spendguard/__init__` or the leaf itself:
  - The `spendguard` package root is a member of the import SCC (the whole package does `import spendguard`), so
    importing the homes from there would add a cyclic edge per home.
  - The leaf must stay dependency-free (it imports nothing from spendguard) to remain outside the SCC, so it
    cannot import the homes either.
This module is imported by nothing inside the SCC (only `spendguard.__init__` calls it), so it stays out of the
SCC, and both `spendguard -> bootstrap` and `bootstrap -> home` are cross-SCC (non-cyclic) edges — the same
property that keeps consumer->leaf edges out of the cycle.
"""


def wire_execution_accessor_resolvers() -> None:
    """Import every execution home module, running each one's module-end resolver registration.

    Imported purely for the registration side effect; the names are not used here. Cheap (~20ms total) and
    free of import-time side effects.
    """
    from . import (  # noqa: F401  (imported for their module-end resolver wiring, not for use here)
        adapters,
        antigravity_exec,
        batch_tracker,
        codex_exec,
        price_identity_judge,
        submit,
        vendor_call,
    )
