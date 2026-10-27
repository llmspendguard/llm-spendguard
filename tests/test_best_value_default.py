"""GUARD — the config-scoped best-value DEFAULT (advisor.default_reasoning='best-value') opts DELEGATED calls into
best-value routing, but ONLY the safe cases: a LABELLED (intent/sig), UNPINNED, non-probe call that set no
reasoning. It must NEVER override an explicit reasoning, a pinned (no_substitution) call, a probe, or an
UNLABELLED call, and it is OFF by default. The correctness risk of a default like this is a silent model swap on a
call that needed its own model — so the policy is a pure function, unit-tested here without a live call. Env wins
over config. Hermetic: isolated home; env/config toggled in-test; no network, no ledger."""
import os
import sys
import tempfile

os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp(prefix="spendguard-bvdefault-")
os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")
os.environ.pop("SPENDGUARD_DEFAULT_REASONING", None)          # start from a clean env
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _script_checks import record_script_check  # noqa: E402

from spendguard import adapters, config   # noqa: E402

fails = []


D = adapters._apply_best_value_default   # (reasoning, intent, sig, no_substitution, probe) -> reasoning

print("-- default OFF (unset) → never defaults, whatever the call --")
record_script_check(fails, "OFF: a labelled, unpinned call keeps reasoning=None", D(None, "loinc-typing", None, False, False) is None)

print("\n-- default ON via env → ONLY the safe cases become best-value --")
os.environ["SPENDGUARD_DEFAULT_REASONING"] = "best-value"
record_script_check(fails, "ON: labelled + unpinned + non-probe + reasoning None → 'best-value'",
   D(None, "loinc-typing", None, False, False) == "best-value")
record_script_check(fails, "ON: sig alone (no intent) still counts as labelled", D(None, None, "code-review", False, False) == "best-value")
record_script_check(fails, "ON: an EXPLICIT reasoning is never overridden (the caller always wins)",
   D("high", "loinc-typing", None, False, False) == "high")
record_script_check(fails, "ON: a PINNED (no_substitution) call is never defaulted", D(None, "loinc-typing", None, True, False) is None)
record_script_check(fails, "ON: a PROBE is never defaulted", D(None, "loinc-typing", None, False, True) is None)
record_script_check(fails, "ON: an UNLABELLED call (no intent/sig) is never defaulted — no prompt inference on the default path",
   D(None, None, None, False, False) is None)

print("\n-- an env value other than best-value → OFF --")
os.environ["SPENDGUARD_DEFAULT_REASONING"] = "off"
record_script_check(fails, "env='off' → not defaulted", D(None, "loinc-typing", None, False, False) is None)

print("\n-- config path (env unset): advisor.default_reasoning drives it --")
os.environ.pop("SPENDGUARD_DEFAULT_REASONING", None)
_orig = config._cfg_get
config._cfg_get = lambda s, k, d=None: "best-value" if (s, k) == ("advisor", "default_reasoning") else _orig(s, k, d)
try:
    record_script_check(fails, "config advisor.default_reasoning='best-value' (no env) → defaulted",
       D(None, "loinc-typing", None, False, False) == "best-value")
finally:
    config._cfg_get = _orig

print(f"\n{'[FAIL]' if fails else 'OK'} test_best_value_default: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
