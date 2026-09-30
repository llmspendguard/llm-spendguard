"""Packaging guard — every RUNTIME data file that model_catalog.py reads must SHIP in the wheel.

THE DEFECT (through 0.11.3). `model_catalog.json` — the curated catalog SSOT that `model_catalog._load_records()`
reads at runtime (embed batch ceilings, curated capabilities / context / reasoning) — was NOT in
`[tool.setuptools.package-data]`; only `prices.json` + `py.typed` were. So on a `pip install`, the catalog file was
absent, `_load_records()` returned {}, and every curated accessor silently degraded to the litellm breadth / defaults.
Concretely the 0.11.3 per-provider embedding batch CLAMP was INERT for pip users (`embed_batch_ceiling` → None), with
no fallback because litellm carries no embedding batch-cap data. It worked only on editable/dev installs.

This pins that every non-.py data file physically present in the package dir is BOTH declared in package-data AND on
disk — so a new runtime data file can't be added without also shipping it. Pure: reads pyproject + the dir, no build.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "src", "spendguard")
DATA_EXT = (".json", ".yaml", ".yml", ".csv", ".data")   # runtime data lives in these; .py ships by default


def main():
    try:
        import tomllib
    except ModuleNotFoundError:
        print("SKIP test_packaging_data_files: tomllib needs Python 3.11+ (the release gate runs 3.12)")
        return 0

    fails = []

    def ck(name, cond, extra=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + (("  — " + str(extra)) if extra and not cond else ""))
        if not cond:
            fails.append(name)

    with open(os.path.join(REPO, "pyproject.toml"), "rb") as fh:
        pp = tomllib.load(fh)
    shipped = set(((pp.get("tool", {}).get("setuptools", {}).get("package-data", {}) or {}).get("spendguard", [])))
    on_disk = sorted(f for f in os.listdir(PKG) if f.endswith(DATA_EXT))

    ck("the package dir actually has data files to ship (sanity)", bool(on_disk), on_disk)
    ck("model_catalog.json is declared in package-data (the 0.11.3 gap — clamp was inert on pip without it)",
       "model_catalog.json" in shipped, sorted(shipped))
    ck("prices.json is declared in package-data", "prices.json" in shipped, sorted(shipped))
    for f in on_disk:
        ck(f"every runtime data file ships: {f}", f in shipped, f"package-data={sorted(shipped)}")

    print(f"\n{'[FAIL]' if fails else 'OK'} test_packaging_data_files: {len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
