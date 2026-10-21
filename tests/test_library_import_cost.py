"""Fresh interpreter import contract: conditional dependencies stay conditional."""
import json
import os
import subprocess
import sys
import time

LIBRARY_IMPORT_HEAVY_PACKAGES = frozenset({
    "openai", "anthropic", "rich", "pygments", "pandas", "numpy", "torch",
    "httpx", "requests", "urllib3", "charset_normalizer", "click",
})
# Seven fresh gated Python 3.11 launches on this branch measured 0.0712–0.0789s.
# Include all startup hooks. A 0.9s margin allows over twelve times the measured
# floor for slow CI, while rejecting the old 1.995–2.544s eager SDK startup.
LIBRARY_IMPORT_MEASURED_FLOOR_S = 0.0712
LIBRARY_IMPORT_CI_MARGIN_S = 0.9
LIBRARY_IMPORT_WALL_BUDGET_S = LIBRARY_IMPORT_MEASURED_FLOOR_S + LIBRARY_IMPORT_CI_MARGIN_S
LIBRARY_IMPORT_PROCESS_DEADLINE_S = 60
LIBRARY_IMPORT_PROBE_SOURCE = """
import json
import sys
import spendguard
spendguard.install()
print(json.dumps(sorted(sys.modules)))
"""


def verify_library_import_dependency_budget():
    """Measure launch through install, including every startup hook and import."""
    import_probe_env = dict(os.environ)
    import_probe_env.pop("GATE_DISABLE", None)
    import_probe_env.pop("SPENDGUARD_NO_AUTOINSTALL", None)
    import_probe_started = time.perf_counter()
    import_probe = subprocess.run(
        [sys.executable, "-c", LIBRARY_IMPORT_PROBE_SOURCE],
        env=import_probe_env, capture_output=True, text=True, check=True,
        timeout=LIBRARY_IMPORT_PROCESS_DEADLINE_S,
    )
    import_probe_elapsed = time.perf_counter() - import_probe_started
    import_probe_modules = set(json.loads(import_probe.stdout))
    import_probe_heavy = LIBRARY_IMPORT_HEAVY_PACKAGES.intersection(import_probe_modules)
    assert not import_probe_heavy, f"Heavy eager imports: {sorted(import_probe_heavy)}"
    assert import_probe_elapsed < LIBRARY_IMPORT_WALL_BUDGET_S, (
        f"Library import took {import_probe_elapsed:.3f}s; "
        f"budget {LIBRARY_IMPORT_WALL_BUDGET_S:.3f}s"
    )
    print(f"Library import contract: heavy dependencies absent; launch+install={import_probe_elapsed:.3f}s")


verify_library_import_dependency_budget()
