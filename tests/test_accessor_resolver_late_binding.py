"""Cross-domain accessors retain live dispatch through canonical module attributes."""

import importlib
import inspect


ACCESSOR_SEAM_SPECS = (
    (
        "spendguard.manage.routing.routing_accessors",
        "_routing_resolvers",
        ("spendguard.manage.routing.lane_balance",),
    ),
    (
        "spendguard.manage.token_operations.token_operations_accessors",
        "_token_operations_resolvers",
        (
            "spendguard.manage.token_operations.codex_broker_owner",
            "spendguard.manage.token_operations.codex_daemon",
            "spendguard.manage.token_operations.delegate_jobs",
            "spendguard.manage.token_operations.delegate_router",
            "spendguard.manage.token_operations.lane_queue",
        ),
    ),
)


for accessor_module_name, registry_name, producer_module_names in ACCESSOR_SEAM_SPECS:
    for producer_module_name in producer_module_names:
        importlib.import_module(producer_module_name)

    accessor_module = importlib.import_module(accessor_module_name)
    resolver_registry = getattr(accessor_module, registry_name)
    tested_symbols = []

    for symbol_name, registered_resolver in tuple(resolver_registry.items()):
        original_resolver = inspect.unwrap(registered_resolver)
        resolver_module_name = getattr(original_resolver, "__module__", None)
        resolver_function_name = getattr(original_resolver, "__name__", None)
        resolver_qualified_name = getattr(original_resolver, "__qualname__", None)
        if (
            not inspect.isfunction(original_resolver)
            or resolver_function_name == "<lambda>"
            or resolver_qualified_name != resolver_function_name
            or resolver_module_name not in producer_module_names
        ):
            continue

        observed_calls = []

        def patched_module_function(
            *args,
            _recorded_calls=observed_calls,
            **kwargs,
        ):
            _recorded_calls.append((args, kwargs))

        producer_module = importlib.import_module(resolver_module_name)
        original_module_attribute = getattr(producer_module, resolver_function_name)
        setattr(producer_module, resolver_function_name, patched_module_function)
        resolver_error = None
        try:
            getattr(accessor_module, f"resolve_{symbol_name}")()
        except Exception as error:  # The dispatch assertion below reports frozen-resolver errors.
            resolver_error = error
        finally:
            setattr(producer_module, resolver_function_name, original_module_attribute)

        assert observed_calls == [((), {})], (
            f"{accessor_module_name}.resolve_{symbol_name} did not dispatch through "
            f"{resolver_module_name}.{resolver_function_name}; resolver error: {resolver_error!r}"
        )
        tested_symbols.append(symbol_name)

    assert tested_symbols, f"{accessor_module_name} exposed no module-function resolvers"
