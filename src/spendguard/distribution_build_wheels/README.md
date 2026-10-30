# Offline distribution build backend

The wheels in this directory supply `pyproject.toml`'s `build-system.requires`
to the nonisolated fleet wheel builder. They are shipped in both the sdist and
wheel through `tool.setuptools.package-data`. The runtime installs only from
this directory with `--no-index`; a missing or incompatible backend fails closed.
The bundled setuptools includes its own wheel-building command and dependencies.
Upstream license files are retained inside the unmodified wheel.

To refresh, use a network-enabled development interpreter to download binary
wheels for every requirement read from `build-system.requires`, including their
dependencies, into a temporary directory (`python -m pip download --only-binary=:all:`).
Replace this directory's wheels with that complete set, then run the catalog
distribution guard with `CATALOG_WHEEL_BUILD_PYTHON` selecting each supported
Python interpreter. Never derive backend versions from the host's installed tools.

The guard begins without setuptools or wheel, covers pip present and absent,
disables indexes and proxies, and verifies that the resulting distribution
contains these backend wheels so an installed copy can provision the next build.
