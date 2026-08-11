"""No whole-file JSON write may bypass config.update_json, and every one must leave a `~` backup.

WHY. On 2026-08-10 ~/.spendguard/config.json went from 9KB of settings to a 26-byte probe value. There was
no backup anywhere, because there was no backup DISCIPLINE anywhere — a whole-repo invariant check found the
rule "every destructive mutation backs up first" enforced in exactly zero files. The proximate cause was one
careless write; the real cause was that 37 places could perform one and nothing made any of them safe.

Two things are checked, and the second is the one that actually protects the user:
  1. a module doing its own `.write_text(json.dumps(...))` is a NEW bypass and fails here
  2. update_json really does leave `<file>~` holding the previous version — proven by writing twice and
     reading the backup back, not by reading the source

The allowlist is deliberately small and each entry says why. It must SHRINK.
"""
import ast
import json
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "spendguard"
os.environ["SPENDGUARD_HOME"] = tempfile.mkdtemp()      # never touch the real one — that is how this started
sys.path.insert(0, str(ROOT / "src"))

failures = []


def check(label, ok, extra=""):
    print(f"  [{'OK' if ok else 'FAIL'}] {label}" + (f"  {extra}" if extra and not ok else ""))
    if not ok:
        failures.append(label)


# Only config.py is exempt wholesale — it DEFINES update_json and the atomic temp-file write beneath it.
# Every other exemption is PER SITE, declared in the source with a `# raw-write-ok: <reason>` comment on a
# line just above. A module-level allowlist would bless every FUTURE write in that file too, which is the
# same "exempt once, exempt forever" shape that let four config.json writers accumulate unnoticed.
ALLOWED_MODULES = {"config": "defines update_json and the atomic temp-file write it is built from"}
MARKER = "raw-write-ok:"

print("-- no NEW module hand-rolls a whole-file JSON write --")
offenders = {}
for f in sorted(SRC.glob("*.py")):
    if f.stem in ALLOWED_MODULES:
        continue
    body = f.read_text()
    lines = body.splitlines()
    try:
        tree = ast.parse(body)
    except SyntaxError:
        continue
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else ""
        # BOTH SPELLINGS. The first cut of this guard looked only for write_text(json.dumps(...)) and
        # therefore reported clean while json.dump(obj, open(p, "w")) sites sat untouched — a guard whose
        # coverage hole exactly matches the thing it exists to find.
        if name == "write_text":
            arg = ast.get_source_segment(body, n.args[0]) if n.args else ""
            if "json.dumps" not in (arg or ""):
                continue
        elif name == "dump" and isinstance(fn.value, ast.Name) and fn.value.id in ("json", "_j", "_json"):
            pass
        else:
            continue
        # the marker must sit within the few lines above the call, so it names THIS write
        near = "\n".join(lines[max(0, n.lineno - 7):n.lineno])
        if MARKER in near:
            continue
        offenders.setdefault(f.stem, []).append(n.lineno)
check("no module outside the allowlist writes a whole JSON file itself",
      not offenders,
      f"{offenders} — route it through config.update_json (atomic, backs up, refuses an unparseable file)")

print("\n-- update_json actually leaves a previous-version backup --")
from spendguard import config                                                          # noqa: E402
d = pathlib.Path(tempfile.mkdtemp())
target = d / "thing.json"
config.update_json(target, lambda x: {"v": 1}, reason="first")
check("no `~` before there is a previous version", not (d / "thing.json~").exists())
config.update_json(target, lambda x: {"v": 2}, reason="second")
tilde = pathlib.Path(str(target) + "~")
check("`~` exists after an overwrite", tilde.exists())
check("`~` holds the PREVIOUS version, not the new one",
      tilde.exists() and json.loads(tilde.read_text()) == {"v": 1})
check("the file itself holds the new version", json.loads(target.read_text()) == {"v": 2})

print("\n-- a backup is taken even when the caller says nothing about backups --")
t2 = d / "quiet.json"
config.update_json(t2, lambda x: {"a": 1})
config.update_json(t2, lambda x: {"a": 2})               # no keep_backups argument at all
check("the default is ON, not off", pathlib.Path(str(t2) + "~").exists())

print("\n-- adapter state goes through the same writer --")
config.save_state("guardtest", {"n": 1})
config.save_state("guardtest", {"n": 2})
check("state files get a `~` backup too", pathlib.Path(str(config.state_path("guardtest")) + "~").exists())
check("...holding the previous state",
      json.loads(pathlib.Path(str(config.state_path("guardtest")) + "~").read_text()) == {"n": 1})

print("\n-- settings are never replaced by a file we could not read; state recovers instead --")
bad = d / "settings.json"
bad.write_text("{ not json")
check("an unparseable SETTINGS file is refused", config.update_json(bad, lambda x: {"z": 1}) is None)
check("...and left exactly as it was", bad.read_text() == "{ not json")
config.state_path("guardtest").write_text("{ not json")
check("an unparseable STATE file does not wedge the adapter", config.save_state("guardtest", {"n": 3}) is True)
check("...and the damaged file is kept, not deleted",
      any(p.name.startswith("guardtest_state.json.corrupt.") for p in config.HOME.glob("*")))

print(f"\n{'[FAIL]' if failures else 'OK'} test_every_json_write_backs_up: {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
