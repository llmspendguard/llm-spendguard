"""Subscription-lane ACTIVATION surface — tell any user exactly what stands between them and their plans.

The pool ships inert until each lane's CLI exists AND is logged in, and both failures are SILENT by design
at call time (degrade to API, never break). That silence is wrong at SETUP time: `spendguard init` and
`spendguard doctor` print this status whenever advisor.executor covers a lane, and `spendguard lanes --probe`
verifies end-to-end with one tiny plan-billed prompt per lane (the definitive check — $0 on the billed axis).

Auth detection is artifact-based and HONEST about its limits (learned live 2026-07-16): the macOS keychain
item can belong to the DESKTOP app while the CLI is logged out, so keychain-only reads as 'unknown', never
'ok' — only each CLI's own credentials file (or a live probe) proves the lane.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from . import config           # _record_probe writes the probe cache through config.update_json (atomic + backed up)

# Auth artifacts per lane (named constants; tests point these at temp paths).
CLAUDE_CREDS = Path.home() / ".claude" / ".credentials.json"      # claude CLI's own login file
CODEX_AUTH = Path.home() / ".codex" / "auth.json"                 # codex CLI login (verified live)
GEMINI_OAUTH_TOKEN = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"   # current agy layout
GEMINI_CREDS = Path.home() / ".gemini" / "oauth_creds.json"       # legacy agy layout (older CLI versions)
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"              # may be the desktop app's → 'unknown' only

_PROBE_PROMPT = "Reply with exactly: OK"
# Probe with an EXPLICIT cheap tier: a probe with no --model runs on the CLI's default-model setting, which
# can be stale (live 2026-07-16: a 404 on an old sonnet snapshot) — real lane calls always pass the advisor's
# tier, so the probe must too or it reports a failure the lane would never hit.
_PROBE_TIER = {"claude-code": "haiku", "codex": None, "gemini": None, "zai-coding": None}   # None → each lane's own default model
_QUOTA_WARN_PCT = 25          # DISPLAY-ONLY: the headroom bar flags yellow below this. NOT a routing threshold (that is
#                               config-driven + evidence-based, Phase 2) — this only colours a human-facing gauge.


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
    """'ok' when agy is logged in. A successful probe is definitive (matches _claude_auth); otherwise the OAuth
    token file proves login. Its path DRIFTS across agy versions (now antigravity-cli/antigravity-oauth-token,
    older layouts oauth_creds.json), so accept ANY known artifact — pinning one that a new agy renamed is exactly
    why this reported a logged-in, working lane as '🔴 inactive — install the CLI'. Quota exhaustion is a separate
    runtime state (agy prints a reset window; `--probe` shows it), NOT a login failure — and gemini work still
    flows via the metered API either way, so a capped lane is never 'unavailable'."""
    ok, _ = _last_probe_ok("gemini")
    if ok:
        return "ok"
    return "ok" if (GEMINI_OAUTH_TOKEN.exists() or GEMINI_CREDS.exists()) else "missing"


def _zai_auth():
    """The z.ai GLM Coding Plan lane is KEY-based, not CLI-based: 'ok' iff a plan-capable key resolves. This is
    optimistic like the CLI lanes' artifact check (a key with no active plan gets an auth error at call time and
    the lane falls back to the metered API); `spendguard lanes --probe` is the definitive check."""
    from . import zai_exec
    return "ok" if zai_exec._key() else "missing"


def lanes_status():
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


def _lane_mods():
    """lane name → its executor module. The SINGLE source for 'which module runs and reports this lane', shared by
    probe() (run_prompt) and lane_headroom() (usage()), so the mapping is defined once, not copied per consumer."""
    from . import subscription_exec, codex_exec, antigravity_exec, zai_exec
    return {"claude-code": subscription_exec, "codex": codex_exec, "gemini": antigravity_exec, "zai-coding": zai_exec}


def probe():
    """Definitive activation check: ONE tiny prompt per enabled lane, straight through its CLI ($0 billed —
    plan-covered; the only spend is a few plan tokens). Returns per-lane live results."""
    mods = _lane_mods()
    res = []
    for ln in lanes_status()["lanes"]:
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
        _record_probe(ln["lane"], ok)     # persisted: the definitive auth evidence lanes_status()/doctor read back
        res.append(dict(lane=ln["lane"], ok=ok, error=r.get("error"),
                        text=(r.get("text") or "")[:40], latency=round(r.get("latency") or 0, 1)))
    return res


_HEADROOM_SNAPSHOT = "lane_headroom"      # persisted cross-process so the routing hot path reads it without a CLI call
_HEADROOM_REFRESH_HOURS_DEFAULT = 0.5     # headroom moves with usage → refresh more often than the 6h balances cache


def refresh_headroom_if_stale():
    """Re-fetch + PERSIST the lane headroom snapshot when it is older than lanes.headroom_refresh_hours (default
    0.5h; 0 disables), on the `saas sync` cadence — the same no-dedicated-scheduler pattern as prices/balances. The
    snapshot is what idle_lanes reads in the routing hot path (no CLI there), so this is how it stays fresh. Fail-open:
    an error leaves the existing snapshot in effect and is reported, never raised."""
    try:
        hours = float(os.environ.get("SPENDGUARD_HEADROOM_REFRESH_HOURS")
                      or config._cfg_get("lanes", "headroom_refresh_hours", _HEADROOM_REFRESH_HOURS_DEFAULT))
    except Exception:
        hours = float(_HEADROOM_REFRESH_HOURS_DEFAULT)
    if hours <= 0:
        return {"skipped": "headroom_refresh_hours=0"}
    snap = config.load_state(_HEADROOM_SNAPSHOT, {}) or {}
    age_h = (time.time() - float(snap.get("asof") or 0)) / 3600.0
    if snap.get("rows") and age_h < hours:
        return {"fresh": True, "age_hours": round(age_h, 2)}
    try:
        rows = lane_headroom(do_fetch=True)
        return {"refreshed": True, "lanes": len(rows)}
    except Exception as e:
        return {"error": str(e)[:120], "note": "existing headroom snapshot still in effect"}


def lane_headroom(do_fetch=True):
    """Per-lane subscription QUOTA headroom, from each executor's usage() — provider TRUTH where the plan exposes it
    (gemini/claude-code status commands; codex/zai captured from real calls), None where it does not. One dict per
    ENABLED lane: {lane, provider, remaining_pct, reset_ts, buckets, known}. known=False ⇒ the provider exposes no
    quota surface — 'UNKNOWN', which callers must treat as DISTINCT from 0% remaining (never as exhausted).

    do_fetch=True (default, the interactive/refresh path): read each lane's usage() — one cached $0 status call for
    gemini/claude-code, captured metadata for codex/zai — and PERSIST the snapshot so a later routing process reads
    it free. do_fetch=False (the routing HOT path): return ONLY the persisted snapshot (never a CLI call); [] when no
    snapshot exists yet, so routing safely falls back to its call-volume proxy rather than paying a fetch per decision."""
    from . import lane_quota
    if not do_fetch:
        snap = config.load_state(_HEADROOM_SNAPSHOT, {}) or {}
        return list(snap.get("rows") or [])
    mods = _lane_mods()
    out = []
    for ln in lanes_status()["lanes"]:
        if not ln["enabled"]:
            continue
        mod = mods.get(ln["lane"])
        buckets = None
        if mod is not None and hasattr(mod, "usage"):
            try:
                buckets = mod.usage()
            except Exception:
                buckets = None                              # a lane's quota read must never break the cross-lane view
        hr = lane_quota.bucket_headroom(buckets) or {}
        out.append({"lane": ln["lane"], "provider": ln["provider"], "remaining_pct": hr.get("remaining_pct"),
                    "reset_ts": hr.get("reset_ts"), "buckets": buckets, "known": buckets is not None})
    try:
        from . import lane_economics
        lane_economics.record_samples(out)                  # append a (pct, tokens) sample so caps self-measure over time
        abs_by = lane_economics.remaining_abs_by_lane(headroom_rows=out)   # binding ABSOLUTE tokens left, where measured
        for r in out:
            r["remaining_abs"] = abs_by.get(r["lane"])      # None until the cap is measured → score falls back to %
    except Exception:
        pass                                                # sampling is a bonus; never fail the live read over it
    try:
        config.save_state(_HEADROOM_SNAPSHOT, {"asof": time.time(), "rows": out}, loud=False)
    except Exception:
        pass                                                # persistence is a bonus; never fail the live read over it
    return out


def lane_summary_lines():
    """Doctor/init block: one line per lane. Empty list when the executor is plain `api` (nothing to say)."""
    s = lanes_status()
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
    for line in (lane_summary_lines() or ["subscription lanes: none enabled (advisor.executor = api) — set "
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
    if "--usage" in argv:                                 # per-lane PLAN QUOTA headroom (provider truth where exposed)
        import datetime
        print("\nplan quota headroom (provider truth where the plan exposes it; 'unknown' = no quota surface yet):")
        for h in lane_headroom():
            if not h["known"]:
                print(f"  {h['lane']:<12} ({h['provider']} plan): quota unknown — no status surface / no call captured yet")
                continue
            rem = int(h["remaining_pct"])
            bar = "█" * (rem // 10) + "░" * (10 - rem // 10)
            when = (" · resets " + datetime.datetime.fromtimestamp(h["reset_ts"]).strftime("%b %d %H:%M")) if h.get("reset_ts") else ""
            flag = "🟢" if rem >= _QUOTA_WARN_PCT else ("🟡" if rem > 0 else "🔴")
            print(f"  {h['lane']:<12} ({h['provider']} plan): {flag} {rem:3}% left {bar}{when}")
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
    if "--economics" in argv:                             # measured token caps · $/token · fee at risk this window
        from . import lane_economics
        print()
        print(lane_economics.format_economics())
    if "--fallback" in argv:                              # lane→metered id equivalence: a down plan must degrade, not strand
        from . import lane_catalog
        print()
        print(lane_catalog.format_lane_fallback())
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
    if "--bulk" in argv:                                  # fan a LIST of similar tasks across ALL idle lanes at once
        import json as _json
        rest = argv[argv.index("--bulk") + 1:]

        def _optval(flag):                                # value following an optional flag, or None
            return rest[rest.index(flag) + 1] if (flag in rest and rest.index(flag) + 1 < len(rest)) else None
        file_p, ck_p, out_p = _optval("--file"), _optval("--checkpoint"), _optval("--out")
        sys_p, sysfile_p = _optval("--system"), _optval("--system-file")
        tier_p = _optval("--tier")                        # capability GROUP (advisor.tiers) → confine the fan-out to it
        opt_vals = {v for v in (file_p, ck_p, out_p, sys_p, sysfile_p, tier_p) if v}  # tier value is NOT the positional intent
        pos = [a for a in rest if not a.startswith("--") and a not in opt_vals]
        intent = pos[0] if pos else None
        if not intent:
            print('usage: spendguard lanes --bulk <intent> [--file tasks.txt] [--jsonl] [--system TEXT | --system-file P] '
                  '[--tier <group>] [--refuse-billed] [--checkpoint ck.jsonl] [--out results.jsonl] [--force]')
            print('       tasks: one per line from --file/stdin (or --jsonl = one JSON-encoded task per line, for '
                  'MULTI-LINE bodies). --system = the shared instruction, sent ONCE not per task; --refuse-billed '
                  'never bills (a lane miss errors); --checkpoint resumes by CONTENT; --out writes {i,task,text,lane,model,...}.')
        else:
            src = Path(file_p).read_text() if file_p else sys.stdin.read()
            _jsonl = any(a == "--jsonl" for a in rest)    # exact flag match (rest is argv list): one JSON-encoded task
            if _jsonl:                                     # per line → MULTI-LINE bodies survive (plain-line splitting
                tasks, _bad = [], 0                        # mangles a function body — symgrep's describe corpus)
                for ln in src.splitlines():
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        t = _json.loads(ln)
                    except Exception:
                        _bad += 1                          # malformed JSONL line = a DROPPED task → count it, never silent
                        continue
                    if isinstance(t, str) and t:
                        tasks.append(t)
                    else:
                        _bad += 1                          # non-string / empty JSON value is also a dropped task
                if _bad:
                    print(f"  note: {_bad} malformed/non-string JSONL line(s) skipped (each is one undescribed task).")
            else:
                tasks = [ln.strip() for ln in src.splitlines() if ln.strip()]
            if not tasks:
                print("no tasks (empty --file / stdin).")
            else:
                from . import lane_balance
                system = Path(sysfile_p).read_text() if sysfile_p else sys_p   # --system-file wins; instruction sent ONCE
                refuse = any(a == "--refuse-billed" for a in rest)
                _force = any(a == "--force" for a in rest)
                if not ck_p:
                    print("  note: no --checkpoint — a crash won't resume; pass --checkpoint <path> for a durable run.")
                _stats = {}
                try:
                    res = lane_balance.bulk_delegate(tasks, intent, system=system, checkpoint=ck_p,
                                                     refuse_billed=refuse, stats=_stats, force=_force, tier=tier_p)
                except lane_balance.BulkResilienceRefused as _e:
                    print(f"\n  ⛔ {_e}\n  → add --checkpoint <path> and/or split into a durable run, or re-run with "
                          f"--force to override.", file=sys.stderr)
                    return
                spread, billed, errs = {}, 0, 0
                for r in res:
                    spread[r.get("lane")] = spread.get(r.get("lane"), 0) + 1
                    billed += 1 if r.get("billed") else 0
                    errs += 1 if r.get("error") else 0
                served = ", ".join(f"{k}:{v}" for k, v in sorted(spread.items(), key=lambda kv: -kv[1]))
                print(f"[bulk {intent}] {len(tasks)} tasks · resumed {_stats.get('resumed', 0)} · "
                      f"dispatched {_stats.get('dispatched', len(tasks))} · spread {served} · "
                      f"{billed} billed(API-fallback) · {errs} errored")
                if out_p:
                    with open(out_p, "w") as f:
                        for i, r in enumerate(res):
                            f.write(_json.dumps({"i": i, "task": tasks[i], **r}) + "\n")
                    print(f"  wrote {len(res)} results → {out_p}")
                else:
                    for r in res[:3]:
                        print(f"  {r.get('lane')} · {r.get('use_name')}: "
                              + ((r.get("text") or r.get("error") or "")[:120].replace("\n", " ")))
    if "--enqueue" in argv:                               # DURABLY append tasks (never blocks) — drain them later
        from . import lane_queue
        rest = argv[argv.index("--enqueue") + 1:]

        def _qval(flag):
            return rest[rest.index(flag) + 1] if (flag in rest and rest.index(flag) + 1 < len(rest)) else None
        file_p, prio_p = _qval("--file"), _qval("--priority")
        opt_vals = {v for v in (file_p, prio_p) if v}
        pos = [a for a in rest if not a.startswith("--") and a not in opt_vals]
        intent = pos[0] if pos else None
        if not intent:
            print('usage: spendguard lanes --enqueue <intent> [--file tasks.txt] [--priority N]   (tasks: --file or stdin)')
        else:
            src = Path(file_p).read_text() if file_p else sys.stdin.read()
            tasks = [ln.strip() for ln in src.splitlines() if ln.strip()]
            ids = lane_queue.enqueue_many(intent, tasks, priority=int(prio_p or 0))
            d = lane_queue.queue_depth()
            print(f"enqueued {len(ids)} task(s) for {intent!r} (priority {int(prio_p or 0)}) — "
                  f"queue now {d.get('pending', 0)} pending. Drain with:  spendguard lanes --drain")
    if "--drain" in argv:                                 # process the durable queue onto idle lanes ($0 plan-served)
        from . import lane_queue
        rest = argv[argv.index("--drain") + 1:]

        def _dval(flag):
            return rest[rest.index(flag) + 1] if (flag in rest and rest.index(flag) + 1 < len(rest)) else None
        batch_p, ceil_p = _dval("--batch"), _dval("--ceiling")
        forever = "--forever" in rest
        d0 = lane_queue.queue_depth()
        print(f"queue: {d0.get('pending', 0)} pending · {d0.get('leased', 0)} leased · "
              f"{d0.get('done', 0)} done · {d0.get('failed', 0)} failed")
        pg = lane_queue.purge()                            # bound the queue every cycle: archive+remove old terminal rows
        if pg.get("archived"):
            print(f"  purged {pg['archived']} old terminal row(s) → {pg.get('archive')}")
        if not d0.get("pending") and not d0.get("leased") and not forever:
            print("nothing to drain.")
        else:
            s = lane_queue.drain(batch=int(batch_p) if batch_p else None,
                                 load_ceiling=float(ceil_p) if ceil_p else None, forever=forever)
            spread = ", ".join(f"{k}:{v}" for k, v in sorted(s["by_lane"].items(), key=lambda kv: -kv[1]))
            print(f"drained: {s['ran']} ran · {s['done']} done · {s['failed']} failed · "
                  f"{s['billed']} billed(API-fallback) · lanes {spread or '—'}")
            d1 = lane_queue.queue_depth()
            print(f"queue now: {d1.get('pending', 0)} pending · {d1.get('leased', 0)} leased · "
                  f"{d1.get('done', 0)} done · {d1.get('failed', 0)} failed")
    if "--queue" in argv:                                 # just the queue depth (is anything backlogged?)
        from . import lane_queue
        d = lane_queue.queue_depth()
        print(f"lane queue: {d.get('pending', 0)} pending · {d.get('leased', 0)} leased · "
              f"{d.get('done', 0)} done · {d.get('failed', 0)} failed")
    if "--purge" in argv:                                 # archive+remove OLD terminal rows so the queue stays bounded
        from . import lane_queue
        pg = lane_queue.purge()
        if pg.get("error"):
            print(f"purge failed: {pg['error']}")
        else:
            print(f"purged {pg.get('archived', 0)} old terminal row(s)"
                  + (f" → {pg['archive']}" if pg.get("archive") else " (none old enough yet)"))
    return 0
