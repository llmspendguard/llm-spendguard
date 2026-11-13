"""Metered readiness uses spendguard's real routing/key seams and never probes; offline/$0."""
import os
import sys
import tempfile
from contextlib import redirect_stdout
from io import StringIO

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-metered-readiness-")
os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check
from spendguard.generate.execution import providers
from spendguard.manage.admission import model_preflight as readiness
from spendguard.store.configuration import config_accessors
from spendguard.store.reference_data import model_catalog


def main():
    failures = []
    provider, model = model_catalog.chat_models()[0]
    qualified_model = f"{provider}:{model}"
    routed_provider = providers.provider_for(qualified_model)
    real_key_resolver = config_accessors._api_key
    real_client_builder = providers._oai_compat_client
    network_attempts = []
    try:
        providers._oai_compat_client = lambda *args, **kwargs: network_attempts.append((args, kwargs))
        config_accessors._api_key = lambda credential_name: "present"
        armed = readiness.metered_call_readiness(qualified_model, intent="offline-readiness-test")
        record_script_check(failures, "known routed model is armed when spendguard resolves its credential",
                            armed["armed"] and armed["provider"] == routed_provider)

        config_accessors._api_key = lambda credential_name: None
        unarmed = readiness.metered_call_readiness(qualified_model, intent="offline-readiness-test")
        record_script_check(failures, "known model without credential names the routed provider",
                            not unarmed["armed"] and unarmed["status"] == "credential-missing" and
                            routed_provider in unarmed["reason"])
        with redirect_stdout(StringIO()):
            unarmed_exit = readiness.run_metered_call_readiness(
                qualified_model, intent="offline-readiness-test")
        record_script_check(failures, "CLI readiness fails closed when the credential is absent",
                            unarmed_exit == readiness.METERED_READINESS_UNARMED_EXIT)

        config_accessors._api_key = lambda credential_name: "present"
        with redirect_stdout(StringIO()):
            armed_exit = readiness.run_metered_call_readiness(
                qualified_model, intent="offline-readiness-test")
        record_script_check(failures, "CLI readiness succeeds only when the credential is armed",
                            armed_exit == readiness.METERED_READINESS_ARMED_EXIT)

        unknown = readiness.metered_call_readiness("provider-without-catalog-model:absent-model")
        record_script_check(failures, "unknown model is distinct from known unarmed model",
                            not unknown["armed"] and unknown["status"] == "unknown-model" and
                            unknown["status"] != unarmed["status"])
        record_script_check(failures, "armed and unarmed readiness perform no provider probe",
                            network_attempts == [])
    finally:
        config_accessors._api_key = real_key_resolver
        providers._oai_compat_client = real_client_builder

    print(f"\n{'[FAIL]' if failures else 'OK'} test_metered_call_readiness: {len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
