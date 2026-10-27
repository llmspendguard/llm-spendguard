"""Fresh interpreter contract: heavy packages remain conditional.

The contract is set membership, not elapsed time. Subprocess wall-clock timing on
shared CI runners conflates cold-start .pyc compilation and disk I/O with eager
imports, causing false failures. The process deadline remains solely a hang guard.
"""
import json
import os
import subprocess
import sys

LIBRARY_IMPORT_HEAVY_PACKAGES = frozenset({
    "openai", "anthropic", "rich", "pygments", "pandas", "numpy", "torch",
    "httpx", "requests", "urllib3", "charset_normalizer", "click",
})
LIBRARY_IMPORT_PROCESS_DEADLINE_S = 60
LIBRARY_IMPORT_PROBE_SOURCE = """
import json
import sys
import spendguard
spendguard.install()
print(json.dumps(sorted(sys.modules)))
"""


def verify_library_import_dependency_budget():
    """Verify install leaves heavy conditional dependencies unloaded."""
    import_probe_env = dict(os.environ)
    import_probe_env.pop("GATE_DISABLE", None)
    import_probe_env.pop("SPENDGUARD_NO_AUTOINSTALL", None)
    import_probe = subprocess.run(
        [sys.executable, "-c", LIBRARY_IMPORT_PROBE_SOURCE],
        env=import_probe_env, capture_output=True, text=True, check=True,
        timeout=LIBRARY_IMPORT_PROCESS_DEADLINE_S,
    )
    import_probe_modules = set(json.loads(import_probe.stdout))
    import_probe_heavy = LIBRARY_IMPORT_HEAVY_PACKAGES.intersection(import_probe_modules)
    assert not import_probe_heavy, f"Heavy eager imports: {sorted(import_probe_heavy)}"
    print("Library import contract: heavy dependencies absent from a fresh install() launch")


verify_library_import_dependency_budget()
