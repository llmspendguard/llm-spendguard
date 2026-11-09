"""spendguard — provider-agnostic LLM cost discipline.

A pre-submit cost GATE (overlay on the OpenAI/Anthropic SDKs), a per-run ESTIMATOR,
token-accurate RECONCILERS, and a daily/weekly/monthly spend REPORT — all priced from
one canonical, verifiable price table.

    import spendguard; spendguard.install(cap=75)   # gate every batch in this process
"""
from .domain_legacy_path_shim import install_packaged_domain_legacy_path_finder

install_packaged_domain_legacy_path_finder()

# `estimate` is EXPORTED AS estimate_cost, deliberately. This package contains a MODULE named
# spendguard.estimate (the measure-a-sample-then-project path), and re-exporting pricing's function under
# the same bare name shadowed it — so `from spendguard import estimate` handed you a function, and which
# of the two you got depended on import ORDER. The consequence was not theoretical: the sanctioned
# measure-then-project path was unreachable by its obvious import, which is part of why a cost was quoted
# from invented token counts instead. A module and a function cannot share a name in one namespace; the
# function is the one that moves, since the module's name says exactly what it is.
_CANONICAL_CLI_MODULE = "spendguard.interface.cli"
_CANONICAL_CLI_PACKAGE = "spendguard.interface"
_LEGACY_CLI_MODULE = "spendguard.cli"


def _cli_submodule_is_being_imported():
    """True only while importlib is resolving a supported CLI module or ``spendguard.__main__``.

    A package initializer runs before the requested submodule, so the CLI cannot set an environment flag before
    this code executes.  Importlib does, however, carry the fully-qualified requested module in its loader frame.
    Ordinary ``import spendguard`` has no such frame and keeps the historical eager auto-gate behaviour.
    """
    import sys
    frame = sys._getframe()
    while frame is not None:
        requested = frame.f_locals.get("name")
        if requested in (_CANONICAL_CLI_MODULE, _CANONICAL_CLI_PACKAGE,
                         _LEGACY_CLI_MODULE, "spendguard.__main__"):
            return True
        frame = frame.f_back
    return False


_CLI_BOOTSTRAP_IS_LAZY = _cli_submodule_is_being_imported()


def _load_public_api_and_install_gate():
    """Load the public API and preserve import-time auto-gating after CLI help has been ruled out."""
    global batch_cost, realtime_cost, estimate_cost, price, normalize
    global PRICING, PRICING_VERIFIED, PRICING_SOURCE
    global install, require, register, SpendGateRefused, gate_external, refuse_metered, MeteredCallRefused, _record_rt
    global on_event, context, set_context, feedback
    global estimate_job, test_job, gated_batch, check_bulk, check_realtime, check_compute
    global record_estimate, record_tested, note_response, maxtokens, is_truncated, GateBlocked
    global ask, AskResult, BudgetRefused, ItemLedger
    global cancel_on_zero_billable, relative_escalation_gate
    global compute_job_fingerprint, guard_resume, compare_fingerprints, read_fingerprint
    global write_fingerprint, JobPlanDrift, JobMethodUndecided
    global _install_litellm, _install_bedrock, _install_vertex
    global _set_litellm_realtime_recorder, _set_bedrock_realtime_recorder, _set_vertex_realtime_recorder

    if "install" in globals():
        return
    # These domain homes are intentionally eager: importing them preserves the root's historical public
    # bootstrap order and registers their dependency-inverted accessor forwarders before consumers import.
    from .store.spend_search import ledger_read as _spend_search_ledger_read  # noqa: F401
    from .store.spend_search import ledger_request_cursor as _spend_search_ledger_request_cursor  # noqa: F401
    from .manage.admission import budget as _admission_budget  # noqa: F401
    from .manage.admission import bulkgate as _admission_bulkgate  # noqa: F401
    from .manage.admission import gate as _admission_gate  # noqa: F401
    from .generate.spend_capture import realtime_spend_recorder as _realtime_spend_recorder  # noqa: F401
    from .platform import fleet as _platform_fleet  # noqa: F401
    from .platform import lanes as _platform_lanes  # noqa: F401
    from .pricing import (batch_cost, realtime_cost, estimate as estimate_cost, price, normalize,
                          PRICING, PRICING_VERIFIED, PRICING_SOURCE)
    from spendguard.manage.admission.admission_accessors import resolve_install as install, resolve_require as require, resolve_register as register, resolve_gate_external as gate_external, resolve_refuse_metered as refuse_metered, resolve__record_rt as _record_rt
    from spendguard.manage.admission.spend_refusal_error import SpendGateRefused, MeteredCallRefused
    from .emit import on_event
    from .calls import context, set_context, feedback
    from spendguard.manage.admission.admission_accessors import resolve_estimate_job as estimate_job, resolve_test_job as test_job, resolve_gated_batch as gated_batch, resolve_check_bulk as check_bulk, resolve_check_realtime as check_realtime, resolve_check_compute as check_compute, resolve_record_estimate as record_estimate, resolve_record_tested as record_tested, resolve_note_response as note_response, resolve_maxtokens as maxtokens, resolve_is_truncated as is_truncated
    from spendguard.manage.admission.spend_refusal_error import GateBlocked
    from .crossllm import ask, AskResult, BudgetRefused
    from .item_ledger import ItemLedger
    from .escalation_ladder import cancel_on_zero_billable, relative_escalation_gate
    from .job_fingerprint import (compute_job_fingerprint, guard_resume, compare_fingerprints, read_fingerprint,
                                  write_fingerprint, JobPlanDrift, JobMethodUndecided)
    from .litellm_adapter import (install as _install_litellm,
                                  set_litellm_realtime_recorder as _set_litellm_realtime_recorder)
    from .bedrock_adapter import (install as _install_bedrock,
                                  set_bedrock_realtime_recorder as _set_bedrock_realtime_recorder)
    from .vertex_adapter import (install as _install_vertex,
                                 set_vertex_realtime_recorder as _set_vertex_realtime_recorder)
    _set_litellm_realtime_recorder(_record_rt)
    _set_vertex_realtime_recorder(_record_rt)
    _set_bedrock_realtime_recorder(_record_rt)
    _auto_install()
    from .capability_surface_generation import document_existing_capability_exports
    document_existing_capability_exports(globals())
    _wire_execution_accessor_resolvers()
    _wire_admission_accessor_resolvers()
    _wire_platform_accessor_resolvers()
    _wire_reference_data_accessor_resolvers()
    _wire_egress_accessor_resolvers()
    _wire_call_corpus_accessor_resolvers()
    _wire_spend_reporting_accessor_resolvers()
    _wire_token_operations_accessor_resolvers()
    _wire_routing_accessor_resolvers()
    _wire_spend_capture_accessor_resolvers()
    _wire_measurement_accessor_resolvers()
    _wire_reconciliation_accessor_resolvers()


def _wire_execution_accessor_resolvers():
    """Trigger the execution domain seam's resolver wiring at startup, through an out-of-SCC bootstrap.

    The execution seam (ctx#34) inverts every cross-domain inbound import into `generate.execution.*`
    through the dep-free `generate.execution.execution_accessors` leaf: external callers read each fronted
    symbol via a leaf resolver (`resolve_<sym>`), and each home module registers its forwarder
    (`execution_accessors.set_<sym>_resolver(...)`) at its own module end. Those forwarders only run when the
    home module is imported, and the execution homes are otherwise lazy-by-design, so without a startup
    trigger every `resolve_<sym>` would raise "resolver not wired" the first time an external caller used it.

    The trigger must NOT live here: the `spendguard` package root is itself a member of the import SCC (the
    whole package does `import spendguard`), so importing the homes directly from this module would add a
    cyclic edge per home. Instead the home imports live in `generate.execution.execution_resolver_bootstrap`,
    a module that nothing in the SCC imports — so `spendguard -> bootstrap` and `bootstrap -> home` are both
    cross-SCC (non-cyclic), exactly as the dep-free leaf keeps consumer->leaf edges out of the cycle.
    Imports are cheap (~20ms total) and side-effect-free; a failure here must never break `import spendguard`.
    """
    try:
        from .generate.execution import execution_resolver_bootstrap
        execution_resolver_bootstrap.wire_execution_accessor_resolvers()
    except Exception:
        # Fail-open: a problem wiring the execution accessors must not stop the guard importing. The
        # resolvers then raise their own clear "resolver not wired" error at the point of use, which is a
        # louder, more localized signal than a failed `import spendguard`.
        pass


def _wire_admission_accessor_resolvers():
    """Trigger lazy admission-home resolver registration through its bootstrap."""
    try:
        from .manage.admission import admission_resolver_bootstrap
        admission_resolver_bootstrap.wire_admission_accessor_resolvers()
    except Exception:
        pass


def _wire_platform_accessor_resolvers():
    """Trigger lazy platform-home resolver registration through its bootstrap."""
    try:
        from .platform import platform_resolver_bootstrap
        platform_resolver_bootstrap.wire_platform_accessor_resolvers()
    except Exception:
        pass


def _wire_reference_data_accessor_resolvers():
    """Trigger lazy reference-data-home resolver registration through its bootstrap."""
    try:
        from .store.reference_data import reference_data_resolver_bootstrap
        reference_data_resolver_bootstrap.wire_reference_data_accessor_resolvers()
    except Exception:
        pass


def _wire_egress_accessor_resolvers():
    """Trigger lazy federation-egress resolver registration through its bootstrap."""
    try:
        from .egress import egress_resolver_bootstrap
        egress_resolver_bootstrap.wire_egress_accessor_resolvers()
    except Exception:
        pass


def _wire_call_corpus_accessor_resolvers():
    """Trigger lazy callio resolver registration through its out-of-SCC bootstrap."""
    try:
        from .store.call_corpus import call_corpus_resolver_bootstrap
        call_corpus_resolver_bootstrap.wire_call_corpus_accessor_resolvers()
    except Exception:
        pass


def _wire_spend_reporting_accessor_resolvers():
    """Trigger lazy spend-reporting resolver registration through its bootstrap."""
    try:
        from .analyze.spend_reporting import spend_reporting_resolver_bootstrap
        spend_reporting_resolver_bootstrap.wire_spend_reporting_accessor_resolvers()
    except Exception:
        pass


def _wire_token_operations_accessor_resolvers():
    """Register lazy token-operations home loading without triggering import-time daemon side effects."""
    try:
        from .manage.token_operations import token_operations_resolver_bootstrap
        token_operations_resolver_bootstrap.wire_token_operations_accessor_resolvers()
    except Exception:
        pass


def _wire_routing_accessor_resolvers():
    """Register lazy routing home loading without importing the routing hub at startup."""
    try:
        from .manage.routing import routing_resolver_bootstrap
        routing_resolver_bootstrap.wire_routing_accessor_resolvers()
    except Exception:
        pass


def _wire_spend_capture_accessor_resolvers():
    """Register lazy spend-capture home loading without importing the spend-capture hub at startup."""
    try:
        from .generate.spend_capture import spend_capture_resolver_bootstrap
        spend_capture_resolver_bootstrap.wire_spend_capture_accessor_resolvers()
    except Exception:
        pass


def _wire_measurement_accessor_resolvers():
    """Register lazy measurement home loading without importing the measurement hub at startup."""
    try:
        from .analyze.measurement import measurement_resolver_bootstrap
        measurement_resolver_bootstrap.wire_measurement_accessor_resolvers()
    except Exception:
        pass


def _wire_reconciliation_accessor_resolvers():
    """Register lazy reconciliation home loading without importing the reconciliation hub at startup."""
    try:
        from .analyze.reconciliation import reconciliation_resolver_bootstrap
        reconciliation_resolver_bootstrap.wire_reconciliation_accessor_resolvers()
    except Exception:
        pass


def install_litellm() -> bool:
    """Capture LiteLLM-routed spend (Bedrock, Vertex/Gemini, Cohere, … — anything LiteLLM normalizes) into the same
    ledger as the SDK gate. Call once, AFTER `import litellm`. Returns True if litellm is present and now wired.
    (The startup gate auto-wires it only if litellm is already imported, so this explicit call is the reliable path.)"""
    _set_litellm_realtime_recorder(_record_rt)
    return _install_litellm(force=True)


def install_bedrock() -> bool:
    """Capture direct AWS Bedrock (boto3) model-invocation spend. Call once, AFTER `import boto3`. Returns True if
    botocore is present and now patched. (Not needed if you call Bedrock through LiteLLM — that's already covered.)"""
    _set_bedrock_realtime_recorder(_record_rt)
    return _install_bedrock(force=True)


def install_vertex() -> bool:
    """Capture direct Google Gemini / Vertex (google-genai) spend. Call once, AFTER importing the SDK. Returns True
    if the SDK is present and now patched. (Not needed if you call Gemini through LiteLLM — already covered.)"""
    _set_vertex_realtime_recorder(_record_rt)
    return _install_vertex(force=True)


__all__ = ["install", "require", "register", "install_litellm", "install_bedrock", "install_vertex",
           "SpendGateRefused", "gate_external", "refuse_metered", "MeteredCallRefused",
           "on_event", "context", "set_context", "feedback",
           "batch_cost", "realtime_cost", "estimate_cost", "price", "normalize",
           "PRICING", "PRICING_VERIFIED", "PRICING_SOURCE",
           "estimate_job", "test_job", "gated_batch", "check_bulk", "check_realtime", "check_compute",
           "record_estimate", "record_tested", "note_response", "maxtokens", "is_truncated", "GateBlocked",
           "ask", "AskResult", "BudgetRefused",
           "cancel_on_zero_billable", "relative_escalation_gate",
           "ItemLedger", "compute_job_fingerprint", "guard_resume", "compare_fingerprints", "read_fingerprint",
           "write_fingerprint", "JobPlanDrift", "JobMethodUndecided"]
from .interface.capability_surface_generation import build_capability_api_exports as _build_capability_api_exports

_capability_api_exports, CAPABILITY_API_COVERAGE = _build_capability_api_exports()
globals().update(_capability_api_exports)
__all__.extend(_capability_api_exports)
# Version SSOT: src/spendguard/platform/_version.py, read LIVE from source so `__version__` is always the version of the CODE
# actually running — including an editable install whose frozen dist-info metadata lags source (importlib.metadata
# reads install-time metadata, so it reported 0.7.2 while running 0.10.0 code). pyproject consumes the SAME file at
# build time via [tool.setuptools.dynamic]. One authoring home; never a metadata read, never a second literal.
from .platform._version import __version__ as __version__   # `as __version__`: explicit re-export (this IS the public attr)


def which_package():
    """Which installed distribution(s) provide the `spendguard` import name — normally just ['llm-spendguard'].

    Two dists can claim one import name (a leftover pre-rename `spendguard` egg-info, or any unrelated package of
    that name), and then whichever installed last wins. This reports it; `spendguard doctor` shows it when it's
    interesting. DELIBERATELY SILENT at import: an ambient stderr warning on every interpreter start is noise —
    it fires during unrelated work, in other repos, for something that is almost always a stale build artifact.
    Diagnostics belong in the diagnostic command."""
    try:
        from importlib.metadata import packages_distributions
        return sorted(packages_distributions().get("spendguard", []) or [])
    except Exception:
        return []


def shadowing_dists():
    """Distributions OTHER than llm-spendguard claiming the `spendguard` import name ([] = clean)."""
    return [d for d in which_package() if d.replace("_", "-").lower() != "llm-spendguard"]


def _auto_install():
    """Make `import spendguard` ACTUALLY GATE — close the #1 adoption gap: "pip install ≠ gated".

    The most common path is `pip install llm-spendguard` then `import spendguard`; without this, no SDK was ever
    patched and spend went ungated SILENTLY (the user thought they were protected). Now importing the guard
    installs it. Idempotent + fail-OPEN: a problem here never breaks the import.

      • SPENDGUARD_NO_AUTOINSTALL=1 — opt out (you call install()/require() yourself, or don't want import side
        effects). The venv/usercustomize hook and the CLI still install explicitly.
      • SPENDGUARD_REQUIRE=1 — upgrade to FAIL-CLOSED at import (like require()): if an LLM SDK is present but the
        gate can't be made to enforce here (wrong interpreter, or `spendguard off`), the import RAISES instead of
        letting you spend ungated. Lets a team enforce with one env var, zero per-script edits. A no-SDK context
        (e.g. running the `spendguard` CLI itself) is a no-op, never a hard error.
    """
    import os
    if os.environ.get("SPENDGUARD_NO_AUTOINSTALL") == "1":
        return
    strict = os.environ.get("SPENDGUARD_REQUIRE") == "1"
    try:
        install()
    except Exception as e:                       # fail-open unless strict
        if strict:
            raise SpendGateRefused(f"SPENDGUARD_REQUIRE=1 but the spend gate could not install: {e}")
        # FAIL-OPEN, BUT NEVER FAIL-QUIET. Returning here leaves the interpreter ungated: calls go out,
        # money is spent, and nothing records it — while every downstream reading (`spent today`, the
        # receipt, the ledger) shows a smaller number and looks perfectly healthy. That is the precise
        # shape of the leak spendguard exists to close, so the one thing it must not do is happen in
        # silence. Non-strict means "do not stop the program", not "do not mention it".
        import sys as _sys
        _sys.stderr.write(f"[spendguard] WARN the spend gate did NOT install ({type(e).__name__}: "
                          f"{str(e)[:100]}). This interpreter is UNGATED — LLM calls from it will not be "
                          f"recorded. Set SPENDGUARD_REQUIRE=1 to make this fatal.\n")
        return
    if not strict:
        return
    from spendguard.manage.admission.admission_accessors import resolve__enforcement_armed as _enforcement_armed, resolve__disabled as _disabled    # share require()'s fail-closed probe
    if _disabled():
        raise SpendGateRefused("SPENDGUARD_REQUIRE=1 but spendguard is DISABLED — `spendguard on` or unset "
                               "GATE_DISABLE. Refusing to import-and-spend ungated.")
    if not _enforcement_armed():
        raise SpendGateRefused(
            "SPENDGUARD_REQUIRE=1 but the gate is NOT enforcing in this interpreter — an installed LLM SDK "
            "is neither fully patched nor covered by a live import hook. Refusing to import-and-spend ungated.")


if not _CLI_BOOTSTRAP_IS_LAZY:
    _load_public_api_and_install_gate()
