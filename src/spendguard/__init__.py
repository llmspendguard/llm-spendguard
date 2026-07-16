"""spendguard — provider-agnostic LLM cost discipline.

A pre-submit cost GATE (overlay on the OpenAI/Anthropic SDKs), a per-run ESTIMATOR,
token-accurate RECONCILERS, and a daily/weekly/monthly spend REPORT — all priced from
one canonical, verifiable price table.

    import spendguard; spendguard.install(cap=75)   # gate every batch in this process
"""
from .pricing import (batch_cost, realtime_cost, estimate, price, normalize,
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
           "batch_cost", "realtime_cost", "estimate", "price", "normalize",
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
