"""Every resolver seam retains live dispatch through canonical module attributes."""

import functools
import importlib
import inspect
import os
from pathlib import Path
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
RAW_SEAM_ENVIRONMENT_VARIABLE = "SPENDGUARD_TEST_RAW_ACCESSOR_SEAM"
EXPLICIT_SEAM_MUTATIONS = ("execution", "call_corpus")

sys.path.insert(0, str(SOURCE_ROOT))
for loaded_module_name in tuple(sys.modules):
    if loaded_module_name == "spendguard" or loaded_module_name.startswith("spendguard."):
        del sys.modules[loaded_module_name]
importlib.import_module("spendguard")


def accessor_module_names():
    return tuple(
        ".".join(accessor_path.relative_to(SOURCE_ROOT).with_suffix("").parts)
        for accessor_path in SOURCE_ROOT.glob("spendguard/**/*_accessors.py")
    )


def import_accessor_homes(accessor_name):
    parent_name = accessor_name.rsplit(".", 1)[0]
    accessor_stem = accessor_name.rsplit(".", 1)[1].removesuffix("_accessors")
    bootstrap_name = f"{parent_name}.{accessor_stem}_resolver_bootstrap"
    bootstrap_path = SOURCE_ROOT.joinpath(*bootstrap_name.split(".")).with_suffix(".py")
    if bootstrap_path.exists():
        bootstrap_module = importlib.import_module(bootstrap_name)
        wire_resolvers = getattr(bootstrap_module, f"wire_{accessor_stem}_accessor_resolvers", None)
        if wire_resolvers is not None:
            wire_resolvers()


def registered_resolvers(accessor_module):
    registered = []
    for attribute_name, attribute_value in vars(accessor_module).items():
        if attribute_name.endswith("_resolvers") and isinstance(attribute_value, dict):
            registered.extend(
                (symbol_name, resolver, attribute_value, symbol_name)
                for symbol_name, resolver in tuple(attribute_value.items())
                if callable(resolver)
            )
        elif attribute_name.endswith("_resolver") and not attribute_name.startswith("set_") and callable(attribute_value):
            if getattr(attribute_value, "__name__", None) == attribute_name:
                continue
            symbol_name = attribute_name[:-len("_resolver")].removeprefix("_")
            registered.append((symbol_name, attribute_value, accessor_module.__dict__, attribute_name))
    return registered


def required_call_arguments(resolver):
    positional_arguments = []
    keyword_arguments = {}
    for parameter in inspect.signature(resolver).parameters.values():
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            positional_arguments.append(object())
        elif parameter.kind is parameter.KEYWORD_ONLY:
            keyword_arguments[parameter.name] = object()
    return positional_arguments, keyword_arguments


def assert_live_dispatch(accessor_name, accessor_module, symbol_name, registered_resolver):
    original_resolver = inspect.unwrap(registered_resolver)
    resolver_module_name = getattr(original_resolver, "__module__", None)
    resolver_function_name = getattr(original_resolver, "__name__", None)
    if (
        not inspect.isfunction(original_resolver)
        or resolver_function_name == "<lambda>"
        or getattr(original_resolver, "__qualname__", None) != resolver_function_name
        or resolver_module_name not in sys.modules
    ):
        return False

    observed_calls = []

    def patched_module_function(*args, **kwargs):
        observed_calls.append((args, kwargs))

    producer_module = sys.modules[resolver_module_name]
    original_module_attribute = getattr(producer_module, resolver_function_name)
    setattr(producer_module, resolver_function_name, patched_module_function)
    resolver_error = None
    try:
        resolve_function = getattr(accessor_module, f"resolve_{symbol_name}")
        positional_arguments, keyword_arguments = required_call_arguments(resolve_function)
        resolve_function(*positional_arguments, **keyword_arguments)
    except Exception as error:
        resolver_error = error
    finally:
        setattr(producer_module, resolver_function_name, original_module_attribute)
    assert len(observed_calls) == 1, (
        f"{accessor_name}.resolve_{symbol_name} did not dispatch through "
        f"{resolver_module_name}.{resolver_function_name}; resolver error: {resolver_error!r}"
    )
    return True


def helper_probe():
    return "original"


def assert_helper_behavior(helper_name, helper):
    anonymous = lambda: "lambda"

    def closure_factory():
        def closure():
            return "closure"
        return closure

    partial = functools.partial(helper_probe)
    assert helper(anonymous) is anonymous, f"{helper_name} wrapped a lambda"
    closure = closure_factory()
    assert helper(closure) is closure, f"{helper_name} wrapped a closure"
    assert helper(partial) is partial, f"{helper_name} wrapped a partial"
    wrapped = helper(helper_probe)
    module = sys.modules[__name__]
    original_attribute = module.helper_probe
    module.helper_probe = lambda: "patched"
    try:
        assert wrapped() == "patched", f"{helper_name} did not late-bind"
        module.helper_probe = None
        assert wrapped() == "original", f"{helper_name} lost non-callable fallback"
        del module.helper_probe
        assert wrapped() == "original", f"{helper_name} lost missing-attribute fallback"
    finally:
        module.helper_probe = original_attribute


def assert_explicit_setters(accessor_name, accessor_module, mutation_name):
    resolver_entries = {
        symbol_name: (resolver, resolver_container, resolver_key)
        for symbol_name, resolver, resolver_container, resolver_key in registered_resolvers(accessor_module)
    }
    tested_symbols = []
    for setter_name, setter in tuple(vars(accessor_module).items()):
        if not setter_name.startswith("set_") or not setter_name.endswith("_resolver") or not callable(setter):
            continue
        if getattr(setter, "__qualname__", None) != setter_name:
            continue
        symbol_name = setter_name[len("set_"):-len("_resolver")]
        if symbol_name not in resolver_entries or not hasattr(accessor_module, f"resolve_{symbol_name}"):
            continue
        original_resolver, resolver_container, resolver_key = resolver_entries[symbol_name]
        if mutation_name and not tested_symbols:
            resolver_container[resolver_key] = helper_probe
        else:
            setter(helper_probe)
        observed_calls = []
        module = sys.modules[__name__]
        original_attribute = module.helper_probe
        module.helper_probe = lambda *args, _observed_calls=observed_calls, **kwargs: _observed_calls.append((args, kwargs))
        try:
            resolve_function = getattr(accessor_module, f"resolve_{symbol_name}")
            positional_arguments, keyword_arguments = required_call_arguments(resolve_function)
            resolve_function(*positional_arguments, **keyword_arguments)
        finally:
            module.helper_probe = original_attribute
            resolver_container[resolver_key] = original_resolver
        assert len(observed_calls) == 1, f"{accessor_name}.resolve_{symbol_name} raw-captured {mutation_name or accessor_name}"
        tested_symbols.append(symbol_name)
    return tested_symbols


accessor_modules = {}
for discovered_accessor_name in accessor_module_names():
    import_accessor_homes(discovered_accessor_name)
    accessor_modules[discovered_accessor_name] = importlib.import_module(discovered_accessor_name)

mutation_name = os.environ.get(RAW_SEAM_ENVIRONMENT_VARIABLE)
tested_by_accessor = {}
for discovered_accessor_name, discovered_accessor_module in accessor_modules.items():
    tested_symbols = [
        symbol_name
        for symbol_name, resolver, _, _ in registered_resolvers(discovered_accessor_module)
        if assert_live_dispatch(discovered_accessor_name, discovered_accessor_module, symbol_name, resolver)
    ]
    if tested_symbols:
        tested_by_accessor[discovered_accessor_name] = tested_symbols

for explicit_seam_name in EXPLICIT_SEAM_MUTATIONS:
    explicit_accessor_name = next(
        accessor_name
        for accessor_name in accessor_modules
        if accessor_name.endswith(f".{explicit_seam_name}.{explicit_seam_name}_accessors")
    )
    explicit_tested_symbols = assert_explicit_setters(
        explicit_accessor_name,
        accessor_modules[explicit_accessor_name],
        mutation_name == explicit_seam_name,
    )
    assert explicit_tested_symbols, f"explicit seam not exercised: {explicit_seam_name}"

helper_count = 0
for discovered_accessor_name, discovered_accessor_module in accessor_modules.items():
    for helper_attribute_name, helper_attribute in vars(discovered_accessor_module).items():
        if helper_attribute_name.startswith("_late_bind_") and helper_attribute_name.endswith("_module_function"):
            assert_helper_behavior(f"{discovered_accessor_name}.{helper_attribute_name}", helper_attribute)
            helper_count += 1
assert helper_count, "no late-binding helpers discovered"

if mutation_name:
    raise AssertionError(f"raw-capture mutation unexpectedly survived: {mutation_name}")

for explicit_seam_name in EXPLICIT_SEAM_MUTATIONS:
    mutation_environment = os.environ.copy()
    mutation_environment[RAW_SEAM_ENVIRONMENT_VARIABLE] = explicit_seam_name
    mutation_result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=PROJECT_ROOT,
        env=mutation_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    mutation_output = mutation_result.stdout + mutation_result.stderr
    assert mutation_result.returncode != 0, f"raw-capture mutation stayed green: {explicit_seam_name}"
    assert explicit_seam_name in mutation_output, f"mutation failure did not name {explicit_seam_name}"

sys.exit(0)
