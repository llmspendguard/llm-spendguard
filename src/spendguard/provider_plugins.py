"""Third-party provider plugins — the `spendguard.providers` entry-point group.

Makes "add a provider" a community-sized task: a separate package (e.g. `spendguard-provider-groq`)
declares an entry point whose target is a ZERO-ARG `activate()` callable that performs its own
registration using the public seams — `adapters.register_provider()` for realtime/compare coverage,
`gate.register()` for full SDK interception, `SPENDGUARD_PRICES` for pricing rows. Installing the
package is all a user does; `spendguard.install()` discovers and activates it here.

The gate's prime invariant extends to plugins: activation is FAIL-OPEN per plugin — a broken plugin
warns once and is skipped; it can never break the gate, the other plugins, or the user's calls.
The recipe + conformance kit for authors: docs/PROVIDERS.md and `spendguard.provider_kit`.
"""
import sys

GROUP = "spendguard.providers"
_LOADED = {}   # name -> "ok" | "error: …"  (idempotence + `spendguard doctor` surface)


def load(eps=None):
    """Activate every installed provider plugin once. `eps` is injectable for tests; None = discover
    installed entry points. Returns {name: status}. Never raises."""
    if eps is None:
        try:
            from importlib.metadata import entry_points
            try:
                eps = entry_points(group=GROUP)                    # 3.10+ selectable API
            except TypeError:
                # Python 3.9: entry_points() takes no kwargs and returns {group: [ep, ...]}. Without this the
                # package printed a WARN to stderr on EVERY import under our own minimum supported version
                # (requires-python >= 3.9) — caught by the "a fresh import is silent" assertion, not by anyone
                # reading the code.
                eps = entry_points().get(GROUP, [])
        except Exception as e:                                     # discovery itself fails open
            print(f"[spendguard] WARN provider-plugin discovery failed: {e}", file=sys.stderr)
            return dict(_LOADED)
    for ep in eps:
        name = getattr(ep, "name", repr(ep))
        # Only a SUCCESS is terminal; a prior failure ("error: …") is RETRIED — an `if name in _LOADED` check
        # permanently skipped a plugin that was fixed/reinstalled in a long-lived process.
        if _LOADED.get(name) == "ok":
            continue
        try:
            activate = ep.load()
            activate()
            _LOADED[name] = "ok"
        except (Exception, SystemExit) as e:
            # Fail-open per the 'Never raises' contract: a plugin that calls sys.exit() / raises SystemExit (a
            # BaseException, which `except Exception` MISSED) would otherwise kill the process + the gate.
            # KeyboardInterrupt is intentionally NOT caught, so the user's Ctrl-C still interrupts.
            _LOADED[name] = f"error: {e}"
            print(f"[spendguard] WARN provider plugin {name!r} failed to activate (skipped): {e}", file=sys.stderr)
    return dict(_LOADED)


def loaded():
    """Status of every plugin seen this process (for doctor/receipts)."""
    return dict(_LOADED)
