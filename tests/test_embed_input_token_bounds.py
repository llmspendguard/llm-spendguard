"""Embedding input bounds are catalog-token based, with a configured conservative character fallback; offline/$0."""
import os
import sys
import tempfile
from types import SimpleNamespace

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-embed-input-bound-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ["OPENAI_API_KEY"] = "sk-test-offline-embed-input-bound"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard import adapters, config, model_catalog
from spendguard.analyze.cost_estimation import provider_tokens
from spendguard.generate.execution import embeddings, providers


class _InputBoundRecordingEndpoint:
    def __init__(self, calls):
        self.calls = calls

    def create(self, model, input, **kwargs):
        self.calls.append(list(input))
        return SimpleNamespace(data=[SimpleNamespace(index=index, embedding=[1.0])
                                     for index, _text in enumerate(input)])


class _InputBoundRecordingClient:
    def __init__(self, calls):
        self.embeddings = _InputBoundRecordingEndpoint(calls)


def main():
    failures = []
    model = "text-embedding-3-small"
    qualified_model = f"openai:{model}"
    token_limit = model_catalog.embedding_input_token_limit(model)
    published_limits = embeddings.embedding_input_limits(model)
    record_script_check(failures, "embedding token limit is catalog-sourced", token_limit is not None)
    record_script_check(failures, "chat models have no embedding token limit",
                        model_catalog.embedding_input_token_limit("gpt-5") is None)
    record_script_check(failures, "uncurated model publishes no enforced character limit",
                        embeddings.embedding_input_limits("uncurated-embedding-model")["char_limit"] is None)

    real_client = embeddings._oai_compat_client
    real_counter = provider_tokens.count_exact_model_text_tokens
    real_cfg_get = config._cfg_get
    calls = []
    try:
        embeddings._oai_compat_client = lambda provider, timeout_s=None: _InputBoundRecordingClient(calls)

        dense = "漢" * (token_limit + 1)
        light = "a" * (token_limit * 4 + 1)
        provider_tokens.count_exact_model_text_tokens = lambda text, provider=None, model=None: (
            token_limit + 1 if text == dense else token_limit - 1
        )
        dense_tokens = provider_tokens.count_exact_model_text_tokens(dense, provider="openai", model=model)
        record_script_check(failures, "dense fixture exceeds the catalog token limit",
                            dense_tokens is not None and dense_tokens > token_limit)
        dense_result = adapters.embed([dense], model=qualified_model, checkpoint=False)
        record_script_check(failures, "token-dense input is rejected before embeddings.create",
                            not calls and dense_result["failed"] and "tokens" in dense_result["failed"][0]["reason"])

        light_tokens = provider_tokens.count_exact_model_text_tokens(light, provider="openai", model=model)
        record_script_check(failures, "long character fixture remains under the token limit",
                            len(light) > token_limit * 3 and light_tokens is not None and light_tokens < token_limit)
        light_result = adapters.embed([light], model=qualified_model, checkpoint=False)
        record_script_check(failures, "token-light long input is sent rather than character-pre-rejected",
                            len(calls) == 1 and light_result["vectors"][0] == [1.0])

        provider_tokens.count_exact_model_text_tokens = lambda text, provider=None, model=None: None
        config._cfg_get = lambda section, key, default=None: (
            1.0 if (section, key) == ("safety", "embed_worst_case_chars_per_token")
            else real_cfg_get(section, key, default)
        )
        published_limits = embeddings.embedding_input_limits(model)
        boundary_text = "b" * published_limits["char_limit"]
        fallback_text = boundary_text + "b"
        calls.clear()
        boundary = adapters.embed([boundary_text], model=qualified_model, checkpoint=False)
        record_script_check(failures, "published character limit is the exact admitted guard boundary",
                            len(calls) == 1 and boundary["vectors"][0] == [1.0])
        calls.clear()
        rejected = adapters.embed([fallback_text], model=qualified_model, checkpoint=False)
        record_script_check(failures, "guard raises immediately beyond the published character limit",
                            not calls and rejected["failed"] and
                            str(published_limits["char_limit"]) in rejected["failed"][0]["reason"])

        config._cfg_get = lambda section, key, default=None: (
            2.0 if (section, key) == ("safety", "embed_worst_case_chars_per_token")
            else real_cfg_get(section, key, default)
        )
        overridden_limits = embeddings.embedding_input_limits(model)
        calls.clear()
        admitted = adapters.embed([fallback_text], model=qualified_model, checkpoint=False)
        record_script_check(failures, "config override moves published and enforced bounds together",
                            overridden_limits["char_limit"] > published_limits["char_limit"] and
                            len(calls) == 1 and admitted["vectors"][0] == [1.0])

        sized_chars = providers.safe_input_chars(model, reply_tokens=0, prompt_tokens=0)
        record_script_check(failures, "embedding sizing has no chat reply/scaffold subtraction",
                            sized_chars is not None and sized_chars > 0)
    finally:
        embeddings._oai_compat_client = real_client
        provider_tokens.count_exact_model_text_tokens = real_counter
        config._cfg_get = real_cfg_get

    print(f"\n{'[FAIL]' if failures else 'OK'} test_embed_input_token_bounds: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
