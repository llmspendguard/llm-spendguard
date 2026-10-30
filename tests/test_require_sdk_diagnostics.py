"""Pin require()'s operator-facing identity for each actually unpatched SDK surface."""
import os
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


REQUIRE_DIAGNOSTICS_HOME_PREFIX = "spendguard-require-diagnostics-"

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix=REQUIRE_DIAGNOSTICS_HOME_PREFIX)
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import gate  # noqa: E402


def test_require_identifies_all_unpatched_sdk_clients():
    core_packages = gate.core_gated_sdk_packages()
    interceptor_specs = gate.INTERCEPTORS + gate._EXTRA + gate.RT_INTERCEPTORS
    chosen_specs = [next(spec for spec in interceptor_specs
                         if spec[0].partition(".")[0] == package)
                    for package in core_packages]
    unpatched_modules = {package: ModuleType(package) for package in core_packages}
    expected_clients = []
    for module_name, client_name, method_name, *_ in chosen_specs:
        sdk_module = unpatched_modules.setdefault(module_name, ModuleType(module_name))
        setattr(sdk_module, client_name, SimpleNamespace(**{method_name: object()}))
        expected_clients.append(f"{module_name}.{client_name}.{method_name}")
    with (
        patch.dict(sys.modules, unpatched_modules),
        patch.object(gate, "gated_sdk_modules", return_value=core_packages),
        patch.object(gate, "install"),
        patch.object(gate, "_disabled", return_value=False),
    ):
        assert not gate._enforcement_armed()
        try:
            gate.require()
        except gate.SpendGateRefused as refusal:
            refusal_message = str(refusal)
        else:
            raise AssertionError("require accepted installed clients with missing interceptors")
    for expected_client in expected_clients:
        assert expected_client in refusal_message, refusal_message
    assert "spendguard doctor" in refusal_message
    assert "gated venv" in refusal_message


test_require_identifies_all_unpatched_sdk_clients()
print("OK test_require_sdk_diagnostics: all unpatched clients named with remediation")
