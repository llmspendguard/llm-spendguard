"""spend_refusal_error — the ROOT exception for every DELIBERATE spend refusal, kept in a dependency-free leaf.

`SpendGateRefused` lives here, NOT in gate.py, so the admission gate's deep import cluster does not have to be
imported merely to NAME or SUBCLASS the refusal concept. gate.py imports and re-exports it (so `gate.SpendGateRefused`
and `from ...gate import SpendGateRefused` are unchanged), and low-import modules (e.g. generate.work_integrity.
job_fingerprint) import it directly from here instead of reaching into gate — which is how this module breaks the
job_fingerprint→gate import cycle without changing any runtime behavior. The class identity is preserved: there is
exactly ONE SpendGateRefused, defined here; every subclass and every `except` site resolves to it.

This module imports only the standard library ON PURPOSE: it is a leaf (a pure import sink, in no import cycle), and
adding any spendguard import here would re-create the very cycle it exists to cut.
"""


class SpendGateRefused(RuntimeError):
    """Raised to block a submission the user/policy declined. Propagates out of the SDK call.

    This is the ROOT of every DELIBERATE gate refusal — the CONCEPT the fail-open handlers name. `_guard` and
    the pre/post-call wrappers catch `SpendGateRefused` and re-raise it, while swallowing genuine malfunctions
    (`database is locked`, a buggy register()'d fn) so a legitimate job never breaks. Subtypes of a refusal
    (e.g. bulkgate.GateBlocked, the estimate+test-first lifecycle block) therefore propagate BY CONSTRUCTION;
    a new refusal type must subclass this rather than be added to an enumerated `except (…)` list — the
    enumeration is the latent hole that once let un-estimated batches through, live 2026-08-28 (guarded by
    tests/test_deliberate_refusal_is_never_failopen.py)."""
