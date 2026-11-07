"""Dependency-free inbound seam for execution-domain symbols."""
import os
import threading
import time

SPENDGUARD_IMPORT_NAME = "spendguard"
LANE_MIN_TIMEOUT_S = 150.0
MAX_TOKEN_CEILING = 128_000
SCHEMA_STRICT = "strict"
TOKEN_FLOOR = 32_000

PROVIDERS = {
    "openai": {"base_url": None, "key_env": "OPENAI_API_KEY", "prefixes": ("gpt-", "o1", "o3", "chatgpt"), "kind": "openai", "batch": True},
    "anthropic": {"base_url": None, "key_env": "ANTHROPIC_API_KEY", "prefixes": ("claude-",), "kind": "anthropic"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "key_env": "GEMINI_API_KEY", "prefixes": ("gemini-",), "kind": "openai", "sdk_import": "google"},
    "deepseek": {"base_url": "https://api.deepseek.com", "key_env": "DEEPSEEK_API_KEY", "prefixes": ("deepseek",), "kind": "openai"},
    "qwen": {"base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "key_env": "DASHSCOPE_API_KEY", "prefixes": ("qwen", "qwq"), "kind": "openai"},
    "zai": {"base_url": "https://api.z.ai/api/paas/v4", "key_env": "ZAI_API_KEY", "prefixes": ("glm-",), "kind": "openai", "sdk_import": "zhipuai"},
    "voyage": {"base_url": "https://api.voyageai.com/v1", "key_env": "VOYAGE_API_KEY", "prefixes": ("voyage-",), "kind": "openai", "batch": False},
    "moonshot": {"base_url": "https://api.moonshot.ai/v1", "key_env": "MOONSHOT_API_KEY", "prefixes": ("kimi", "moonshot-"), "kind": "openai"},
}

CODEX_APP_SERVER_RATE_LIMITS_METHOD = "account/rateLimits/read"
CODEX_POOL_WORKER_PREFIX = "worker-"

OK = "ok"
TRUNCATED = "truncated"
EMPTY = "empty"
REFUSED = "refused"
TRANSPORT_ERROR = "transport_error"
DEADLINE_EXCEEDED = "deadline_exceeded"
SCHEMA_VIOLATION = "schema_violation"
UNFUNDED = "unfunded"
GATE_REFUSED = "gate_refused"
OVERLOADED = "overloaded"
PAYLOAD_REJECTED = "payload_rejected"
PREFLIGHT_UNMET = "preflight_unmet"
KINDS = (OK, TRUNCATED, EMPTY, REFUSED, TRANSPORT_ERROR, DEADLINE_EXCEEDED, SCHEMA_VIOLATION, UNFUNDED, OVERLOADED, PAYLOAD_REJECTED, PREFLIGHT_UNMET, GATE_REFUSED)
FAILURES = tuple(kind for kind in KINDS if kind != OK)
RETRYABLE = (TRANSPORT_ERROR, OVERLOADED)

_execution_result_run_id = None
_execution_result_run_id_lock = threading.RLock()


class NotOk(RuntimeError):
    """Raised when a caller reads text from an unsuccessful execution result."""


class Result:
    """Typed vendor-call outcome; text is available only for successful results."""

    __slots__ = ("kind", "_text", "vendor", "model", "stop_reason", "in_tok", "out_tok", "cost",
                 "latency", "error", "prompt_sha", "run_id", "ts", "purpose", "payload",
                 "http_status", "provider_error", "attempts", "text_head")

    def __init__(self, kind, vendor, model, text=None, stop_reason=None, in_tok=None, out_tok=None, cost=None,
                 latency=0.0, error=None, prompt_sha="", purpose="", payload=None,
                 http_status=None, provider_error=None, attempts=1, text_head=None):
        if kind not in KINDS:
            raise ValueError(f"unknown result kind {kind!r} — one of {KINDS}")
        self.kind, self._text = kind, (text if kind == OK else None)
        self.vendor, self.model, self.stop_reason = vendor, model, stop_reason
        self.in_tok, self.out_tok, self.cost, self.latency = in_tok, out_tok, cost, latency
        self.error, self.prompt_sha, self.purpose, self.payload = error, prompt_sha, purpose, payload
        self.http_status, self.provider_error, self.attempts = http_status, provider_error, int(attempts or 1)
        self.text_head = (text_head if text_head is not None else (text or ""))[:200]
        self.run_id, self.ts = run_id(), time.time()

    @property
    def ok(self):
        return self.kind == OK

    @property
    def elapsed_s(self):
        return round(self.latency, 3)

    @property
    def finish_reason(self):
        return self.stop_reason

    @property
    def text(self):
        if self.kind != OK:
            raise NotOk(f"{self.vendor}/{self.model}: result is {self.kind!r} "
                        f"(stop_reason={self.stop_reason!r}, error={self.error!r}) — there is no usable text. "
                        f"A {self.kind} response is a FAILURE; treating it as content is the bug this prevents.")
        return self._text

    def as_row(self):
        return {"run_id": self.run_id, "ts": self.ts, "vendor": self.vendor, "model": self.model,
                "purpose": self.purpose, "kind": self.kind, "stop_reason": self.stop_reason,
                "finish_reason": self.stop_reason, "prompt_sha": self.prompt_sha,
                "in_tok": self.in_tok, "out_tok": self.out_tok, "cost": self.cost,
                "latency": round(self.latency, 3), "elapsed_s": round(self.latency, 3), "error": self.error,
                "http_status": self.http_status, "provider_error": self.provider_error,
                "attempts": self.attempts, "text_head": self.text_head}

    def __repr__(self):
        return f"<Result {self.kind} {self.vendor}/{self.model} out={self.out_tok} {self.latency:.1f}s>"


def run_id():
    global _execution_result_run_id
    if _execution_result_run_id is None:
        with _execution_result_run_id_lock:
            if _execution_result_run_id is None:
                _execution_result_run_id = "run-%d-%s" % (int(time.time()), os.urandom(3).hex())
    return _execution_result_run_id


_execution_accessor_lock = threading.Lock()

__LANES_resolver = None

def set__LANES_resolver(resolver):
    global __LANES_resolver
    with _execution_accessor_lock:
        __LANES_resolver = resolver

def resolve__LANES():
    resolver = __LANES_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._LANES resolver not wired")
    return resolver()

__compose_gemini_reasoning_resolver = None

def set__compose_gemini_reasoning_resolver(resolver):
    global __compose_gemini_reasoning_resolver
    with _execution_accessor_lock:
        __compose_gemini_reasoning_resolver = resolver

def resolve__compose_gemini_reasoning(*args, **kwargs):
    resolver = __compose_gemini_reasoning_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._compose_gemini_reasoning resolver not wired")
    return resolver(*args, **kwargs)

__est_call_tokens_resolver = None

def set__est_call_tokens_resolver(resolver):
    global __est_call_tokens_resolver
    with _execution_accessor_lock:
        __est_call_tokens_resolver = resolver

def resolve__est_call_tokens(*args, **kwargs):
    resolver = __est_call_tokens_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._est_call_tokens resolver not wired")
    return resolver(*args, **kwargs)

__exception_failure_kind_resolver = None

def set__exception_failure_kind_resolver(resolver):
    global __exception_failure_kind_resolver
    with _execution_accessor_lock:
        __exception_failure_kind_resolver = resolver

def resolve__exception_failure_kind(*args, **kwargs):
    resolver = __exception_failure_kind_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._exception_failure_kind resolver not wired")
    return resolver(*args, **kwargs)

__executor_resolver = None

def set__executor_resolver(resolver):
    global __executor_resolver
    with _execution_accessor_lock:
        __executor_resolver = resolver

def resolve__executor(*args, **kwargs):
    resolver = __executor_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._executor resolver not wired")
    return resolver(*args, **kwargs)

__image_input_tokens_resolver = None

def set__image_input_tokens_resolver(resolver):
    global __image_input_tokens_resolver
    with _execution_accessor_lock:
        __image_input_tokens_resolver = resolver

def resolve__image_input_tokens(*args, **kwargs):
    resolver = __image_input_tokens_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._image_input_tokens resolver not wired")
    return resolver(*args, **kwargs)

__lane_cooling_resolver = None

def set__lane_cooling_resolver(resolver):
    global __lane_cooling_resolver
    with _execution_accessor_lock:
        __lane_cooling_resolver = resolver

def resolve__lane_cooling(*args, **kwargs):
    resolver = __lane_cooling_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._lane_cooling resolver not wired")
    return resolver(*args, **kwargs)

__lane_for_resolver = None

def set__lane_for_resolver(resolver):
    global __lane_for_resolver
    with _execution_accessor_lock:
        __lane_for_resolver = resolver

def resolve__lane_for(*args, **kwargs):
    resolver = __lane_for_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._lane_for resolver not wired")
    return resolver(*args, **kwargs)

__output_budget_resolver = None

def set_output_budget_resolver(resolver):
    global __output_budget_resolver
    with _execution_accessor_lock:
        __output_budget_resolver = resolver

def resolve_output_budget(*args, **kwargs):
    resolver = __output_budget_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.output_budget resolver not wired")
    return resolver(*args, **kwargs)

__heal_guard_resolver = None

def set__heal_guard_resolver(resolver):
    global __heal_guard_resolver
    with _execution_accessor_lock:
        __heal_guard_resolver = resolver

def resolve__heal_guard():
    resolver = __heal_guard_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._heal_guard resolver not wired")
    return resolver()

__resolve_guard_resolver = None

def set__resolve_guard_resolver(resolver):
    global __resolve_guard_resolver
    with _execution_accessor_lock:
        __resolve_guard_resolver = resolver

def resolve__resolve_guard():
    resolver = __resolve_guard_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._resolve_guard resolver not wired")
    return resolver()

__lane_model_cooling_resolver = None

def set__lane_model_cooling_resolver(resolver):
    global __lane_model_cooling_resolver
    with _execution_accessor_lock:
        __lane_model_cooling_resolver = resolver

def resolve__lane_model_cooling(*args, **kwargs):
    resolver = __lane_model_cooling_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._lane_model_cooling resolver not wired")
    return resolver(*args, **kwargs)

__load_image_resolver = None

def set__load_image_resolver(resolver):
    global __load_image_resolver
    with _execution_accessor_lock:
        __load_image_resolver = resolver

def resolve__load_image(*args, **kwargs):
    resolver = __load_image_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._load_image resolver not wired")
    return resolver(*args, **kwargs)

_call_resolver = None

def set_call_resolver(resolver):
    global _call_resolver
    with _execution_accessor_lock:
        _call_resolver = resolver

def resolve_call(*args, **kwargs):
    resolver = _call_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.call resolver not wired")
    return resolver(*args, **kwargs)

_deadline_for_resolver = None

def set_deadline_for_resolver(resolver):
    global _deadline_for_resolver
    with _execution_accessor_lock:
        _deadline_for_resolver = resolver

def resolve_deadline_for(*args, **kwargs):
    resolver = _deadline_for_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.deadline_for resolver not wired")
    return resolver(*args, **kwargs)

_llm_capability_import_names_resolver = None

def set_llm_capability_import_names_resolver(resolver):
    global _llm_capability_import_names_resolver
    with _execution_accessor_lock:
        _llm_capability_import_names_resolver = resolver

def resolve_llm_capability_import_names(*args, **kwargs):
    resolver = _llm_capability_import_names_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.llm_capability_import_names resolver not wired")
    return resolver(*args, **kwargs)

_metered_fallback_id_resolver = None

def set_metered_fallback_id_resolver(resolver):
    global _metered_fallback_id_resolver
    with _execution_accessor_lock:
        _metered_fallback_id_resolver = resolver

def resolve_metered_fallback_id(*args, **kwargs):
    resolver = _metered_fallback_id_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.metered_fallback_id resolver not wired")
    return resolver(*args, **kwargs)

_provider_for_resolver = None

def set_provider_for_resolver(resolver):
    global _provider_for_resolver
    with _execution_accessor_lock:
        _provider_for_resolver = resolver

def resolve_provider_for(*args, **kwargs):
    resolver = _provider_for_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.provider_for resolver not wired")
    return resolver(*args, **kwargs)

_schema_capability_resolver = None

def set_schema_capability_resolver(resolver):
    global _schema_capability_resolver
    with _execution_accessor_lock:
        _schema_capability_resolver = resolver

def resolve_schema_capability(*args, **kwargs):
    resolver = _schema_capability_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.schema_capability resolver not wired")
    return resolver(*args, **kwargs)

_strict_map_violation_resolver = None

def set_strict_map_violation_resolver(resolver):
    global _strict_map_violation_resolver
    with _execution_accessor_lock:
        _strict_map_violation_resolver = resolver

def resolve_strict_map_violation(*args, **kwargs):
    resolver = _strict_map_violation_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.strict_map_violation resolver not wired")
    return resolver(*args, **kwargs)

_structured_reply_resolver = None

def set_structured_reply_resolver(resolver):
    global _structured_reply_resolver
    with _execution_accessor_lock:
        _structured_reply_resolver = resolver

def resolve_structured_reply(*args, **kwargs):
    resolver = _structured_reply_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.structured_reply resolver not wired")
    return resolver(*args, **kwargs)

_vision_resolver = None

def set_vision_resolver(resolver):
    global _vision_resolver
    with _execution_accessor_lock:
        _vision_resolver = resolver

def resolve_vision(*args, **kwargs):
    resolver = _vision_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.vision resolver not wired")
    return resolver(*args, **kwargs)

_model_ids_resolver = None

def set_model_ids_resolver(resolver):
    global _model_ids_resolver
    with _execution_accessor_lock:
        _model_ids_resolver = resolver

def resolve_model_ids(*args, **kwargs):
    resolver = _model_ids_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.model_ids resolver not wired")
    return resolver(*args, **kwargs)

_poll_resolver = None

def set_poll_resolver(resolver):
    global _poll_resolver
    with _execution_accessor_lock:
        _poll_resolver = resolver

def resolve_poll(*args, **kwargs):
    resolver = _poll_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.poll resolver not wired")
    return resolver(*args, **kwargs)

_status_resolver = None

def set_status_resolver(resolver):
    global _status_resolver
    with _execution_accessor_lock:
        _status_resolver = resolver

def resolve_status(*args, **kwargs):
    resolver = _status_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.status resolver not wired")
    return resolver(*args, **kwargs)

_submit_offload_resolver = None

def set_submit_offload_resolver(resolver):
    global _submit_offload_resolver
    with _execution_accessor_lock:
        _submit_offload_resolver = resolver

def resolve_submit_offload(*args, **kwargs):
    resolver = _submit_offload_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.submit_offload resolver not wired")
    return resolver(*args, **kwargs)

__bin_resolver = None

def set__bin_resolver(resolver):
    global __bin_resolver
    with _execution_accessor_lock:
        __bin_resolver = resolver

def resolve__bin(*args, **kwargs):
    resolver = __bin_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._bin resolver not wired")
    return resolver(*args, **kwargs)

__codex_effort_resolver = None

def set__codex_effort_resolver(resolver):
    global __codex_effort_resolver
    with _execution_accessor_lock:
        __codex_effort_resolver = resolver

def resolve__codex_effort(*args, **kwargs):
    resolver = __codex_effort_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._codex_effort resolver not wired")
    return resolver(*args, **kwargs)

__daemon_enabled_resolver = None

def set__daemon_enabled_resolver(resolver):
    global __daemon_enabled_resolver
    with _execution_accessor_lock:
        __daemon_enabled_resolver = resolver

def resolve__daemon_enabled(*args, **kwargs):
    resolver = __daemon_enabled_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._daemon_enabled resolver not wired")
    return resolver(*args, **kwargs)

__mcp_disable_flags_resolver = None

def set__mcp_disable_flags_resolver(resolver):
    global __mcp_disable_flags_resolver
    with _execution_accessor_lock:
        __mcp_disable_flags_resolver = resolver

def resolve__mcp_disable_flags(*args, **kwargs):
    resolver = __mcp_disable_flags_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._mcp_disable_flags resolver not wired")
    return resolver(*args, **kwargs)

__plugin_disable_flags_resolver = None

def set__plugin_disable_flags_resolver(resolver):
    global __plugin_disable_flags_resolver
    with _execution_accessor_lock:
        __plugin_disable_flags_resolver = resolver

def resolve__plugin_disable_flags(*args, **kwargs):
    resolver = __plugin_disable_flags_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._plugin_disable_flags resolver not wired")
    return resolver(*args, **kwargs)

_capture_execution_codex_thread_resolver = None

def set_capture_execution_codex_thread_resolver(resolver):
    global _capture_execution_codex_thread_resolver
    with _execution_accessor_lock:
        _capture_execution_codex_thread_resolver = resolver

def resolve_capture_execution_codex_thread(*args, **kwargs):
    resolver = _capture_execution_codex_thread_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.capture_execution_codex_thread resolver not wired")
    return resolver(*args, **kwargs)

_codex_daemon_worker_home_resolver = None

def set_codex_daemon_worker_home_resolver(resolver):
    global _codex_daemon_worker_home_resolver
    with _execution_accessor_lock:
        _codex_daemon_worker_home_resolver = resolver

def resolve_codex_daemon_worker_home(*args, **kwargs):
    resolver = _codex_daemon_worker_home_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.codex_daemon_worker_home resolver not wired")
    return resolver(*args, **kwargs)

_hold_execution_codex_session_resolver = None

def set_hold_execution_codex_session_resolver(resolver):
    global _hold_execution_codex_session_resolver
    with _execution_accessor_lock:
        _hold_execution_codex_session_resolver = resolver

def resolve_hold_execution_codex_session(*args, **kwargs):
    resolver = _hold_execution_codex_session_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.hold_execution_codex_session resolver not wired")
    return resolver(*args, **kwargs)

_reap_execution_codex_sessions_resolver = None

def set_reap_execution_codex_sessions_resolver(resolver):
    global _reap_execution_codex_sessions_resolver
    with _execution_accessor_lock:
        _reap_execution_codex_sessions_resolver = resolver

def resolve_reap_execution_codex_sessions(*args, **kwargs):
    resolver = _reap_execution_codex_sessions_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.reap_execution_codex_sessions resolver not wired")
    return resolver(*args, **kwargs)

_run_cold_prompt_resolver = None

def set_run_cold_prompt_resolver(resolver):
    global _run_cold_prompt_resolver
    with _execution_accessor_lock:
        _run_cold_prompt_resolver = resolver

def resolve_run_cold_prompt(*args, **kwargs):
    resolver = _run_cold_prompt_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.run_cold_prompt resolver not wired")
    return resolver(*args, **kwargs)

_settle_execution_codex_session_resolver = None

def set_settle_execution_codex_session_resolver(resolver):
    global _settle_execution_codex_session_resolver
    with _execution_accessor_lock:
        _settle_execution_codex_session_resolver = resolver

def resolve_settle_execution_codex_session(*args, **kwargs):
    resolver = _settle_execution_codex_session_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.settle_execution_codex_session resolver not wired")
    return resolver(*args, **kwargs)

_cross_check_openrouter_resolver = None

def set_cross_check_openrouter_resolver(resolver):
    global _cross_check_openrouter_resolver
    with _execution_accessor_lock:
        _cross_check_openrouter_resolver = resolver

def resolve_cross_check_openrouter(*args, **kwargs):
    resolver = _cross_check_openrouter_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.cross_check_openrouter resolver not wired")
    return resolver(*args, **kwargs)

__count_tokens_resolver = None

def set__count_tokens_resolver(resolver):
    global __count_tokens_resolver
    with _execution_accessor_lock:
        __count_tokens_resolver = resolver

def resolve__count_tokens(*args, **kwargs):
    resolver = __count_tokens_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._count_tokens resolver not wired")
    return resolver(*args, **kwargs)

_submit_chat_tasks_resolver = None

def set_submit_chat_tasks_resolver(resolver):
    global _submit_chat_tasks_resolver
    with _execution_accessor_lock:
        _submit_chat_tasks_resolver = resolver

def resolve_submit_chat_tasks(*args, **kwargs):
    resolver = _submit_chat_tasks_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.submit_chat_tasks resolver not wired")
    return resolver(*args, **kwargs)

_submit_message_batch_resolver = None

def set_submit_message_batch_resolver(resolver):
    global _submit_message_batch_resolver
    with _execution_accessor_lock:
        _submit_message_batch_resolver = resolver

def resolve_submit_message_batch(*args, **kwargs):
    resolver = _submit_message_batch_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.submit_message_batch resolver not wired")
    return resolver(*args, **kwargs)

__classify_resolver = None

def set__classify_resolver(resolver):
    global __classify_resolver
    with _execution_accessor_lock:
        __classify_resolver = resolver

def resolve__classify(*args, **kwargs):
    resolver = __classify_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors._classify resolver not wired")
    return resolver(*args, **kwargs)

_caps_resolver = None

def set_caps_resolver(resolver):
    global _caps_resolver
    with _execution_accessor_lock:
        _caps_resolver = resolver

def resolve_caps(*args, **kwargs):
    resolver = _caps_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.caps resolver not wired")
    return resolver(*args, **kwargs)

_closest_served_resolver = None

def set_closest_served_resolver(resolver):
    global _closest_served_resolver
    with _execution_accessor_lock:
        _closest_served_resolver = resolver

def resolve_closest_served(*args, **kwargs):
    resolver = _closest_served_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.closest_served resolver not wired")
    return resolver(*args, **kwargs)

_consensus_resolver = None

def set_consensus_resolver(resolver):
    global _consensus_resolver
    with _execution_accessor_lock:
        _consensus_resolver = resolver

def resolve_consensus(*args, **kwargs):
    resolver = _consensus_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.consensus resolver not wired")
    return resolver(*args, **kwargs)

_fan_out_resolver = None

def set_fan_out_resolver(resolver):
    global _fan_out_resolver
    with _execution_accessor_lock:
        _fan_out_resolver = resolver

def resolve_fan_out(*args, **kwargs):
    resolver = _fan_out_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.fan_out resolver not wired")
    return resolver(*args, **kwargs)

_first_ok_resolver = None

def set_first_ok_resolver(resolver):
    global _first_ok_resolver
    with _execution_accessor_lock:
        _first_ok_resolver = resolver

def resolve_first_ok(*args, **kwargs):
    resolver = _first_ok_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.first_ok resolver not wired")
    return resolver(*args, **kwargs)

_list_models_resolver = None

def set_list_models_resolver(resolver):
    global _list_models_resolver
    with _execution_accessor_lock:
        _list_models_resolver = resolver

def resolve_list_models(*args, **kwargs):
    resolver = _list_models_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.list_models resolver not wired")
    return resolver(*args, **kwargs)

_served_check_resolver = None

def set_served_check_resolver(resolver):
    global _served_check_resolver
    with _execution_accessor_lock:
        _served_check_resolver = resolver

def resolve_served_check(*args, **kwargs):
    resolver = _served_check_resolver
    if resolver is None:
        raise RuntimeError("execution_accessors.served_check resolver not wired")
    return resolver(*args, **kwargs)
