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

# A portable rule for any AI coding assistant (Claude Code, Cursor, etc.). `install-rule` drops it into a
# CLAUDE.md so EVERY conversation in that project automatically wires spendguard into whatever it builds.
_RULE_BEGIN = "<!-- spendguard:rule:begin -->"
_RULE_END = "<!-- spendguard:rule:end -->"
_RULE = _RULE_BEGIN + """
## LLM Cost Governance — MANDATORY (llm-spendguard)

Any code in this project that calls an LLM or embeddings API (OpenAI **or** Anthropic) MUST go through
**llm-spendguard**. This is a hard rule that OVERRIDES default behavior — never write or run ungated LLM code.

When you WRITE or RUN code that calls an LLM:
1. **Run it under a gated interpreter.** Use a venv whose `sitecustomize.py` loads the gate (or a python whose
   user-site has the spendguard `usercustomize`). Never bare/system `python3` that isn't gated. Verify with
   **`spendguard doctor`** — it must print `ENFORCING HERE: YES`.
2. **Fail closed.** Put `import spendguard; spendguard.require()` at the top of the script. It raises if the
   gate isn't actually enforcing in that interpreter, so a bypass can't run silently.
3. **Never hardcode prices.** Get $/token only from `spendguard.pricing` (or the repo's `pricing.py`).
4. **Estimate before you spend.** For any paid batch, do a SEPARATE zero-spend estimate run (count + $ est),
   confirm, then submit. Never cancel/kill a running job as cost control — completed requests still bill.
5. Prefer the **Batch API** for non-interactive work; keep a per-job cost estimate + approval for large batches.

Setup (one-time): `spendguard install-hook --venv <venv>` (or `--user --python <interp>` for system python),
then `spendguard doctor`. Kill switch: `GATE_DISABLE=1` or `spendguard off`.
""" + _RULE_END + "\n"


def install_rule(target=None, glob_=False):
    """Write the spendguard usage rule into a CLAUDE.md so EVERY AI-assistant conversation in that project
     auto-wires spendguard into whatever it builds. `--project <dir>` (default: cwd) or `--global` (~/.claude).
     Idempotent: replaces the marked block if present, else appends. Re-run after `spendguard` upgrades."""
    from pathlib import Path
    if glob_:
        path = Path.home() / ".claude" / "CLAUDE.md"
    else:
        path = Path(target or ".").expanduser().resolve() / "CLAUDE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text() if path.exists() else ""
    if _RULE_BEGIN in old and _RULE_END in old:                       # replace the existing block in place
        pre, rest = old.split(_RULE_BEGIN, 1)
        _, post = rest.split(_RULE_END, 1)
        new = pre + _RULE.rstrip("\n") + post
        action = "updated"
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + _RULE
        action = "appended to" if old.strip() else "created"
    path.write_text(new)
    print(f"  ✓ {action} {path}")
    print("  every AI-assistant conversation in this project will now be told to route LLM code through spendguard.")
    return 0


def cmd_install_rule(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-rule")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--global", dest="glob_", action="store_true", help="write to ~/.claude/CLAUDE.md (all projects)")
    g.add_argument("--project", help="project dir whose CLAUDE.md to write (default: current dir)")
    a = ap.parse_args(argv)
    return install_rule(a.project, glob_=a.glob_)


def _probe(interp):
    """(version, has_sdks, enforcing|None) for an interpreter. enforcing=None if it can't even run.
    'enforcing' means the gate is ACTUALLY patched onto the OpenAI SDK in that interpreter's startup."""
    import subprocess
    try:
        ver = subprocess.run([interp, "--version"], capture_output=True, text=True, timeout=10
                             ).stdout.strip().split()[-1]
    except Exception:
        return (None, False, None)
    # "has" = the SDK actually IMPORTS (find_spec lies on arch-mismatched installs, e.g. intel pydantic on arm64).
    chk = ("has=False; enf=None\n"
           "try:\n"
           " import openai; has=True\n"
           " from openai.resources import files as of; enf=bool(getattr(of.Files.create,'_spend_gated',False))\n"
           "except Exception: pass\n"
           "if enf is None:\n"
           " try:\n"
           "  import anthropic; has=True\n"
           "  from anthropic.resources.messages import batches as ab; enf=bool(getattr(ab.Batches.create,'_spend_gated',False))\n"
           " except Exception: pass\n"
           "print(int(has),int(bool(enf)))")
    try:
        out = subprocess.run([interp, "-c", chk], capture_output=True, text=True, timeout=20).stdout.strip().split()
        has, enf = bool(int(out[0])), bool(int(out[1]))
    except Exception:
        has, enf = False, False
    return (ver, has, enf)


def coverage(extra=None):
    """Show, across EVERY python on this machine (you use 3.11/3.14/…), which can make LLM calls and which
     are actually GATED. The gate is per-interpreter, so this is how you confirm nothing is silently ungated.
     `spendguard coverage [interp_or_venv ...]`."""
    import glob, sys as _sys
    from pathlib import Path
    cands = [_sys.executable, "/usr/bin/python3"]
    cands += sorted(glob.glob("/opt/homebrew/bin/python3.*") + glob.glob("/usr/local/bin/python3.*"))
    # discover venvs SHALLOWLY under common project roots — never a recursive $HOME walk (iCloud/dataless trap)
    roots = [Path.cwd(), Path.cwd().parent, Path.home() / "Documents", Path.home() / "Documents" / "claude"]
    for root in roots:
        for pat in ("*/.venv/bin/python", ".venv/bin/python", "*/*/.venv/bin/python"):
            cands += glob.glob(str(root / pat))
    for e in (extra or []):                                       # a passed venv dir → its python
        p = os.path.join(e, "bin", "python")
        cands.append(p if os.path.exists(p) else e)
    seen, rows = set(), []
    for c in cands:
        rc = os.path.realpath(c) if os.path.exists(c) else c
        if rc in seen:
            continue
        seen.add(rc)
        ver, has, enf = _probe(c)
        if ver is None:
            continue
        rows.append((c, ver, has, enf))
    print("spendguard coverage — the gate is PER-INTERPRETER; each python/venv must be gated on its own.\n")
    print(f"  {'interpreter':<52}{'ver':<9}{'LLM SDKs':<10}{'GATED'}")
    gap = []
    for c, ver, has, enf in rows:
        mark = ("🟢 yes" if enf else "🔴 NO") if has else "— n/a"
        print(f"  {c[:51]:<52}{ver:<9}{('yes' if has else 'no'):<10}{mark}")
        if has and not enf:
            gap.append(c)
    print()
    if gap:
        print("  ⚠️ these CAN call LLMs but are NOT gated — gate each:")
        for c in gap:
            if "/.venv/" in c or c.endswith("/.venv/bin/python"):
                print(f"     spendguard install-hook --venv {c.rsplit('/bin/python',1)[0]}")
            else:
                print(f"     spendguard install-hook --user --python {c}")
        print("  (or rely on `import spendguard; spendguard.require()` at the top of the script — fail-closed in ANY interpreter.)")
    else:
        print("  ✓ every interpreter that has the LLM SDKs is gated.")
    return 2 if gap else 0


def cmd_coverage(argv=None):
    return coverage(list(argv or []))


def _site_packages(venv):
    import glob
    c = glob.glob(os.path.join(venv, "lib", "python*", "site-packages"))
    return c[0] if c else None


def _pkg_src():
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2] / "src")


def install_hook(venv=None, uninstall=False, install_pkg=True, user=False, python=None):
    """Gate every process in another interpreter:
      --venv <path>            pip-install spendguard + a sitecustomize hook (clean venv).
      --user [--python <interp>]  write a PATH-INJECTING usercustomize into that interpreter's user site —
                               NO pip, so it works on PEP668 'externally-managed' pythons (Homebrew/system).
    Closes the system-python bypass. `--uninstall` removes the hook."""
    import subprocess
    from pathlib import Path
    cross = user or python                          # user/python mode = path-injected usercustomize (no pip)
    if cross:
        target = python or __import__("sys").executable
        try:
            sp = subprocess.run([target, "-c", "import site,os;os.makedirs(site.getusersitepackages(),exist_ok=True);"
                                 "print(site.getusersitepackages())"], capture_output=True, text=True, check=True).stdout.strip()
        except Exception as e:
            print(f"  ✗ couldn't resolve {target}'s user site: {e}"); return 1
        hook = os.path.join(sp, "usercustomize.py")
    else:
        venv = os.path.abspath(os.path.expanduser(venv))
        target = os.path.join(venv, "bin", "python")
        if not os.path.exists(target):
            print(f"  ✗ not a venv (no {target}). Create one: python -m venv {venv}"); return 1
        sp = _site_packages(venv)
        if not sp:
            print(f"  ✗ no site-packages under {venv}"); return 1
        hook = os.path.join(sp, "sitecustomize.py")

    if uninstall:
        if os.path.exists(hook) and "spendguard" in open(hook).read():
            os.remove(hook); print(f"  ✓ removed gate hook: {hook}")
        else:
            print(f"  (no spendguard hook at {hook})")
        return 0
    if os.path.exists(hook) and "spendguard" not in open(hook).read():
        print(f"  ✗ {hook} exists and isn't ours — not overwriting. Merge the spendguard.install() snippet manually.")
        return 1

    if cross:                                       # path-injected — no pip (PEP668-safe)
        body = _HOOK.replace("import spendguard\n        spendguard.install()",
                             f"sys.path.insert(0, {_pkg_src()!r})\n        import spendguard\n        spendguard.install()")
        open(hook, "w").write(body)
    else:
        pkg_root = str(Path(__file__).resolve().parents[2])
        if install_pkg:
            print(f"  pip install -e {pkg_root}  →  {venv}")
            r = subprocess.run([os.path.join(venv, "bin", "pip"), "install", "-e", pkg_root],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print("  ✗ pip install failed:\n" + (r.stderr or r.stdout)[-600:]); return 1
        open(hook, "w").write(_HOOK)

    v = subprocess.run([target, "-c", "from openai.resources import files as of;"
                        "print('ENFORCING' if getattr(of.Files.create,'_spend_gated',False) else 'loaded (no OpenAI SDK?)')"],
                       capture_output=True, text=True)
    print(f"  ✓ hook → {hook}")
    print(f"  verify ({target}): {v.stdout.strip() or v.stderr.strip()[-160:]}")
    # ensure setup is actually USABLE, not just installed: keys must resolve in this interpreter (the repo-move
    # break was silent because nothing checked this). Reconcile/report are dead without them.
    kk = subprocess.run([target, "-c", "from spendguard import config as c;"
                         "print('  keys: openai='+('ok' if c.api_key('OPENAI_API_KEY') else 'MISSING')+"
                         "', anthropic='+('ok' if c.api_key('ANTHROPIC_API_KEY') else 'MISSING'))"],
                        capture_output=True, text=True)
    print(kk.stdout.strip() or ("  keys: (check failed) " + kk.stderr.strip()[-120:]))
    print("  that interpreter is now gated (kill switch: GATE_DISABLE=1 or `spendguard off`).")
    print("  next: `spendguard doctor` — verifies keys + SaaS push readiness for this repo. Add keys to "
          "~/.spendguard/.env (cwd-independent) if MISSING; add a per-repo .spendguard.json to push this repo to the server.")
    return 0


def install_skills(dest=None):
    """Deploy the repo's skills/ as Claude slash-commands (copy into ~/.claude/skills/). They then work
    as /<name> in Claude Code (CLI + the VS Code extension). `spendguard install-skills`."""
    import shutil
    from pathlib import Path
    dest = Path(dest or (Path.home() / ".claude" / "skills"))
    src = Path(__file__).resolve().parents[2] / "skills"
    if not src.is_dir():
        print(f"  no skills/ dir at {src}"); return 1
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for d in sorted(src.iterdir()):
        if d.is_dir() and (d / "SKILL.md").exists():
            tgt = dest / d.name
            tgt.mkdir(exist_ok=True)
            shutil.copy2(d / "SKILL.md", tgt / "SKILL.md")
            copied.append(d.name)
    print(f"  ✓ installed {len(copied)} skill(s) → {dest}: {', '.join('/' + c for c in copied)}")
    print("  use them as slash-commands in Claude Code (CLI or the VS Code extension). `spend` is the quick status.")
    return 0


def cmd_install_skills(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-skills")
    ap.add_argument("--dest", help="skills dir (default: ~/.claude/skills)")
    a = ap.parse_args(argv)
    return install_skills(a.dest)


def cmd_install_hook(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="spendguard install-hook")
    ap.add_argument("--venv", help="path to the target virtualenv (e.g. ../slide-recon/.venv)")
    ap.add_argument("--user", action="store_true", help="gate the per-USER site of the target python "
                    "(covers `python3 …` from anywhere for that interpreter — the system-python bypass)")
    ap.add_argument("--python", help="target interpreter for --user (default: current python). "
                    "Use the system python you want to gate, e.g. /opt/homebrew/bin/python3 — "
                    "writes a path-injecting usercustomize, NO pip, so it works on PEP668-managed pythons")
    ap.add_argument("--uninstall", action="store_true", help="remove the gate hook")
    ap.add_argument("--no-pkg", action="store_true", help="skip pip install (package already present)")
    a = ap.parse_args(argv)
    if not a.venv and not a.user and not a.python:
        ap.error("give --venv <path>, or --user [--python <interp>]")
    return install_hook(a.venv, uninstall=a.uninstall, install_pkg=not a.no_pkg, user=a.user, python=a.python)


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
    if store.startswith("saas.json:"):
        v = config.saas_config().get(store[len("saas.json:"):])
        return (v, "saas.json") if v not in (None, "") else (s["default"], "default")
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
    print(f"\nfiles: {config.CONFIG_JSON} · {config.HOME / 'email.json'} · {config.saas_path()}")
    print("API keys come from env / ./.env (never written to config files).")
    return 0


def cmd_init(argv=None):
    print("spendguard setup\n")
    print("  spendguard runs FULLY STANDALONE — a local spend gate on this machine, no account needed.")
    print("  Optionally connect to a team/org dashboard (llmspendguard.com) to roll spend up across your team.\n")
    connect = "--connect" in (argv or [])
    if not connect and "--local" not in (argv or []):
        try:
            connect = input("  Connect to a team/org now? (needs an org key from your admin; or use `spendguard saas link` later) [y/N]\n  > ").strip().lower() in ("y", "yes")
        except EOFError:
            connect = False
    print("\n  Enter keeps the current/default; 'null' clears.\n")
    cfgjson = dict(config._cfg())
    ep = config.HOME / "email.json"
    sp = config.saas_path()
    email, saas = {}, {}
    if ep.exists():
        try:
            email = json.loads(ep.read_text())
        except Exception:
            pass
    if sp.exists():
        try:
            saas = json.loads(sp.read_text())
        except Exception:
            pass
    for s in config_schema.SETTINGS:
        if s["section"] == "keys" or s["store"] == "env":
            continue  # env-only (API keys, home, prices override) — instructed below, not written
        if not connect and s["section"] == "saas":
            continue  # local-only: skip all team/org connection prompts
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
        elif s["store"].startswith("saas.json:"):
            saas[s["store"][len("saas.json:"):]] = val
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.CONFIG_JSON.write_text(json.dumps(cfgjson, indent=2))
    config._cfg._cache = None  # invalidate cache
    if email:
        ep.write_text(json.dumps(email, indent=2))
    if saas:
        sp.write_text(json.dumps(saas, indent=2))
    print(f"\nwrote {config.CONFIG_JSON}" + (f" and {ep}" if email else "") + (f" and {sp}" if saas else ""))
    # Contributor identity is a MUST (it's the billable/rollup user). Materialize + show the resolved id now so it's
    # never blank/unattributed; an email here also becomes the alert target.
    try:
        from . import saas as _saas
        ident = _saas.contributor()
        if config.is_email(ident):
            print(f"Contributor: {ident}  (email — per-user roll-up, billing, AND alerts)")
        else:
            print(f"Contributor: {ident}  (auto anonymous id — attribution works; set an email via `spendguard init` for alerts)")
    except Exception:
        pass
    if connect:
        print("\nTeam/org: put your org key in saas.json (saas.api_key) if you haven't, then run `spendguard saas link` "
              "to approve in the browser + set your verified email.")
    else:
        print("\nRunning LOCAL-ONLY (no account). Connect a team anytime: `spendguard init --connect`, or "
              "`spendguard saas link` once you have an org key.")
    keys = ", ".join(s["env"] for s in config_schema.SETTINGS if s["section"] == "keys")
    print(f"Set API keys in your environment or ./.env: {keys}")
    if (cfgjson.get("budget") or {}).get("backend") == "sqlite":
        print(f"SQLite budget ledger will be created at {config.db_path()} on first charge.")
    return 0
