"""`spendguard keys-audit [path...]` — STATIC scan for a repo .env that would shadow the keys.env SSOT.

The gap this closes (see `config.dotenv_key_shadow_report`): `spendguard doctor` resolves keys from the CURRENT
interpreter's os.environ, so a bare CLI never runs the app's `load_dotenv()` and cannot see a repo `.env` holding an
uncommented provider key that WOULD shadow the (possibly rotated) keys.env key at runtime — the warden 401. This reads
the FILES directly and flags the latent shadow before it bites. Read-only, $0, last-4 only.
"""
import json

from . import config


def _render_dotenv_audit_lines(findings, ssot_path, scanned_n, paths):
    """The human report for a dotenv shadow scan, as strings — PURE, so it is testable without the CLI. `findings` =
    config.dotenv_key_shadow_report(...); `scanned_n` = how many .env files were read; `paths` = what the user asked
    to scan. Groups by severity: 🔴 differ (a rotated-key 401 waiting to happen) · 🟡 dup."""
    where = ", ".join(paths)
    ssot_empty = next(config._iter_env_file(ssot_path), None) is None
    if not findings:
        msg = [f"🟢 no key in any scanned .env shadows the keys.env SSOT ({scanned_n} .env file(s) scanned under {where})"]
        if ssot_empty:
            msg.append("  ⚠ keys.env declares no keys here — there was nothing to compare against; set up the SSOT "
                       "first (`spendguard init`), then re-audit.")
        return msg
    differ = [f for f in findings if f["state"] == "differ"]
    dup = [f for f in findings if f["state"] == "dup"]
    lines = []
    for f in differ:
        lines.append(f"🔴 DIFFER  {f['name']} …{f['file4']}  in {f['path']}")
        lines.append(f"          would SHADOW keys.env (…{f['declared4']}) once the app runs load_dotenv() — a stale "
                     f"shadow is how a ROTATED key keeps 401ing. Remove it from the repo .env (keys live ONLY in "
                     f"keys.env), or declare {f['name']}__<profile> in keys.env + key_profile in .spendguard.json.")
    for f in dup:
        lines.append(f"🟡 DUP     {f['name']} …{f['file4']}  in {f['path']}  — duplicates keys.env; harmless now, but "
                     f"move it to the SSOT (a later keys.env rotation would turn this into a silent stale shadow).")
    lines.append(f"— {len(differ)} differ · {len(dup)} dup across {scanned_n} .env file(s); SSOT {ssot_path}")
    return lines


def cmd(rest):
    """`keys-audit [path...] [--strict] [--json]`. Scans each path (a directory → its root .env-family files; a file →
    itself) for keys that would shadow a keys.env-declared key at load_dotenv() time. Exit 1 if any 'differ' (the
    dangerous shadow); with --strict, exit 1 on a 'dup' too. Default path = the current directory."""
    rest = list(rest or [])
    if "-h" in rest or "--help" in rest:
        print("usage: spendguard keys-audit [path...] [--strict] [--json]\n"
              "  Static scan of .env-family files for a provider key that would shadow the keys.env SSOT at\n"
              "  load_dotenv() time — the shadow `doctor` can't see. Read-only, $0, last-4 only.\n"
              "    path      a repo dir (scans its root .env/.env.local/…) or an explicit .env file; default: .\n"
              "    --strict  exit non-zero on a dup too, not only on a differing (shadowing) key\n"
              "    --json    machine-readable findings (last-4 only)")
        return 0
    strict = "--strict" in rest
    as_json = "--json" in rest
    paths = [a for a in rest if not a.startswith("-")] or ["."]
    scanned = config._dotenv_scan_files(paths)            # the ONE walk; the report re-walks the same set internally
    findings = config.dotenv_key_shadow_report(paths)
    if as_json:
        print(json.dumps({"ssot": str(config.KEYS_ENV), "scanned": [str(f) for f in scanned],
                          "findings": findings}, indent=2))
    else:
        for line in _render_dotenv_audit_lines(findings, config.KEYS_ENV, len(scanned), paths):
            print(line)
    has_differ = any(f["state"] == "differ" for f in findings)
    return 1 if (has_differ or (strict and findings)) else 0
