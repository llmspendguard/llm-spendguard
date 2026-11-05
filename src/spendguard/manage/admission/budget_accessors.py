"""Accessor bridge for cyclic consumers that need budget's per-process attribution tags.

This dependency-free leaf lets a consumer such as ``egress.emit`` read the conversation id and project tag that budget
records (``budget._conv`` / ``budget._project``) without importing ``budget`` and re-creating the import cycle. budget
wires its real functions during its eager import.
"""

_conv_getter = None
_project_getter = None


def set_budget_accessors(conv, project):
    """Injected by budget with live getters over budget._conv / budget._project at its eager import."""
    global _conv_getter, _project_getter
    _conv_getter = conv
    _project_getter = project


def conv_id():
    if _conv_getter is None:
        raise RuntimeError("import spendguard before reading the conversation id so budget can wire its accessors")
    return _conv_getter()


def project_tag():
    if _project_getter is None:
        raise RuntimeError("import spendguard before reading the project tag so budget can wire its accessors")
    return _project_getter()
