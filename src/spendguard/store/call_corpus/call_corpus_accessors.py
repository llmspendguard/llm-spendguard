"""Dependency-free runtime accessors for the call corpus."""
import threading

CALL_OBSERVATION_NANO_COLUMNS = {column: f"{column}_nano" for column in ("cost", "input_cost_floor")}
_CONF = {"explicit": 1.0, "truncation": 1.0, "judge": 0.95, "used": 0.6, "mined": 0.5}
_local = threading.local()
_calls_module_contract = None
_calls_module_contract_lock = threading.Lock()
_calls_module_contract_consumers = []
_inbound_callio_resolvers = {}
_inbound_callio_resolver_lock = threading.Lock()


def __getattr__(attribute_name):
    """Materialize the generic inbound callio ``set_``/``resolve_`` protocol."""
    if attribute_name.startswith("set_") and attribute_name.endswith("_resolver"):
        symbol_name = attribute_name[len("set_"):-len("_resolver")]

        def set_inbound_callio_resolver(resolver):
            with _inbound_callio_resolver_lock:
                _inbound_callio_resolvers[symbol_name] = resolver

        set_inbound_callio_resolver.__name__ = attribute_name
        globals()[attribute_name] = set_inbound_callio_resolver
        return set_inbound_callio_resolver
    if attribute_name.startswith("resolve_"):
        symbol_name = attribute_name[len("resolve_"):]

        def resolve_inbound_callio_symbol(*args, **kwargs):
            with _inbound_callio_resolver_lock:
                resolver = _inbound_callio_resolvers.get(symbol_name)
            if resolver is None:
                raise RuntimeError(f"call_corpus_accessors.{symbol_name} resolver not wired")
            return resolver(*args, **kwargs)

        resolve_inbound_callio_symbol.__name__ = attribute_name
        globals()[attribute_name] = resolve_inbound_callio_symbol
        return resolve_inbound_callio_symbol
    raise AttributeError(attribute_name)


_calls_insert_resolver = None
_calls_insert_resolver_lock = threading.Lock()
_calls_cost_summary_resolver = None
_calls_cost_summary_resolver_lock = threading.Lock()
_calls_record_call_resolver = None
_calls_record_call_resolver_lock = threading.Lock()
_calls__ensure_call_observation_schema_resolver = None
_calls__ensure_call_observation_schema_resolver_lock = threading.Lock()
_calls_call_recording_consent_enabled_resolver = None
_calls_call_recording_consent_enabled_resolver_lock = threading.Lock()
_calls_caller_resolver = None
_calls_caller_resolver_lock = threading.Lock()
_calls_check_output_resolver = None
_calls_check_output_resolver_lock = threading.Lock()
_calls_clear_retry_context_resolver = None
_calls_clear_retry_context_resolver_lock = threading.Lock()
_calls_context_resolver = None
_calls_context_resolver_lock = threading.Lock()
_calls_current_resolver = None
_calls_current_resolver_lock = threading.Lock()
_calls_fell_from_context_resolver = None
_calls_fell_from_context_resolver_lock = threading.Lock()
_calls_finalize_bakeoff_measurement_resolver = None
_calls_finalize_bakeoff_measurement_resolver_lock = threading.Lock()
_calls_flow_agg_resolver = None
_calls_flow_agg_resolver_lock = threading.Lock()
_calls_gate_internal_resolver = None
_calls_gate_internal_resolver_lock = threading.Lock()
_calls_intent_spend_resolver = None
_calls_intent_spend_resolver_lock = threading.Lock()
_calls_lane_model_outcomes_resolver = None
_calls_lane_model_outcomes_resolver_lock = threading.Lock()
_calls_mean_out_by_executor_model_resolver = None
_calls_mean_out_by_executor_model_resolver_lock = threading.Lock()
_calls_metered_realtime_by_intent_resolver = None
_calls_metered_realtime_by_intent_resolver_lock = threading.Lock()
_calls_reconcile_calls_batch_estimate_resolver = None
_calls_reconcile_calls_batch_estimate_resolver_lock = threading.Lock()
_calls_recorded_intents_resolver = None
_calls_recorded_intents_resolver_lock = threading.Lock()
_calls_redirect_context_resolver = None
_calls_redirect_context_resolver_lock = threading.Lock()
_calls_set_context_resolver = None
_calls_set_context_resolver_lock = threading.Lock()
_calls_tested_recently_resolver = None
_calls_tested_recently_resolver_lock = threading.Lock()


def _set_calls_resolver(resolver_name, resolver, resolver_lock):
    with resolver_lock:
        globals()[resolver_name] = resolver


def _resolve_calls_provider(resolver_name, resolver_lock, display_name, args, kwargs):
    with resolver_lock:
        resolver = globals()[resolver_name]
    if resolver is None:
        raise RuntimeError(f"calls {display_name} resolver is not wired")
    return resolver(*args, **kwargs)


def set_calls_module_contract(calls_module_contract):
    """Wire the eagerly imported calls module for identity-sensitive compatibility surfaces."""
    global _calls_module_contract
    with _calls_module_contract_lock:
        _calls_module_contract = calls_module_contract
        calls_module_contract_consumers = tuple(_calls_module_contract_consumers)
    for calls_module_contract_consumer in calls_module_contract_consumers:
        calls_module_contract_consumer(calls_module_contract)


def resolve_calls_module_contract():
    """Return the calls module identity without creating an importer edge to it."""
    with _calls_module_contract_lock:
        calls_module_contract = _calls_module_contract
    if calls_module_contract is None:
        raise RuntimeError("calls module contract is not wired")
    return calls_module_contract


def register_calls_module_contract_consumer(calls_module_contract_consumer):
    """Register an identity-sensitive consumer without importing calls there."""
    with _calls_module_contract_lock:
        _calls_module_contract_consumers.append(calls_module_contract_consumer)
        calls_module_contract = _calls_module_contract
    if calls_module_contract is not None:
        calls_module_contract_consumer(calls_module_contract)


def set_calls_insert_resolver(calls_insert_resolver):
    """Wire the resolver for inserting call-corpus rows."""
    global _calls_insert_resolver
    with _calls_insert_resolver_lock:
        _calls_insert_resolver = calls_insert_resolver


def resolve_calls_insert(*args, **kwargs):
    """Insert a call-corpus row through the eagerly wired provider."""
    with _calls_insert_resolver_lock:
        calls_insert_resolver = _calls_insert_resolver
    if calls_insert_resolver is None:
        raise RuntimeError("calls insert resolver is not wired")
    return calls_insert_resolver(*args, **kwargs)


def set_calls_cost_summary_resolver(calls_cost_summary_resolver):
    """Wire the resolver for summarizing call-corpus costs."""
    global _calls_cost_summary_resolver
    with _calls_cost_summary_resolver_lock:
        _calls_cost_summary_resolver = calls_cost_summary_resolver


def resolve_calls_cost_summary(*args, **kwargs):
    """Summarize call-corpus costs through the eagerly wired provider."""
    with _calls_cost_summary_resolver_lock:
        calls_cost_summary_resolver = _calls_cost_summary_resolver
    if calls_cost_summary_resolver is None:
        raise RuntimeError("calls cost summary resolver is not wired")
    return calls_cost_summary_resolver(*args, **kwargs)


def set_calls_record_call_resolver(calls_record_call_resolver):
    """Wire the resolver for recording call-corpus rows."""
    global _calls_record_call_resolver
    with _calls_record_call_resolver_lock:
        _calls_record_call_resolver = calls_record_call_resolver


def resolve_calls_record_call(*args, **kwargs):
    """Record a call-corpus row through the eagerly wired provider."""
    with _calls_record_call_resolver_lock:
        calls_record_call_resolver = _calls_record_call_resolver
    if calls_record_call_resolver is None:
        raise RuntimeError("calls record call resolver is not wired")
    return calls_record_call_resolver(*args, **kwargs)


def set_calls__ensure_call_observation_schema_resolver(resolver):
    _set_calls_resolver("_calls__ensure_call_observation_schema_resolver", resolver, _calls__ensure_call_observation_schema_resolver_lock)

def resolve_calls__ensure_call_observation_schema(*args, **kwargs):
    return _resolve_calls_provider("_calls__ensure_call_observation_schema_resolver", _calls__ensure_call_observation_schema_resolver_lock, "_ensure_call_observation_schema", args, kwargs)

def set_calls_call_recording_consent_enabled_resolver(resolver):
    _set_calls_resolver("_calls_call_recording_consent_enabled_resolver", resolver, _calls_call_recording_consent_enabled_resolver_lock)

def resolve_calls_call_recording_consent_enabled(*args, **kwargs):
    return _resolve_calls_provider("_calls_call_recording_consent_enabled_resolver", _calls_call_recording_consent_enabled_resolver_lock, "call_recording_consent_enabled", args, kwargs)

def set_calls_caller_resolver(resolver):
    _set_calls_resolver("_calls_caller_resolver", resolver, _calls_caller_resolver_lock)

def resolve_calls_caller(*args, **kwargs):
    return _resolve_calls_provider("_calls_caller_resolver", _calls_caller_resolver_lock, "caller", args, kwargs)

def set_calls_check_output_resolver(resolver):
    _set_calls_resolver("_calls_check_output_resolver", resolver, _calls_check_output_resolver_lock)

def resolve_calls_check_output(*args, **kwargs):
    return _resolve_calls_provider("_calls_check_output_resolver", _calls_check_output_resolver_lock, "check_output", args, kwargs)

def set_calls_clear_retry_context_resolver(resolver):
    _set_calls_resolver("_calls_clear_retry_context_resolver", resolver, _calls_clear_retry_context_resolver_lock)

def resolve_calls_clear_retry_context(*args, **kwargs):
    return _resolve_calls_provider("_calls_clear_retry_context_resolver", _calls_clear_retry_context_resolver_lock, "clear_retry_context", args, kwargs)

def set_calls_context_resolver(resolver):
    _set_calls_resolver("_calls_context_resolver", resolver, _calls_context_resolver_lock)

def resolve_calls_context(*args, **kwargs):
    return _resolve_calls_provider("_calls_context_resolver", _calls_context_resolver_lock, "context", args, kwargs)

def set_calls_current_resolver(resolver):
    _set_calls_resolver("_calls_current_resolver", resolver, _calls_current_resolver_lock)

def resolve_calls_current(*args, **kwargs):
    return _resolve_calls_provider("_calls_current_resolver", _calls_current_resolver_lock, "current", args, kwargs)

def set_calls_fell_from_context_resolver(resolver):
    _set_calls_resolver("_calls_fell_from_context_resolver", resolver, _calls_fell_from_context_resolver_lock)

def resolve_calls_fell_from_context(*args, **kwargs):
    return _resolve_calls_provider("_calls_fell_from_context_resolver", _calls_fell_from_context_resolver_lock, "fell_from_context", args, kwargs)

def set_calls_finalize_bakeoff_measurement_resolver(resolver):
    _set_calls_resolver("_calls_finalize_bakeoff_measurement_resolver", resolver, _calls_finalize_bakeoff_measurement_resolver_lock)

def resolve_calls_finalize_bakeoff_measurement(*args, **kwargs):
    return _resolve_calls_provider("_calls_finalize_bakeoff_measurement_resolver", _calls_finalize_bakeoff_measurement_resolver_lock, "finalize_bakeoff_measurement", args, kwargs)

def set_calls_flow_agg_resolver(resolver):
    _set_calls_resolver("_calls_flow_agg_resolver", resolver, _calls_flow_agg_resolver_lock)

def resolve_calls_flow_agg(*args, **kwargs):
    return _resolve_calls_provider("_calls_flow_agg_resolver", _calls_flow_agg_resolver_lock, "flow_agg", args, kwargs)

def set_calls_gate_internal_resolver(resolver):
    _set_calls_resolver("_calls_gate_internal_resolver", resolver, _calls_gate_internal_resolver_lock)

def resolve_calls_gate_internal(*args, **kwargs):
    return _resolve_calls_provider("_calls_gate_internal_resolver", _calls_gate_internal_resolver_lock, "gate_internal", args, kwargs)

def set_calls_intent_spend_resolver(resolver):
    _set_calls_resolver("_calls_intent_spend_resolver", resolver, _calls_intent_spend_resolver_lock)

def resolve_calls_intent_spend(*args, **kwargs):
    return _resolve_calls_provider("_calls_intent_spend_resolver", _calls_intent_spend_resolver_lock, "intent_spend", args, kwargs)

def set_calls_lane_model_outcomes_resolver(resolver):
    _set_calls_resolver("_calls_lane_model_outcomes_resolver", resolver, _calls_lane_model_outcomes_resolver_lock)

def resolve_calls_lane_model_outcomes(*args, **kwargs):
    return _resolve_calls_provider("_calls_lane_model_outcomes_resolver", _calls_lane_model_outcomes_resolver_lock, "lane_model_outcomes", args, kwargs)

def set_calls_mean_out_by_executor_model_resolver(resolver):
    _set_calls_resolver("_calls_mean_out_by_executor_model_resolver", resolver, _calls_mean_out_by_executor_model_resolver_lock)

def resolve_calls_mean_out_by_executor_model(*args, **kwargs):
    return _resolve_calls_provider("_calls_mean_out_by_executor_model_resolver", _calls_mean_out_by_executor_model_resolver_lock, "mean_out_by_executor_model", args, kwargs)

def set_calls_metered_realtime_by_intent_resolver(resolver):
    _set_calls_resolver("_calls_metered_realtime_by_intent_resolver", resolver, _calls_metered_realtime_by_intent_resolver_lock)

def resolve_calls_metered_realtime_by_intent(*args, **kwargs):
    return _resolve_calls_provider("_calls_metered_realtime_by_intent_resolver", _calls_metered_realtime_by_intent_resolver_lock, "metered_realtime_by_intent", args, kwargs)

def set_calls_reconcile_calls_batch_estimate_resolver(resolver):
    _set_calls_resolver("_calls_reconcile_calls_batch_estimate_resolver", resolver, _calls_reconcile_calls_batch_estimate_resolver_lock)

def resolve_calls_reconcile_calls_batch_estimate(*args, **kwargs):
    return _resolve_calls_provider("_calls_reconcile_calls_batch_estimate_resolver", _calls_reconcile_calls_batch_estimate_resolver_lock, "reconcile_calls_batch_estimate", args, kwargs)

def set_calls_recorded_intents_resolver(resolver):
    _set_calls_resolver("_calls_recorded_intents_resolver", resolver, _calls_recorded_intents_resolver_lock)

def resolve_calls_recorded_intents(*args, **kwargs):
    return _resolve_calls_provider("_calls_recorded_intents_resolver", _calls_recorded_intents_resolver_lock, "recorded_intents", args, kwargs)

def set_calls_redirect_context_resolver(resolver):
    _set_calls_resolver("_calls_redirect_context_resolver", resolver, _calls_redirect_context_resolver_lock)

def resolve_calls_redirect_context(*args, **kwargs):
    return _resolve_calls_provider("_calls_redirect_context_resolver", _calls_redirect_context_resolver_lock, "redirect_context", args, kwargs)

def set_calls_set_context_resolver(resolver):
    _set_calls_resolver("_calls_set_context_resolver", resolver, _calls_set_context_resolver_lock)

def resolve_calls_set_context(*args, **kwargs):
    return _resolve_calls_provider("_calls_set_context_resolver", _calls_set_context_resolver_lock, "set_context", args, kwargs)

def set_calls_tested_recently_resolver(resolver):
    _set_calls_resolver("_calls_tested_recently_resolver", resolver, _calls_tested_recently_resolver_lock)

def resolve_calls_tested_recently(*args, **kwargs):
    return _resolve_calls_provider("_calls_tested_recently_resolver", _calls_tested_recently_resolver_lock, "tested_recently", args, kwargs)
