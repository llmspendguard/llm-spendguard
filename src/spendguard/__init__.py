"""spendguard — provider-agnostic LLM cost discipline.

A pre-submit cost GATE (overlay on the OpenAI/Anthropic SDKs), a per-run ESTIMATOR,
token-accurate RECONCILERS, and a daily/weekly/monthly spend REPORT — all priced from
one canonical, verifiable price table.

    import spendguard; spendguard.install(cap=75)   # gate every batch in this process
"""
# `estimate` is EXPORTED AS estimate_cost, deliberately. This package contains a MODULE named
# spendguard.estimate (the measure-a-sample-then-project path), and re-exporting pricing's function under
# the same bare name shadowed it — so `from spendguard import estimate` handed you a function, and which
# of the two you got depended on import ORDER. The consequence was not theoretical: the sanctioned
# measure-then-project path was unreachable by its obvious import, which is part of why a cost was quoted
# from invented token counts instead. A module and a function cannot share a name in one namespace; the
# function is the one that moves, since the module's name says exactly what it is.
from .pricing import (batch_cost, realtime_cost, estimate as estimate_cost, price, normalize,
                      PRICING, PRICING_VERIFIED, PRICING_SOURCE)
from .gate import install, require, register, SpendGateRefused
from .emit import on_event
from .calls import context, set_context, feedback
from .bulkgate import (estimate_job, test_job, gated_batch, check_bulk, check_realtime, check_compute,
                       record_estimate, record_tested, note_response, maxtokens, is_truncated, GateBlocked)
from .litellm_adapter import install as _install_litellm
from .bedrock_adapter import install as _install_bedrock
from .vertex_adapter import install as _install_vertex


def install_litellm() -> bool:
    """Capture LiteLLM-routed spend (Bedrock, Vertex/Gemini, Cohere, … — anything LiteLLM normalizes) into the same
    ledger as the SDK gate. Call once, AFTER `import litellm`. Returns True if litellm is present and now wired.
    (The startup gate auto-wires it only if litellm is already imported, so this explicit call is the reliable path.)"""
    return _install_litellm(force=True)


def install_bedrock() -> bool:
    """Capture direct AWS Bedrock (boto3) model-invocation spend. Call once, AFTER `import boto3`. Returns True if
    botocore is present and now patched. (Not needed if you call Bedrock through LiteLLM — that's already covered.)"""
    return _install_bedrock(force=True)


def install_vertex() -> bool:
    """Capture direct Google Gemini / Vertex (google-genai) spend. Call once, AFTER importing the SDK. Returns True
    if the SDK is present and now patched. (Not needed if you call Gemini through LiteLLM — already covered.)"""
    return _install_vertex(force=True)


__all__ = ["install", "require", "register", "install_litellm", "install_bedrock", "install_vertex",
           "SpendGateRefused", "on_event", "context", "set_context", "feedback",
           "batch_cost", "realtime_cost", "estimate_cost", "price", "normalize",
           "PRICING", "PRICING_VERIFIED", "PRICING_SOURCE",
           "estimate_job", "test_job", "gated_batch", "check_bulk", "check_realtime", "check_compute",
           "record_estimate", "record_tested", "note_response", "maxtokens", "is_truncated", "GateBlocked"]
# Version comes from the INSTALLED package metadata (single source: pyproject.toml) — a hardcoded literal
# here shipped as "0.3.0" for four releases before anyone noticed. Editable/source-tree fallback: "0.0.0.dev0".
try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("llm-spendguard")
except Exception:
    __version__ = "0.0.0.dev0"


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
    from .gate import _any_patched, _disabled    # fail-closed checks (refuse loudly)
    if _disabled():
        raise SpendGateRefused("SPENDGUARD_REQUIRE=1 but spendguard is DISABLED — `spendguard on` or unset "
                               "GATE_DISABLE. Refusing to import-and-spend ungated.")
    if not _any_patched():
        import importlib.util
        if any(importlib.util.find_spec(m) for m in ("openai", "anthropic")):
            raise SpendGateRefused(
                "SPENDGUARD_REQUIRE=1 but the gate is NOT enforcing in this interpreter — an LLM SDK is installed "
                "yet wasn't patched (wrong python/venv?). Refusing to import-and-spend ungated. Fix: run under a "
                "gated venv, or unset SPENDGUARD_REQUIRE for a no-op import here.")


_auto_install()
