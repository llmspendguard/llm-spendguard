"""The version SSOT — the ONE authoring home for llm-spendguard's version string.

`spendguard.__init__` imports `__version__` from here, so the attribute is always the version of the CODE that is
actually running. `pyproject.toml` reads this SAME attribute at build time via
`[tool.setuptools.dynamic] version = {attr = "spendguard._version.__version__"}`. Bump the version by editing the
one line below and nothing else.

Do NOT reintroduce a literal in pyproject, nor a metadata read (`importlib.metadata.version`) in __init__: metadata
returns the INSTALL-TIME (frozen) version, which under an editable install lags the source — it reported 0.7.2 while
0.10.0 code was running (warden 2026-09-25, the "packaging version lie"). Reading this file live removes that lie.
"""
__version__ = "0.11.0"
