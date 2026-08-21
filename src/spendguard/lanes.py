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

from . import config           # _record_probe writes the probe cache through config.update_json (atomic + backed up)

# Auth artifacts per lane (named constants; tests point these at temp paths).
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"      # claude CLI's own login file
CODEX_AUTH = Path.home() / ".codex" / "auth.json"                 # codex CLI login (verified live)
GEMINI_CREDS = Path.home() / ".gemini" / "oauth_creds.json"       # Antigravity CLI (`agy`) OAuth login (verified live)
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"              # may be the desktop app's → 'unknown' only

_PROBE_PROMPT = "Reply with exactly: OK"
# Probe with an EXPLICIT cheap tier: a probe with no --model runs on the CLI's default-model setting, which
# can be stale (live 2026-07-16: a 404 on an old sonnet snapshot) — real lane calls always pass the advisor's
# tier, so the probe must too or it reports a failure the lane would never hit.
_PROBE_TIER = {"claude-code": "haiku", "codex": None, "gemini": None, "zai-coding": None}   # None → each lane's own default model


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
    p = _probe_cache_path()
    # THROUGH THE ONE WRITER. `except: d = {}` then a full rewrite meant an unreadable probe file
    # silently discarded every OTHER lane's recorded state, and the non-atomic write could truncate it.
    config.update_json(p, lambda d: d.update({lane: {
        "ok": bool(ok),
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}}),
        reason="lane-probe")


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


def _gemini_auth():
    return "ok" if GEMINI_CREDS.exists() else "missing"


def _zai_auth():
    """The z.ai GLM Coding Plan lane is KEY-based, not CLI-based: 'ok' iff a plan-capable key resolves. This is
    optimistic like the CLI lanes' artifact check (a key with no active plan gets an auth error at call time and
    the lane falls back to the metered API); `spendguard lanes --probe` is the definitive check."""
    from . import zai_exec
    return "ok" if zai_exec._key() else "missing"


def status():
    """One dict per lane: is it enabled by advisor.executor, is its CLI on this host, does a login artifact
    exist, and the exact activation step if not. Free — no network, no model calls."""
    from . import subscription_exec, codex_exec, antigravity_exec, zai_exec
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
        ("gemini", "google", antigravity_exec, _gemini_auth,
         "install the Antigravity CLI (`curl -fsSL https://antigravity.google/cli/install.sh | bash`), then run "
         "`agy` and sign in with your Google account — decline any API-key option (a key meters every call to the "
         "Gemini API instead of your Antigravity plan)"),
        ("zai-coding", "zai", zai_exec, _zai_auth,
         "add a z.ai GLM Coding Plan key to keys.env — `ZAI_CODING_API_KEY` (or your account's `ZAI_API_KEY` on "
         "an active plan); this lane is a key + endpoint, not a CLI login"),
    ):
        # A lane is CLI-based (exposes _bin → a host binary to find + a login artifact) or KEY-based (z.ai coding
        # plan: an HTTP endpoint + key, no binary). The module DECLARES which by whether it defines _bin; readiness
        # for both is auth_fn (login artifact / probe for CLI lanes, key presence for key lanes).
        has_bin = hasattr(mod, "_bin")
        cli = mod._bin() if has_bin else None
        try:
            # CLI lane: auth is only meaningful once the binary exists (no CLI ⇒ nothing to be logged into).
            # Key lane: auth IS the key check, so always run it.
            auth = auth_fn() if (cli or not has_bin) else "missing"
        except Exception as e:
            # One lane's auth probe (a login-artifact read / keychain lookup) must not abort the status of the
            # OTHER lanes. Report this lane's auth as an error and keep going — the activation step still shows.
            auth = f"error:{type(e).__name__}"
        steps = []
        if has_bin and not cli:
            steps.append(f"install the {lane} CLI (then `spendguard lanes` to re-check)")
        if auth != "ok":
            steps.append(login)
        out.append(dict(lane=lane, provider=provider, enabled=ex in ("pool", lane), cli=cli, auth=auth,
                        activate=("; ".join(steps) or None)))
    return {"executor": ex, "lanes": out}


def probe():
    """Definitive activation check: ONE tiny prompt per enabled lane, straight through its CLI ($0 billed —
    plan-covered; the only spend is a few plan tokens). Returns per-lane live results."""
    from . import subscription_exec, codex_exec, antigravity_exec, zai_exec
    mods = {"claude-code": subscription_exec, "codex": codex_exec, "gemini": antigravity_exec, "zai-coding": zai_exec}
    res = []
    for ln in status()["lanes"]:
        if not ln["enabled"]:
            res.append(dict(lane=ln["lane"], skipped="not enabled by advisor.executor"))
            continue
        mod = mods.get(ln["lane"])
        if mod is None:
            res.append(dict(lane=ln["lane"], ok=False, error="no probe runner for this lane"))
            continue
        try:
            r = mod.run_prompt(_PROBE_PROMPT, model=_PROBE_TIER.get(ln["lane"]))
        except Exception as e:
            # a lane's CLI can be missing (FileNotFoundError) or hang (TimeoutExpired); one lane's probe raising
            # must not abort the WHOLE --probe run and lose the other lanes' already-collected results.
            r = {"error": f"{type(e).__name__}: {str(e)[:100]}"}
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
        if ln["auth"] == "ok":
            state = f"🟢 ready ({ln['cli'] or ln['provider'] + ' key'})"   # CLI lanes show the binary path; key lanes show the key
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
                                     "advisor.executor to claude-code / codex / zai-coding / gemini / pool "
                                     "to use your plans"]):
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
    if "--catalog" in argv:                               # the lane model catalog: use-names · provider · reasoning · $
        from . import lane_catalog
        print()
        lane_catalog.main()
    if "--learn" in argv:                                 # what the cross-lane BANDIT has learned per intent
        from . import lane_bandit
        print()
        lane_bandit.main()
    if "--bakeoff" in argv:                               # run ONE bake-off (2 lanes $0 + a cheap judge) to seed learning
        rest = [a for a in argv[argv.index("--bakeoff") + 1:] if not a.startswith("--")]
        if len(rest) < 2:
            print('usage: spendguard lanes --bakeoff <intent> "<task>"   (needs ≥2 lanes in advisor.lane_models + a judge)')
        else:
            from . import lane_bandit
            _intent, _task = rest[0], " ".join(rest[1:])
            r = lane_bandit.run_bakeoff(_intent, _task)
            if r and r.get("text"):
                print(f"[bake-off {_intent}] winner: {r['lane']} · {r.get('use_name')} — recorded ({r.get('why')}).")
                print("  answer: " + (r["text"][:300].replace("\n", " ")))
            else:
                print("bake-off produced no winner (need ≥2 live lanes with configured models, and a judge model).")
    if "--estimate" in argv:                              # ZERO-SPEND: what would the bandit's judge cost?
        from . import lane_bandit
        e = lane_bandit.estimate_judge_cost()
        per = e["per_bakeoff_usd"]
        print("\nBandit judge-cost estimate (ZERO SPEND — the 2 lane answers per bake-off are $0, plan-served):")
        print(f"  judge model      : {e['judge_model']}")
        print(f"  per bake-off     : {('$%.4f' % per) if per is not None else 'UNPRICED judge model'}"
              f"   (≤{e['in_tok_bound']:,} in + {e['out_tok_cap']} out)")
        if per is not None:
            for n, m in e["monthly"].items():
                print(f"  {n:>5} bake-offs/mo : ${m:,.2f}")
        print("  a bake-off runs only while an intent is COLD or at rate ε — exploration is nearly free here.")
    if "--balance" in argv:                               # per-plan utilisation: which plans are hot vs idle
        from . import lane_balance
        print(lane_balance.format_utilization())
    if "--propose" in argv:                               # model PROPOSES acceptable substitutes for an intent (PENDING)
        rest = [a for a in argv[argv.index("--propose") + 1:] if not a.startswith("--")]
        if len(rest) < 2:
            print("usage: spendguard lanes --propose <intent> <primary_model>   (needs advisor.lane_models set)")
        else:
            from . import lane_balance
            res = lane_balance.propose_substitutes(rest[0], rest[1])
            print(f"proposed for {rest[0]!r} (PENDING — confirm to use): {res['acceptable'] or 'none'}")
            if res.get("rationale"):
                print("  judge:", res["rationale"][:200])
            if res["acceptable"]:
                print(f"  confirm with:  spendguard lanes --confirm {rest[0]} <substitute>")
    if "--confirm" in argv:                               # the CONFIRM-ONCE step — makes a proposed substitute usable
        rest = [a for a in argv[argv.index("--confirm") + 1:] if not a.startswith("--")]
        if len(rest) < 2:
            print("usage: spendguard lanes --confirm <intent> <substitute>")
        else:
            from . import lane_balance
            print(f"confirmed for {rest[0]!r}: {lane_balance.confirm_substitute(rest[0], rest[1])}")
    if "--delegate" in argv:                              # offload ONE task to the cheapest viable idle lane ($0)
        rest = argv[argv.index("--delegate") + 1:]
        task = " ".join(a for a in rest if not a.startswith("--")).strip()
        if not task:
            print('usage: spendguard lanes --delegate "<task>"   (needs advisor.lane_models set)')
        else:
            from . import lane_balance
            r = lane_balance.delegate(task)
            if r.get("text"):
                flag = f"BILLED ${r.get('cost')}" if r.get("billed") else "$0 on-plan"
                print(f"[delegated → {r['lane']} · {r['model']} · {flag}]\n{r['text']}")
            else:
                print(f"delegate failed: {r.get('error')}")
    return 0
