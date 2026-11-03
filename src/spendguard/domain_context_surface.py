"""Install a context package's published language without eager backing imports."""

from importlib import import_module
from types import MappingProxyType


DOMAIN_EXPORT_SEPARATOR = ":"
PRIVATE_DOMAIN_NAME_PREFIX = "_"


def install_domain_context_surface(module_namespace, export_targets):
    """Install a PEP-562 surface backed by ``module:attribute`` target strings."""
    normalized_targets = MappingProxyType(dict(export_targets))
    private_names = sorted(
        published_name
        for published_name in normalized_targets
        if published_name.startswith(PRIVATE_DOMAIN_NAME_PREFIX)
    )
    if private_names:
        raise ValueError(f"domain context surfaces cannot publish private names: {private_names}")

    published_names = sorted(normalized_targets)

    def resolve_domain_context_attribute(published_name):
        if published_name not in normalized_targets:
            raise AttributeError(
                f"module {module_namespace.get('__name__')!r} has no attribute {published_name!r}"
            )
        target_specification = normalized_targets[published_name]
        try:
            backing_module_name, backing_attribute_name = target_specification.rsplit(
                DOMAIN_EXPORT_SEPARATOR, 1
            )
            resolved_attribute = getattr(
                import_module(backing_module_name),
                backing_attribute_name,
            )
        except (AttributeError, ImportError, ModuleNotFoundError, ValueError) as error:
            raise AttributeError(
                f"published domain name {published_name!r} cannot resolve {target_specification!r}"
            ) from error
        module_namespace[published_name] = resolved_attribute
        return resolved_attribute

    def list_domain_context_attributes():
        return sorted(set(module_namespace) | set(published_names))

    module_namespace["__all__"] = published_names
    module_namespace["__getattr__"] = resolve_domain_context_attribute
    module_namespace["__dir__"] = list_domain_context_attributes
