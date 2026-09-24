"""THE CANONICAL-CONCERNS GATE — the deterministic half of single-source-of-truth (docs/SINGLE_SOURCE_OF_TRUTH.md).

Every capability has ONE home; all callers use it. Enforcement is two layers, and this file is ONLY the deterministic
one: the registry is well-formed and every registered home actually EXISTS (a home renamed or removed fails here, so the
registry can never point at nothing). The OTHER half — "does this diff re-implement a concern outside its home / duplicate
an existing capability by MEANING" — is a JUDGEMENT, so it belongs to the agentic honestreview `canonical_concerns`
doctrine at write-time, NOT to a substring check here (a mechanical proxy for a judgement is exactly what the
DECISIONS-ALWAYS-AGENTIC doctrine forbids, in tests too). Correctness of the flagship concern (output_budget never below
the floor) is proven behaviourally in test_output_ceiling_never_below_floor.

Offline; pure JSON + import. No network, no model call."""
import importlib
import json
import os
import pathlib
import sys

os.environ.setdefault("SPENDGUARD_TEST_ISOLATED", "1")
os.environ.setdefault("SPENDGUARD_NO_AUTOINSTALL", "1")

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
REGISTRY = json.loads((ROOT / "docs" / "CANONICAL_CONCERNS.json").read_text())["concerns"]

fails = []
def ck(name, cond):
    print(("  [OK] " if cond else "  [FAIL] ") + name)
    if not cond:
        fails.append(name)


print("-- the registry is well-formed --")
ck("registry is non-empty", len(REGISTRY) > 0)
_required = {"concern", "home", "description", "telltale", "status"}
ck("every concern has the required fields", all(_required <= set(c) for c in REGISTRY))
_names = [c["concern"] for c in REGISTRY]
ck("concern names are unique", len(_names) == len(set(_names)))
_homes = [c["home"] for c in REGISTRY]
ck("homes are unique — one concern, one home; no two concerns share a home", len(_homes) == len(set(_homes)))
ck("status is a known value", all(c["status"] in ("enforced", "consolidating") for c in REGISTRY))

print("\n-- every registered home EXISTS (a renamed/removed home can never silently point at nothing) --")
for c in REGISTRY:
    home = c["home"]                                    # "spendguard/pricing.py::output_ceiling" or "spendguard/pricing.py"
    modpath, _, fn = home.partition("::")
    modname = modpath.replace("/", ".").removesuffix(".py")
    try:
        mod = importlib.import_module(modname)
        exists = (not fn) or hasattr(mod, fn)
        detail = "" if exists else " (module imported, symbol missing)"
    except Exception as e:
        exists, detail = False, f" (import error: {type(e).__name__}: {str(e)[:80]})"
    ck(f"home exists: {home}{detail}", exists)

print(f"\n{'[FAIL]' if fails else 'OK'} test_canonical_concerns: {len(fails)} failure(s)")
sys.exit(1 if fails else 0)
