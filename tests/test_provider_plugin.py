"""Provider plugin API — the `spendguard.providers` entry-point loader + the conformance kit.
Guards: plugins activate exactly once (idempotent across load() calls); a RAISING plugin is warned +
skipped without breaking the loader, other plugins, or install(); the conformance kit passes for a
well-formed toy provider and FAILS one that never registers. Offline, zero spend."""
import os, sys, tempfile

if not os.environ.get("SPENDGUARD_TEST_ISOLATED"):
    os.environ["SPENDGUARD_TEST_ISOLATED"] = "1"
    os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-plugin-")
    os.execv(sys.executable, [sys.executable] + sys.argv)

from spendguard import provider_plugins, provider_kit, adapters

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)

class EP:                                      # a fake importlib.metadata entry point
    def __init__(self, name, fn): self.name, self._fn = name, fn
    def load(self): return self._fn

calls = {"good": 0}
def good_activate():
    calls["good"] += 1
    adapters.register_provider("toyprov", "https://api.toyprov.example/v1", "TOYPROV_API_KEY", ("toy-",))
def bad_activate():
    raise RuntimeError("boom")

# ── loader: activation, containment, idempotence ──
status = provider_plugins.load(eps=[EP("good", good_activate), EP("bad", bad_activate)])
ck("good plugin activated", status.get("good") == "ok" and calls["good"] == 1)
ck("bad plugin contained (error status, no raise)", str(status.get("bad", "")).startswith("error:"))
ck("provider actually registered", "toyprov" in adapters.PROVIDERS)
status2 = provider_plugins.load(eps=[EP("good", good_activate)])
ck("second load() is idempotent (no re-activation)", calls["good"] == 1 and status2.get("good") == "ok")
ck("loaded() surfaces both", set(provider_plugins.loaded()) >= {"good", "bad"})

# ── install() survives a raising plugin path end-to-end ──
from spendguard import gate
try:
    gate.install()                              # loads REAL entry points (none installed here) — must not raise
    ck("gate.install() tolerant of plugin loading", True)
except Exception as e:
    ck(f"gate.install() tolerant of plugin loading ({e})", False)

# ── conformance kit: passes for the good provider ──
results = provider_kit.run_conformance(good_activate, name="toyprov", sample_model="gpt-5.5")
ck("kit: all checks pass for a well-formed provider", all(ok for _, ok, _ in results))
ck("kit: covers registers/priced/idempotent/fail_open",
   {c for c, _, _ in results} >= {"activates", "registers", "priced", "idempotent", "fail_open"})

# ── conformance kit: FAILS a provider that never registers ──
results_bad = provider_kit.run_conformance(lambda: None, name="ghost-provider")
ck("kit: catches a provider that never registers", any(c == "registers" and not ok for c, ok, _ in results_bad))
try:
    provider_kit.assert_conformance(lambda: None, name="ghost-provider")
    ck("assert_conformance raises on failure", False)
except AssertionError:
    ck("assert_conformance raises on failure", True)

print(("[OK]" if not fails else "[FAIL]") + " provider-plugin: %d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
