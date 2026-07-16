"""Subscription-lane ACTIVATION surface — tell any user exactly what stands between them and their plans.

The pool ships inert until each lane's CLI exists AND is logged in, and both failures are SILENT by design
at call time (degrade to API, never break). That silence is wrong at SETUP time: `spendguard init` and
`spendguard doctor` print this status whenever advisor.executor covers a lane, and `spendguard lanes --probe`
verifies end-to-end with one tiny plan-billed prompt per lane (the definitive check — $0 on the billed axis).

Auth detection is artifact-based and HONEST about its limits (learned live 2026-07-16): the macOS keychain
item can belong to the DESKTOP app while the CLI is logged out, so keychain-only reads as 'unknown', never
'ok' — only each CLI's own credentials file (or a live probe) proves the lane.
"""
import subprocess
import sys
from pathlib import Path

# Auth artifacts per lane (named constants; tests point these at temp paths).
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"      # claude CLI's own login file
CODEX_AUTH = Path.home() / ".codex" / "auth.json"                 # codex CLI login (verified live)
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"              # may be the desktop app's → 'unknown' only

_PROBE_PROMPT = "Reply with exactly: OK"
# Probe with an EXPLICIT cheap tier: a probe with no --model runs on the CLI's default-model setting, which
# can be stale (live 2026-07-16: a 404 on an old sonnet snapshot) — real lane calls always pass the advisor's
# tier, so the probe must too or it reports a failure the lane would never hit.
_PROBE_TIER = {"claude-code": "haiku", "codex": None}


def _probe_cache_path():
    from . import config
    return config.HOME / "lanes_probe.json"


def _last_probe_ok(lane):
    """(True, iso-day) if the last recorded probe of this lane succeeded — the definitive auth evidence on
    macOS, where the claude CLI stores login in the keychain and no credentials file ever appears."""
    import json
    try:
        r = json.loads(_probe_cache_path().read_text()).get(lane) or {}
        return (bool(r.get("ok")), (r.get("ts") or "")[:10])
    except Exception:
        return (False, "")


def _record_probe(lane, ok):
    import datetime
    import json
    p = _probe_cache_path()
    try:
        d = json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        d = {}
    d[lane] = {"ok": bool(ok), "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d))
    except Exception:
        pass


def _claude_auth():
    ok, day = _last_probe_ok("claude-code")
    if ok:
        return "ok"                       # a successful live probe is the definitive evidence
    if CLAUDE_CREDS.exists():
        return "ok"
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE],
                               capture_output=True, timeout=5)
            if r.returncode == 0:
                return "unknown"          # an item exists but may be the desktop app's, not the CLI's login
        except Exception:
            pass
    return "missing"


def _codex_auth():
    return "ok" if CODEX_AUTH.exists() else "missing"


def status():
    """One dict per lane: is it enabled by advisor.executor, is its CLI on this host, does a login artifact
    exist, and the exact activation step if not. Free — no network, no model calls."""
    from . import subscription_exec, codex_exec
    from .adapters import _executor
    ex = _executor()
    out = []
    for lane, provider, mod, auth_fn, login in (
        # No static login URL exists to print: each CLI generates a ONE-TIME OAuth link when you start its
        # login (and prints it if the browser doesn't open) — the command below is the link-generator.
        ("claude-code", "anthropic", subscription_exec, _claude_auth,
         "run `claude` then `/login`, sign in with your SUBSCRIPTION account — and if it offers to use a "
         "detected ANTHROPIC_API_KEY, choose No: Yes meters every call to the API instead of your plan"),
        ("codex", "openai", codex_exec, _codex_auth,
         "run `codex` and sign in with your ChatGPT account (not an API key)"),
    ):
        cli = mod._bin()
        auth = auth_fn() if cli else "missing"
        steps = []
        if not cli:
            steps.append(f"install the {lane} CLI (then `spendguard lanes` to re-check)")
        if auth != "ok":
            steps.append(login)
        out.append(dict(lane=lane, provider=provider, enabled=ex in ("pool", lane), cli=cli, auth=auth,
                        activate=("; ".join(steps) or None)))
    return {"executor": ex, "lanes": out}


def probe():
    """Definitive activation check: ONE tiny prompt per enabled lane, straight through its CLI ($0 billed —
    plan-covered; the only spend is a few plan tokens). Returns per-lane live results."""
    from . import subscription_exec, codex_exec
    mods = {"claude-code": subscription_exec, "codex": codex_exec}
    res = []
    for ln in status()["lanes"]:
        if not ln["enabled"]:
            res.append(dict(lane=ln["lane"], skipped="not enabled by advisor.executor"))
            continue
        r = mods[ln["lane"]].run_prompt(_PROBE_PROMPT, model=_PROBE_TIER.get(ln["lane"]))
        ok = not r.get("error")
        _record_probe(ln["lane"], ok)     # persisted: the definitive auth evidence status()/doctor read back
        res.append(dict(lane=ln["lane"], ok=ok, error=r.get("error"),
                        text=(r.get("text") or "")[:40], latency=round(r.get("latency") or 0, 1)))
    return res


def summary_lines():
    """Doctor/init block: one line per lane. Empty list when the executor is plain `api` (nothing to say)."""
    s = status()
    if not any(ln["enabled"] for ln in s["lanes"]):
        return []
    lines = [f"subscription lanes (advisor.executor = {s['executor']}):"]
    for ln in s["lanes"]:
        if not ln["enabled"]:
            continue
        if ln["cli"] and ln["auth"] == "ok":
            state = f"🟢 ready ({ln['cli']})"
        elif ln["cli"] and ln["auth"] == "unknown":
            state = f"🟡 CLI found; login unverified — {ln['activate']} if unsure, or `spendguard lanes --probe`"
        else:
            state = f"🔴 inactive — {ln['activate']}"
        lines.append(f"  {ln['lane']:<12} ({ln['provider']} plan): {state}")
    lines.append("  (until a lane is active its prompts fall back to the metered API — no breakage, just billed)")
    return lines


def main(argv=None):
    argv = list(argv or [])
    for line in (summary_lines() or ["subscription lanes: none enabled (advisor.executor = api) — set "
                                     "advisor.executor to claude-code / codex / pool to use your plans"]):
        print(line)
    if "--probe" in argv:
        print("probe (one tiny plan-billed prompt per enabled lane, $0):")
        for r in probe():
            if r.get("skipped"):
                print(f"  {r['lane']:<12} skipped: {r['skipped']}")
            elif r["ok"]:
                print(f"  {r['lane']:<12} 🟢 LIVE — answered in {r['latency']}s at $0 billed")
            else:
                print(f"  {r['lane']:<12} 🔴 {r['error']}")
    return 0
