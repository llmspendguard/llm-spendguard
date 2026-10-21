"""Real SDK import boundaries, offline bookkeeping, and import-loader fault containment.

Each scenario owns a fresh interpreter. Only the SDK transport is replaced; gate
wrappers, prechecks, pricing, and SQLite recording remain real. No network is used.
"""
import importlib
import importlib.machinery
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
from unittest import mock


LAZY_SDK_SCENARIO_ENV = "SPENDGUARD_LAZY_SDK_SCENARIO"
LAZY_SDK_SCENARIOS = ("after", "before", "strict", "disarmed", "concurrent", "interrupted",
                      "patch_interrupted", "initializing_before", "importer_compatibility")
LAZY_SDK_PACKAGES = ("openai", "anthropic")
LAZY_SDK_CHILD_TIMEOUT_S = 60
LAZY_SDK_THREAD_TIMEOUT_S = 20
LAZY_SDK_WAITERS = 4
LAZY_SDK_INPUT_TOKENS = 17
LAZY_SDK_OUTPUT_TOKENS = 5
LAZY_SDK_OUTPUT_LIMIT = 10
LAZY_SDK_MODELS = {"openai": "gpt-5.5", "anthropic": "claude-sonnet-4-6"}
LAZY_SDK_TEST_INTENT = "lazy-sdk-enforcement-offline"
LAZY_SDK_OFFLINE_KEY = "offline-transport-stub"
LAZY_SDK_TMP_PREFIX = "lazy-sdk-enforcement-"
LAZY_SDK_LEGACY_PACKAGE = "spendguard_lazy_legacy_fixture"
LAZY_SDK_NAMESPACE_PACKAGE = "spendguard_lazy_namespace_fixture"
LAZY_SDK_SYNTHETIC_READY_ATTR = "lazy_sdk_fixture_ready"
LAZY_SDK_SYNTHETIC_PATCHED_ATTR = "lazy_sdk_fixture_patched"


def assert_lazy_sdk_surface_markers(gate_module):
    """Every available registered surface must be patched, not merely one sentinel."""
    for spec in (gate_module.INTERCEPTORS + gate_module._EXTRA + gate_module.RT_INTERCEPTORS
                 + gate_module.STREAM_INTERCEPTORS + gate_module.UNIT_INTERCEPTORS):
        sdk_class = getattr(importlib.import_module(spec[0]), spec[1])
        sdk_method = getattr(sdk_class, spec[2], None)
        if sdk_method is not None:
            assert getattr(sdk_method, "_spend_gated", False), spec[:3]


def book_lazy_sdk_offline_calls(spendguard_module):
    """Stub _post below the actual SDK create wrapper, then inspect the actual ledger."""
    from spendguard import config, gate, http_capture, pricing

    spendguard_module.require()
    expected = []
    for sdk_package in LAZY_SDK_PACKAGES:
        sdk_module = importlib.import_module(sdk_package)
        model = LAZY_SDK_MODELS[sdk_package]
        if sdk_package == "openai":
            client = sdk_module.OpenAI(api_key=LAZY_SDK_OFFLINE_KEY)
            resource = client.chat.completions
            usage = SimpleNamespace(prompt_tokens=LAZY_SDK_INPUT_TOKENS,
                                    completion_tokens=LAZY_SDK_OUTPUT_TOKENS)
            result = SimpleNamespace(model=model, usage=usage, choices=[])
        else:
            client = sdk_module.Anthropic(api_key=LAZY_SDK_OFFLINE_KEY)
            resource = client.messages
            usage = SimpleNamespace(input_tokens=LAZY_SDK_INPUT_TOKENS,
                                    output_tokens=LAZY_SDK_OUTPUT_TOKENS)
            result = SimpleNamespace(model=model, usage=usage, content=[], stop_reason="end_turn")

        def lazy_sdk_stub_post(*args, _result=result, **kwargs):
            assert http_capture.in_sdk_call.get(), "call bypassed SDK gate wrapper"
            return _result

        with mock.patch.object(resource, "_post", side_effect=lazy_sdk_stub_post) as transport:
            with spendguard_module.context(intent=LAZY_SDK_TEST_INTENT):
                returned = resource.create(model=model, messages=[], max_tokens=LAZY_SDK_OUTPUT_LIMIT)
            assert returned is result
            transport.assert_called_once()
            transport.reset_mock()
            with mock.patch.object(gate, "_rt_precheck", side_effect=gate.SpendGateRefused("injected refusal")):
                try:
                    resource.create(model=model, messages=[], max_tokens=LAZY_SDK_OUTPUT_LIMIT)
                except gate.SpendGateRefused:
                    pass
                else:
                    raise AssertionError("SDK wrapper swallowed deliberate budget refusal")
            transport.assert_not_called()
        client.close()
        expected.append((sdk_package, model, LAZY_SDK_INPUT_TOKENS, LAZY_SDK_OUTPUT_TOKENS,
                         pricing.realtime_cost(model, LAZY_SDK_INPUT_TOKENS, LAZY_SDK_OUTPUT_TOKENS)))
    with sqlite3.connect(config.db_path()) as ledger:
        actual = ledger.execute(
            "SELECT provider,model,in_tok,out_tok,cost FROM calls WHERE intent=? ORDER BY rowid",
            (LAZY_SDK_TEST_INTENT,)).fetchall()
    assert actual == expected, (actual, expected)


def exercise_lazy_sdk_loader_fault(spendguard_module, interrupted):
    """Delay real package initialization while callers race, or interrupt it and retry."""
    original_find_spec = importlib.machinery.PathFinder.find_spec
    entered = threading.Event()
    release = threading.Event()
    finished = []
    errors = []
    fault_pending = True

    def lazy_sdk_fault_find_spec(fullname, path=None, target=None):
        nonlocal fault_pending
        spec = original_find_spec(fullname, path, target)
        if fullname == LAZY_SDK_PACKAGES[0] and fault_pending and spec is not None:
            fault_pending = False
            original_exec = spec.loader.exec_module

            def lazy_sdk_fault_exec(module):
                entered.set()
                if interrupted:
                    raise KeyboardInterrupt("injected SDK loader interruption")
                assert release.wait(LAZY_SDK_THREAD_TIMEOUT_S), "loader release deadline"
                return original_exec(module)

            spec.loader.exec_module = lazy_sdk_fault_exec
        return spec

    def lazy_sdk_import_waiter():
        try:
            importlib.import_module(LAZY_SDK_PACKAGES[0])
            from spendguard import gate
            assert_lazy_sdk_surface_markers(gate)
            finished.append(True)
        except BaseException as error:
            errors.append(error)

    with mock.patch.object(importlib.machinery.PathFinder, "find_spec", side_effect=lazy_sdk_fault_find_spec):
        if interrupted:
            try:
                importlib.import_module(LAZY_SDK_PACKAGES[0])
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("loader interruption did not propagate")
            assert LAZY_SDK_PACKAGES[0] not in sys.modules
            spendguard_module.require()
            importlib.import_module(LAZY_SDK_PACKAGES[0])
        else:
            threads = [threading.Thread(target=lazy_sdk_import_waiter) for _ in range(LAZY_SDK_WAITERS)]
            threads[0].start()
            assert entered.wait(LAZY_SDK_THREAD_TIMEOUT_S), "SDK loader did not enter"
            for thread in threads[1:]:
                thread.start()
            try:
                assert not finished, "SDK import escaped before initialization completed"
                spendguard_module.install()
                assert not finished, "install released an unpatched in-progress SDK"
            finally:
                release.set()
            for thread in threads:
                thread.join(LAZY_SDK_THREAD_TIMEOUT_S)
                assert not thread.is_alive(), "SDK concurrent import deadlocked"
            assert not errors, errors
            assert len(finished) == LAZY_SDK_WAITERS


def exercise_lazy_sdk_unwrapped_initialization():
    """Installing on a same-thread, already running vendor loader cannot claim armed."""
    original_find_spec = importlib.machinery.PathFinder.find_spec
    intercepted = False

    def lazy_sdk_preinstall_find_spec(fullname, path=None, target=None):
        nonlocal intercepted
        spec = original_find_spec(fullname, path, target)
        if fullname == LAZY_SDK_PACKAGES[0] and not intercepted and spec is not None:
            intercepted = True
            original_exec = spec.loader.exec_module

            def lazy_sdk_preinstall_exec(module):
                import spendguard
                from spendguard import gate
                try:
                    spendguard.install()
                except gate.SpendGateRefused:
                    pass
                else:
                    raise AssertionError("in-progress unwrapped SDK import was accepted")
                assert not gate._enforcement_armed(), "unwrapped initializing loader claimed enforcement"
                return original_exec(module)

            spec.loader.exec_module = lazy_sdk_preinstall_exec
        return spec

    with mock.patch.object(importlib.machinery.PathFinder, "find_spec", side_effect=lazy_sdk_preinstall_find_spec):
        importlib.import_module(LAZY_SDK_PACKAGES[0])
    assert intercepted


def exercise_lazy_sdk_importer_compatibility(gate_module):
    """Complete one-shot callbacks for legacy replacement modules and namespace packages."""
    completed = []

    def lazy_sdk_legacy_load_module(fullname):
        module = ModuleType(fullname)
        setattr(module, LAZY_SDK_SYNTHETIC_READY_ATTR, True)
        sys.modules[fullname] = module
        return module

    legacy_loader = SimpleNamespace(load_module=lazy_sdk_legacy_load_module)

    def lazy_sdk_legacy_find_module(fullname, path=None):
        return legacy_loader if fullname == LAZY_SDK_LEGACY_PACKAGE else None

    def lazy_sdk_namespace_find_spec(fullname, path=None, target=None):
        if fullname == LAZY_SDK_NAMESPACE_PACKAGE:
            return importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
        return None

    def lazy_sdk_fixture_callback(module):
        assert module is sys.modules[module.__name__], "callback received replaced legacy module"
        if module.__name__ == LAZY_SDK_LEGACY_PACKAGE:
            assert getattr(module, LAZY_SDK_SYNTHETIC_READY_ATTR)
        setattr(module, LAZY_SDK_SYNTHETIC_PATCHED_ATTR, True)
        completed.append(module)

    importers = (SimpleNamespace(find_module=lazy_sdk_legacy_find_module),
                 SimpleNamespace(find_spec=lazy_sdk_namespace_find_spec))
    for importer in importers:
        sys.meta_path.append(importer)
    try:
        for package in (LAZY_SDK_LEGACY_PACKAGE, LAZY_SDK_NAMESPACE_PACKAGE):
            gate_module._register_gate_package_postimport(package, lazy_sdk_fixture_callback)
            module = importlib.import_module(package)
            assert getattr(module, LAZY_SDK_SYNTHETIC_PATCHED_ATTR), package
            assert importlib.import_module(package) is module
            assert completed.count(module) == len((module,)), "callback was not exactly once"
            assert package not in gate_module._gate_package_import_finder.callbacks
    finally:
        for importer in importers:
            sys.meta_path.remove(importer)
        for package in (LAZY_SDK_LEGACY_PACKAGE, LAZY_SDK_NAMESPACE_PACKAGE):
            sys.modules.pop(package, None)


def run_lazy_sdk_enforcement_scenario(scenario):
    # Disable only interpreter-startup installation so preimport ordering can be
    # observed. Before any mock SDK call require() reestablishes real enforcement.
    os.environ.pop("GATE_DISABLE", None)
    os.environ["SPENDGUARD_CALLS"] = "1"
    if scenario == "strict":
        os.environ.pop("SPENDGUARD_NO_AUTOINSTALL", None)
        os.environ["SPENDGUARD_REQUIRE"] = "1"
    if scenario == "before":
        for sdk_package in LAZY_SDK_PACKAGES:
            importlib.import_module(sdk_package)
    if scenario == "initializing_before":
        exercise_lazy_sdk_unwrapped_initialization()
    import spendguard
    from spendguard import gate
    spendguard.install()
    if scenario not in ("before", "initializing_before"):
        assert all(sdk_package not in sys.modules for sdk_package in LAZY_SDK_PACKAGES)
    if scenario == "disarmed":
        # Remove actual import hooks, and prevent require's reinstall from
        # repairing the injected bypass: an importable SDK must now refuse.
        sys.meta_path[:] = [finder for finder in sys.meta_path
                            if not type(finder).__module__.startswith("spendguard")]
        with mock.patch.object(gate, "install"):
            try:
                spendguard.require()
            except gate.SpendGateRefused:
                return
            raise AssertionError("disarmed importable SDK was accepted")
    spendguard.require()
    if scenario == "importer_compatibility":
        exercise_lazy_sdk_importer_compatibility(gate)
    import httpx
    import requests
    assert getattr(httpx.Client.send, "_spend_gated", False)
    assert getattr(httpx.AsyncClient.send, "_spend_gated", False)
    assert getattr(requests.Session.send, "_spend_gated", False)
    if scenario in ("concurrent", "interrupted"):
        exercise_lazy_sdk_loader_fault(spendguard, interrupted=scenario == "interrupted")
    if scenario == "patch_interrupted":
        with mock.patch.object(gate, "_patch_sdk_package_surfaces", side_effect=KeyboardInterrupt("patch interrupted")):
            try:
                importlib.import_module(LAZY_SDK_PACKAGES[0])
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("patch interruption did not abort SDK import")
        assert LAZY_SDK_PACKAGES[0] not in sys.modules
        spendguard.require()
    for sdk_package in LAZY_SDK_PACKAGES:
        importlib.import_module(sdk_package)
    assert_lazy_sdk_surface_markers(gate)
    from openai.resources.chat.completions import Completions
    first_wrapper = Completions.create
    spendguard.install()
    spendguard.require()
    assert Completions.create is first_wrapper, "reinstall nested the wrapper"
    book_lazy_sdk_offline_calls(spendguard)


def launch_lazy_sdk_enforcement_children():
    scenario = os.environ.get(LAZY_SDK_SCENARIO_ENV)
    if scenario:
        run_lazy_sdk_enforcement_scenario(scenario)
        return
    for scenario in LAZY_SDK_SCENARIOS:
        with tempfile.TemporaryDirectory(prefix=LAZY_SDK_TMP_PREFIX) as home:
            child_env = dict(os.environ, GATE_DISABLE="1", SPENDGUARD_NO_AUTOINSTALL="1",
                             SPENDGUARD_HOME=home, SPENDGUARD_REQUIRE="0")
            child_env[LAZY_SDK_SCENARIO_ENV] = scenario
            result = subprocess.run([sys.executable, str(Path(__file__).resolve())], env=child_env,
                                    capture_output=True, text=True, timeout=LAZY_SDK_CHILD_TIMEOUT_S)
            assert result.returncode == 0, f"{scenario}:\n{result.stdout}\n{result.stderr}"
            print(f"[OK] lazy SDK enforcement: {scenario}")


if __name__ == "__main__":
    launch_lazy_sdk_enforcement_children()
