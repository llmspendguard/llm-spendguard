"""`spendguard config` and `spendguard init` — both generated from config_schema.SETTINGS,
so they always match the code. `config` shows resolved values + where each came from; `init`
runs the interview and writes ~/.spendguard/config.json (+ email.json), leaving API keys to env.
"""
import os, json
from . import config, config_schema


_HOOK = '''# Auto-installs the spendguard cost gate for every process in this venv (the `spendguard` package).
# Kill switches checked HERE first, before the import, so disabling works even if the package is broken:
#   GATE_DISABLE=1 (per-run)  OR  `spendguard off` (persistent flag).  Nuclear option: delete this file.
import os, sys
from pathlib import Path
_home = os.environ.get("SPENDGUARD_HOME") or str(Path.home() / ".spendguard")
if os.environ.get("GATE_DISABLE") != "1" and not os.path.exists(os.path.join(_home, "disabled")):
    try:
        import spendguard
        spendguard.install()
    except Exception as _e:  # never let the gate break a process
        sys.stderr.write(f"[sitecustomize] spendguard not installed: {_e}\\n")
'''


def _site_packages(venv):
    import glob
    c = glob.glob(os.path.join(venv, "lib", "python*", "site-packages"))
    return c[0] if c else None


def install_hook(venv, uninstall=False, install_pkg=True):
    """Gate every process in `venv`: pip-install spendguard (editable from this repo) + drop the
    sitecustomize hook. `spendguard install-hook --venv <path> [--uninstall]`."""
    import subprocess
    from pathlib import Path
    venv = os.path.abspath(os.path.expanduser(venv))
    py = os.path.join(venv, "bin", "python")
    if not os.path.exists(py):
        print(f"  ✗ not a venv (no {py}). Create one first: python -m venv {venv}")
        return 1
    sp = _site_packages(venv)
    if not sp:
        print(f"  ✗ no site-packages under {venv}")
        return 1
    hook = os.path.join(sp, "sitecustomize.py")
    if uninstall:
        if os.path.exists(hook) and "spendguard" in open(hook).read():
            os.remove(hook)
            print(f"  ✓ removed gate hook from {venv} (run `pip uninstall llm-spendguard` to remove the package)")
        else:
            print(f"  (no spendguard hook in {venv})")
        return 0
    if os.path.exists(hook) and "spendguard" not in open(hook).read():
        print(f"  ✗ {hook} exists and isn't ours — not overwriting. Merge manually:\n{_HOOK}")
        return 1
    pkg_root = str(Path(__file__).resolve().parents[2])
    if install_pkg:
        print(f"  pip install -e {pkg_root}  →  {venv}")
        r = subprocess.run([os.path.join(venv, "bin", "pip"), "install", "-e", pkg_root],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  ✗ pip install failed:\n" + (r.stderr or r.stdout)[-600:])
            return 1
    open(hook, "w").write(_HOOK)
    v = subprocess.run([py, "-c", "import os; os.environ['GATE_DISABLE']='1'; import spendguard; "
                        "print('spendguard importable; gate auto-installs on next run')"],
                       capture_output=True, text=True)
    print(f"  ✓ hook written → {hook}\n  {v.stdout.strip() or v.stderr.strip()[:160]}")
    print("  every process in this venv is now gated (kill switch: GATE_DISABLE=1 or `spendguard off`).")
    return 0


def cmd_install_hook(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-hook")
    ap.add_argument("--venv", required=True, help="path to the target virtualenv (e.g. ../slide-recon/.venv)")
    ap.add_argument("--uninstall", action="store_true", help="remove the gate hook from the venv")
    ap.add_argument("--no-pkg", action="store_true", help="skip pip install (package already present)")
    a = ap.parse_args(argv)
    return install_hook(a.venv, uninstall=a.uninstall, install_pkg=not a.no_pkg)


def _resolve(s):
    """(value, source) for one setting. env always wins; then the file; then the default."""
    env = s.get("env")
    if env and os.environ.get(env) not in (None, ""):
        return os.environ[env], f"env:{env}"
    store = s["store"]
    if store == "env":
        return s["default"], "default"
    if store.startswith("config.json:"):
        sec, key = store[len("config.json:"):].split(".", 1)
        v = (config._cfg().get(sec) or {}).get(key)
        return (v, "config.json") if v is not None else (s["default"], "default")
    if store.startswith("email.json:"):
        v = config.email_config().get(store[len("email.json:"):])
        return (v, "email.json") if v is not None else (s["default"], "default")
    return s["default"], "default"


def _coerce(ans, kind):
    if ans.lower() in ("null", "none"):
        return None
    if kind.startswith("float"):
        return float(ans)
    if kind == "bool":
        return ans.lower() in ("1", "true", "yes", "y")
    if kind.startswith("enum:"):
        opts = kind[5:].split("|")[0].split(",")
        if ans not in opts:
            print(f"  (warning: '{ans}' not in {opts})")
    return ans


def cmd_config(argv=None):
    print(f"spendguard config  (home: {config.HOME})\n")
    for sec, items in config_schema.sections().items():
        print(f"[{sec}]")
        for s in items:
            v, src = _resolve(s)
            if s["secret"] and v:
                disp = "***set***"
            elif v in (None, ""):
                disp = "(unset)"
            else:
                disp = v
            print(f"  {s['key']:<20} {str(disp):<28} {src}")
    print(f"\nfiles: {config.CONFIG_JSON} · {config.HOME / 'email.json'}")
    print("API keys come from env / ./.env (never written to config files).")
    return 0


def cmd_init(argv=None):
    print("spendguard setup — Enter keeps the current/default; 'null' clears.\n")
    cfgjson = dict(config._cfg())
    ep = config.HOME / "email.json"
    email = {}
    if ep.exists():
        try:
            email = json.loads(ep.read_text())
        except Exception:
            pass
    for s in config_schema.SETTINGS:
        if s["section"] == "keys" or s["store"] == "env":
            continue  # env-only (API keys, home, prices override) — instructed below, not written
        cur, _src = _resolve(s)
        try:
            ans = input(f"{s['section']}.{s['key']}  [{cur}]  — {s['desc']}\n  > ").strip()
        except EOFError:
            ans = ""
        if ans == "":
            continue
        try:
            val = _coerce(ans, s["kind"])
        except ValueError:
            print(f"  (couldn't parse '{ans}' as {s['kind']}; skipped)")
            continue
        if s["store"].startswith("config.json:"):
            sec, key = s["store"][len("config.json:"):].split(".", 1)
            cfgjson.setdefault(sec, {})[key] = val
        elif s["store"].startswith("email.json:"):
            email[s["store"][len("email.json:"):]] = val
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.CONFIG_JSON.write_text(json.dumps(cfgjson, indent=2))
    config._cfg._cache = None  # invalidate cache
    if email:
        ep.write_text(json.dumps(email, indent=2))
    print(f"\nwrote {config.CONFIG_JSON}" + (f" and {ep}" if email else ""))
    keys = ", ".join(s["env"] for s in config_schema.SETTINGS if s["section"] == "keys")
    print(f"Set API keys in your environment or ./.env: {keys}")
    if (cfgjson.get("budget") or {}).get("backend") == "sqlite":
        print(f"SQLite budget ledger will be created at {config.db_path()} on first charge.")
    return 0
