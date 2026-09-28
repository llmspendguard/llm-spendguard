"""Cluster A (caller-intent analysis, 2026-09-28): config.update_json returns None (declines the write) when the
existing file is UNPARSEABLE and the caller left the default (required=False, quarantine_unparseable=False). ~21 callers
IGNORED that None and reported success — an audit record, a cache, a registry silently NOT written while the caller said
"recorded"/"wrote"/"ran". update_json's own docstring: "SETTINGS AND CACHES NEED OPPOSITE ANSWERS HERE, and only the
caller knows which it holds." So each caller must CHOOSE:
  · quarantine_unparseable=True  → rebuildable cache/state/history/registry/learned-facts: move a corrupt file aside
                                    (kept .corrupt) and write fresh, so the success it reports is HONEST.
  · required=True                → irreplaceable settings (CONFIG_JSON) or the hand-authored model_catalog SSOT: RAISE
                                    on a corrupt file (a human repairs it) — never silently declined nor auto-destroyed.

This guard AST-scans the files those callers live in and asserts EVERY update_json call passes one of those two
dispositions — never the silent-decline default. It cannot regress into the bug it fixes without failing here. A NEW
caller in one of these files that genuinely wants the default (it checks the None return itself) must be added to
_ALLOW_BARE with the reason, so the exemption is adjudicated, not silent.

Structural ($0, AST — parsing, not a judgement of meaning). No import of spendguard needed; reads source only.
"""
import sys, ast, pathlib

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "spendguard"

# The files whose update_json callers were fixed in cluster A. Every update_json call in these files must carry a
# disposition. (config.py is NOT here: it DEFINES update_json and its one call-site returns the value to its caller.)
_CLUSTER_A_FILES = [
    "balances.py", "calibrate.py", "catalog.py", "lane_balance.py", "refresh.py", "resources.py", "review.py",
    "saas.py", "setup.py", "submit.py", "sync.py", "sync_capabilities.py", "tier_config.py", "vendor_call.py",
]

# (file, lineno) call-sites deliberately left on the default because the caller HANDLES the None return itself.
# Empty today; an entry here is an adjudicated exemption, not a silent one.
_ALLOW_BARE = set()


def _update_json_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
            if name == "update_json":
                yield node


def _has_true_kw(call, key):
    for kw in call.keywords:
        if kw.arg == key and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def main():
    fails, checked = [], 0
    for fname in _CLUSTER_A_FILES:
        p = _SRC / fname
        if not p.exists():
            fails.append(f"{fname}: MISSING (cluster-A file list is stale — rename/retire it here)")
            continue
        tree = ast.parse(p.read_text())
        calls = list(_update_json_calls(tree))
        if not calls:
            fails.append(f"{fname}: no update_json call found — stale entry, remove it from _CLUSTER_A_FILES")
            continue
        for call in calls:
            checked += 1
            if (fname, call.lineno) in _ALLOW_BARE:
                continue
            picked = _has_true_kw(call, "quarantine_unparseable") or _has_true_kw(call, "required")
            tag = "OK" if picked else "FAIL"
            print(f"  [{tag}] {fname}:{call.lineno} update_json disposition")
            if not picked:
                fails.append(f"{fname}:{call.lineno} — update_json on the SILENT-DECLINE default (no "
                             f"quarantine_unparseable=True / required=True): a corrupt file returns None and this "
                             f"caller would report success anyway. Pick a disposition, or adjudicate in _ALLOW_BARE.")

    print(f"\nchecked {checked} update_json call(s) across {len(_CLUSTER_A_FILES)} cluster-A file(s)")
    if fails:
        print("[FAIL] test_update_json_callers_pick_a_disposition:")
        for f in fails:
            print("   -", f)
        return 1
    print("[OK] every cluster-A update_json call picks a disposition (never the silent-decline default)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
