"""Dependency-free inbound seam for admission-domain symbols."""

import threading

import functools
import sys
import types


def _late_bind_admission_module_function(resolver):
    """Preserve module-attribute dispatch while retaining safe callable fallback."""
    resolver_module_name = getattr(resolver, "__module__", None)
    resolver_function_name = getattr(resolver, "__name__", None)
    resolver_qualified_name = getattr(resolver, "__qualname__", None)
    is_top_level_function = (
        isinstance(resolver, types.FunctionType)
        and isinstance(resolver_module_name, str)
        and isinstance(resolver_function_name, str)
        and resolver_function_name != "<lambda>"
        and resolver_qualified_name == resolver_function_name
    )
    if not is_top_level_function:
        return resolver

    @functools.wraps(resolver)
    def call_current_admission_module_function(*args, **kwargs):
        resolver_module = sys.modules.get(resolver_module_name)
        current_resolver = getattr(resolver_module, resolver_function_name, None)
        if not callable(current_resolver):
            current_resolver = resolver
        return current_resolver(*args, **kwargs)

    return call_current_admission_module_function


UNKNOWN_PROVIDER = "unknown"
BUDGET_AUXILIARY_POOL_KEY = "budget_auxiliary_reads"
UNATTRIBUTED_PROJECT = "unattributed"
_RECONCILED = "(provider-batch)"
_TRUE_DOWN_CONV = "(true-down)"
QUARANTINE_CONV = "(impossible-estimate)"  # impossible estimates stay auditable but out of spend totals
BASIS_ESTIMATE = "estimate"
BASIS_BILLED = "billed"
BASIS_ASSUMED = "assumed"
BASIS_RECONSTRUCTED = "reconstructed"
BASIS_UNPRICED = "unpriced"  # the call happened and the TOKENS are real; the $ is unknown, NOT zero
RUNAWAY_MIN_SAMPLES = 20

RESIDENCY_NUDGE_ENABLED_DEFAULT = True
RESIDENCY_MIN_RESULT_BYTES_DEFAULT = 10240
RESIDENCY_MIN_TURNS_DEFAULT = 20
RESIDENCY_MIN_CONTEXT_TOKENS_DEFAULT = 100000
RESIDENCY_REMAINING_TURNS_DEFAULT = 100
RESIDENCY_JUDGE_RATIONALE_MAX_CHARS_DEFAULT = 160
RESIDENCY_JSON_ESCAPE_CHARS_PER_INPUT_CHAR_MAX = 12
RESIDENCY_JUDGE_REPLY_FIXED_TOKEN_MARGIN = 128
RESIDENCY_JUDGE_OUTPUT_TOKENS_DEFAULT = (
    RESIDENCY_JUDGE_RATIONALE_MAX_CHARS_DEFAULT * RESIDENCY_JSON_ESCAPE_CHARS_PER_INPUT_CHAR_MAX
    + RESIDENCY_JUDGE_REPLY_FIXED_TOKEN_MARGIN
)
RESIDENCY_SUBAGENT_RETURN_TOKENS_DEFAULT = 400
RESIDENCY_RECEIPT_TOP_CONTRIBUTORS_DEFAULT = 5
RESIDENCY_VERDICT_CACHE_ENTRIES_DEFAULT = 512
RESIDENCY_HOOK_TIMEOUT_SECONDS_DEFAULT = 30

WEEKLY_CAP_GUARD_ENABLED_DEFAULT = True
WEEKLY_CAP_GUARD_THRESHOLD_DEFAULT = 0.30
WEEKLY_CAP_GUARD_ON_UNKNOWN_DEFAULT = "block"
WEEKLY_CAP_GUARD_BLOCKED_SPAWN_TOOLS_DEFAULT = ["Task", "Agent"]

_admission_accessor_lock = threading.Lock()
_admission_resolvers = {}


def __getattr__(attribute_name):
    """Materialize resolver APIs from their fixed ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_admission_symbol_resolver(resolver):
            with _admission_accessor_lock:
                _admission_resolvers[symbol_name] = _late_bind_admission_module_function(resolver)

        set_admission_symbol_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_admission_symbol_resolver
        return set_admission_symbol_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_admission_symbol(*args, **kwargs):
            resolver = _admission_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"admission_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_admission_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_admission_symbol
        return resolve_admission_symbol
    raise AttributeError(attribute_name)
