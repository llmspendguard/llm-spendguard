"""Dependency-free provider API-key environment registry."""
import threading


BUILTIN_PROVIDER_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "zai": "ZAI_API_KEY",
    "voyage": "VOYAGE_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
}

_runtime_provider_key_env = {}
_runtime_provider_key_env_lock = threading.Lock()


def register_runtime_provider_key_env(provider, key_env):
    """Register or replace the API-key environment name for a runtime provider."""
    with _runtime_provider_key_env_lock:
        _runtime_provider_key_env[provider.lower()] = key_env


def resolve_provider_key_env(provider):
    """Return the API-key environment name for a provider, if registered."""
    provider_key = (provider or "").lower()
    with _runtime_provider_key_env_lock:
        runtime_key_env = _runtime_provider_key_env.get(provider_key)
    return runtime_key_env or BUILTIN_PROVIDER_KEY_ENV.get(provider_key)


def all_provider_key_envs():
    """Return every built-in and runtime provider API-key environment name."""
    with _runtime_provider_key_env_lock:
        runtime_key_envs = set(_runtime_provider_key_env.values())
    return set(BUILTIN_PROVIDER_KEY_ENV.values()) | runtime_key_envs
