"""SaaS client seam — the (zero-dependency) bridge from this local install to the FUTURE separate
spendguard **server** repo (target domain: llmspendguard.com).

Design (decided with the user):
- **Partner, not supervisor.** Every user keeps their OWN ledger + sets their OWN caps locally. The server
  is opt-in *visibility + pooled learnings* roll-up — it NEVER pushes caps down or blocks a user.
- **ONE key is the identity.** The client holds only a `url` + `api_key`; the SERVER maps that key to the
  user→team→org hierarchy. The client stores no team_id/org_id (less to leak, nothing to keep in sync).
- **The server is a SEPARATE repo.** This file is only the client: it reads the connection from
  `~/.spendguard/saas.json` (or env), and speaks a small, documented HTTP contract. Until the server
  exists, every call degrades gracefully ("not connected") instead of erroring.
- **Secrets never hit the repo.** `api_key` lives in `saas.json` (gitignored) or `SPENDGUARD_SAAS_KEY`.
- **Only SCRUBBED data leaves**, and only at the configured `visibility` (private = nothing). Reuses the
  same scrub as `share.py` (abstracts: task_class/regime/model/ratios — never $/intent/prompt text).
- **Cadence is configurable** (`sync_interval`: off|hourly|daily|weekly). `sync(if_due=True)` is safe to call
  from cron / the daily report and only pushes when due; `last_sync` is tracked in `saas_state.json`.

The HTTP contract the server repo will implement (versioned under {url}/v1); identity comes from the Bearer key:
    GET  /v1/health                      -> {"ok": true, "version": "..."}            (ping)
    POST /v1/ledger     {day_totals:[{day,provider,model,kind,channel,spend_micros,calls,member_ref}],...}
                                         -> {"accepted": N}                            (push spend roll-up)
        member_ref = the contributor (this install's dev — their org email) so the server rolls up per user
        → team → org. The key sets the SCOPE (where in the hierarchy); member_ref sets WHO within it.
    POST /v1/insights   {abstracts:[...]} -> {"accepted": N}                           (push scrubbed learnings)
    GET  /v1/insights?scope=team|org     -> {"abstracts": [...]}                       (pull pooled learnings)
All requests send `Authorization: Bearer <api_key>` and `X-Spendguard-Client: <version>`.
"""
import json
import time
import urllib.request
import urllib.error

from . import config

_INTERVAL_SECONDS = {"off": None, "hourly": 3600, "daily": 86400, "weekly": 604800}


def conn():
    """Resolved SaaS connection (saas.json overlaid by env). See config.saas_config()."""
    return config.saas_config()


def _client_version():
    try:
        from . import __version__
        return __version__
    except Exception:
        return "0"


def ready():
    """(ok, reason). ok only if enabled AND a url is set AND a key is set — i.e. we COULD talk to a server."""
    c = conn()
    if not c.get("enabled"):
        return False, "saas.enabled is off (set it on once the server exists)"
    if not c.get("url"):
        return False, "saas.url is unset (point it at the server, e.g. https://llmspendguard.com)"
    if not c.get("api_key"):
        return False, "saas.api_key is unset (set SPENDGUARD_SAAS_KEY or saas.json)"
    return True, "configured"


def _request(method, path, payload=None, timeout=15):
    """Speak the contract. Raises RuntimeError with a clear message if not ready or the server is unreachable.
    Returns parsed JSON on success."""
    ok, reason = ready()
    if not ok:
        raise RuntimeError(f"spendguard SaaS not connected: {reason}")
    c = conn()
    url = c["url"].rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {c['api_key']}")
    req.add_header("X-Spendguard-Client", _client_version())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=config.ssl_context()) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"server {e.code} on {method} {path}: {e.read().decode()[:200]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"server unreachable ({c['url']}): {e.reason} — the server repo may not be running yet")


def ping():
    """Liveness check against the server (GET /v1/health)."""
    return _request("GET", "/v1/health")


def contributor():
    """Who this install attributes its spend to (member_ref) — the REAL billable/rollup user, identified by the
    email each teammate sets in their repo config (one org key is shared across teammates' repos). NEVER empty:
    explicit email/string → git user.email → a stable persisted anonymous id (`usr_<hex>`). An email doubles as
    the alert target; an auto-id still gives clean per-user attribution + billing. Set your ORG email so the server
    maps you to your SaaS member + can email you alerts. (Set via `spendguard init` or saas.json `contributor`.)"""
    c = conn()
    v = (c.get("contributor") or "").strip()
    if v:
        return v.lower()[:128]
    try:
        import subprocess
        e = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, timeout=2).stdout.strip()
        if e:
            return e.lower()[:128]
    except Exception:
        pass
    return config.machine_id()   # persisted usr_<hex> — never empty, no user@host leak


def contributor_ok():
    """(ok, reason) — is the contributor identity adequate for the CURRENT SaaS mode?

    Only matters when enabled AND visibility != private (i.e. per-user spend is actually leaving this machine).
    Then a real EMAIL is REQUIRED: the server bills + rolls up BY contributor email, so an anonymous usr_<hex>
    would create a phantom member and the org would mis-/under-attribute spend. Fix (happy path): `spendguard saas
    link` (verifies + persists your org email). Solo/local dashboards that don't need per-person mapping can opt out
    with SPENDGUARD_ALLOW_ANON=1."""
    import os
    c = conn()
    if not c.get("enabled") or c.get("visibility", "private") == "private":
        return True, "n/a (not pushing per-user data)"
    if os.environ.get("SPENDGUARD_ALLOW_ANON") == "1":
        return True, "anonymous id allowed (SPENDGUARD_ALLOW_ANON=1)"
    if config.is_email(contributor()):
        return True, "contributor email set"
    return False, ("contributor is not an email — the server bills/rolls up by email, so an anon id can't map to "
                   "your member. Fix: `spendguard saas link` (verifies it) or set saas.contributor. "
                   "Solo/local? export SPENDGUARD_ALLOW_ANON=1")


def _persist_contributor(email):
    """Write the verified contributor email to the USER-level ~/.spendguard/saas.json (applies across the user's
    repos; repo-local .spendguard.json can still override). Idempotent."""
    import json as _j
    p = config.saas_path()
    try:
        cfg = _j.loads(p.read_text()) if p.exists() else {}
    except Exception:
        cfg = {}
    cfg["contributor"] = (email or "").lower()[:128]
    try:
        config.HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(_j.dumps(cfg, indent=2))
    except Exception:
        pass


def link(open_browser=True, timeout=900):
    """Device-link this install to the org: start a link with the org key, a teammate approves in the browser
    (Clerk sign-in), then we write their VERIFIED email as the contributor — no hand-editing config. Free, no spend."""
    import time
    ok, reason = ready()
    if not ok:
        return {"error": f"not connected: {reason} — set the org key in saas.json/.spendguard.json first"}
    try:
        start = _request("POST", "/v1/link/start", {})
    except Exception as e:
        return {"error": str(e)}
    code, url, dt = start.get("code"), start.get("link_url"), start.get("device_token")
    interval = int(start.get("poll_interval", 3))
    print(f"\n  Approve this device at:\n    {url}\n  (verify the code there matches:  {code} )\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    print("  waiting for approval…  (Ctrl-C to cancel)", flush=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(interval)
        try:
            r = _request("POST", "/v1/link/poll", {"device_token": dt})
        except Exception:
            continue
        st = r.get("status")
        if st == "approved":
            em = r.get("email")
            _persist_contributor(em)
            print(f"\n  ✓ linked as {em}\n    saved to {config.saas_path()} — this is now your contributor across all repos.")
            return {"linked": em}
        if st in ("expired", "denied"):
            return {"error": f"link {st} — re-run `spendguard saas link`"}
    return {"error": "timed out waiting for approval"}


def _project_filter(c):
    """Which project(s) THIS connection pushes → so one shared ledger can serve several repos/orgs without
    cross-attributing. `projects` (list) or `project` (single) in the config; None = push all. spendguard's own
    meta ('llmseg') always rides along so each org can see + call out the small spendguard overhead."""
    ps = c.get("projects")
    base = set()
    if isinstance(ps, list) and ps:
        base = set(str(x).strip().lower() for x in ps if x)
    elif c.get("project"):
        base = {str(c["project"]).strip().lower()}
    if not base:
        return None                # no project configured → push everything
    # account-level / shared spend (the reconciled 'unattributed' gap + spendguard's own 'llmseg' meta) belongs to
    # exactly ONE org — the connection that runs reconcile. Only it opts in via owns_account, so other repos
    # (e.g. vision-pipeline → its org) don't double-count the shared provider-account gap.
    if c.get("owns_account"):
        base.add("llmseg")
        base.add("unattributed")
    return base


def _row_uid(row):
    """Client-side mirror of the server's core.mjs rowUid — MUST stay byte-identical so a local row and its server
    row share one id (cross-check). key = 'v1|member_ref|project(lower)|day|provider|model|kind|channel' → sha1[:24].
    A `team` (chat rows) is appended as '|team:<lower>' so teamless rows keep their v1 ids (back-compat)."""
    import hashlib
    parts = ["v1", row.get("member_ref") or "", str(row.get("project") or "").lower(),
             row.get("day") or "", row.get("provider") or "", row.get("model") or "",
             row.get("kind") or "workload", row.get("channel") or "batch"]
    if row.get("team"):
        parts.append("team:" + str(row["team"]).lower())
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:24]


def _rollup_rows(since=None):
    """Build the structured /v1/ledger day_totals from the local ledger, stamping this install's contributor and
    the project tag, and filtering to the project(s) this connection owns. Maps the local `kind`
    (batch|realtime|meta) to the server's kind (workload|meta) + channel (batch|realtime) and $ → micros.
    Pure (no network) so it can be tested + dry-run."""
    from . import budget
    try:
        raw = budget.by_dims(since=since)
    except Exception:
        raw = []
    ref = contributor()
    flt = _project_filter(conn())
    out = []
    for r in raw:
        proj = (r.get("project") or "").lower()
        if flt is not None and proj not in flt:
            continue                      # not this connection's project — don't cross-attribute
        k = (r.get("kind") or "workload").lower()
        row = {
            "day": r["day"], "provider": r.get("provider") or "?", "model": r.get("model") or "?",
            "kind": "meta" if k == "meta" else "workload",
            "channel": "realtime" if k == "realtime" else "batch",
            "spend_micros": round(float(r.get("cost", 0)) * 1_000_000),
            "calls": int(r.get("calls", 0)),
            "member_ref": "" if proj == "unattributed" else ref,   # reconciled gap has no known contributor
            "project": proj,
        }
        row["uid"] = _row_uid(row)        # per-row id, local↔server cross-check (server recomputes + verifies)
        out.append(row)
    return out


def _guarded_rows(since=None):
    """Per (day, project, source) cumulant SUMS of guarded spend (cache/block/cascade/…), filtered to this
    connection's project(s). Cumulants add → the server rolls up to any scope and recovers the distribution."""
    from . import guard
    try:
        rows = guard.by_dims_guarded(since=since)
    except Exception:
        return []
    c = conn()
    ps = c.get("projects")
    base = set()
    if isinstance(ps, list) and ps:
        base = set(str(x).strip().lower() for x in ps if x)
    elif c.get("project"):
        base = {str(c["project"]).strip().lower()}
    out = []
    for r in rows:
        proj = (r.get("project") or "").lower()
        if base and proj not in base:
            continue
        out.append({"day": r["day"], "project": proj, "source": r["source"], "n": int(r["n"]),
                    "k1": r["k1"], "k2": r["k2"], "k3": r["k3"], "k4": r["k4"]})
    return out


def push_rollup(since=None, dry=False):
    """Push this machine's per-day spend roll-up + GUARDED cumulants (NOT per-call, NOT prompts), stamped with the
    contributor so the server can roll up per user → team → org. Honors visibility: no-op note if private.
    dry=True returns the payload without sending (offline-testable)."""
    c = conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    cok, cwhy = contributor_ok()
    if not cok:
        return {"skipped": cwhy}   # don't push un-attributable rows (phantom member)
    payload = {"visibility": c.get("visibility"), "day_totals": _rollup_rows(since=since),
               "guarded_totals": _guarded_rows(since=since)}
    if dry:
        return payload
    return _request("POST", "/v1/ledger", payload)


def push_insights():
    """Push SCRUBBED insight abstracts (reuses share.py's scrub). Honors visibility."""
    c = conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    cok, cwhy = contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    try:
        from . import share
        abstracts = share.scrubbed_abstracts() if hasattr(share, "scrubbed_abstracts") else []
    except Exception:
        abstracts = []
    try:
        return _request("POST", "/v1/insights", {"abstracts": abstracts})
    except RuntimeError as e:
        # the server may not implement insights yet — don't let it break the spend roll-up sync
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/insights endpoint yet"}
        raise


def push_workdone(since=None, by="month", dry=False):
    """Push the WORK-DONE roll-up — git commit subjects + LLM batch-intent counts per period·project — so the
    dashboard reads "spent $X, here's what got done." Tier-1: deterministic + FREE (no diffs, no prompts). Honors
    visibility (private → no-op) and the connection's project filter. Monthly periods by default, to match the
    dashboard's current-month view. dry=True returns the payload without sending (offline-testable)."""
    c = conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    cok, cwhy = contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    from . import workdone
    flt = _project_filter(c)
    work = []
    for r in workdone.rollup(since=since, by=by):
        proj = (r.get("project") or "").lower()
        if flt is not None and proj not in flt:
            continue                      # not this connection's project — don't cross-attribute
        work.append({
            "period": r["period"], "project": proj,
            "active_days": int(r.get("active_days") or 0),
            "n_commits": int(r.get("n_commits") or 0),
            "n_batch_calls": int(r.get("n_batch_calls") or 0),
            "commits": [str(s)[:200] for s in (r.get("commits") or [])][:100],
            "intents": {str(k): int(v) for k, v in (r.get("intents") or {}).items()},
        })
    if not work:
        return {"skipped": "no work in range for this connection's project(s)"}
    if dry:
        return {"work": work}
    try:
        return _request("POST", "/v1/work", {"work": work})
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/work endpoint yet"}
        raise


def push_status(dry=False):
    """Push this contributor's GATE-COVERAGE + PRICING-DRIFT snapshot → the server's /v1/status. Powers the org
    'X of N seats gated' panel (PRD #3) + the price-drift flag (PRD #6). Scrubbed: a `gated` bool (does THIS
    interpreter auto-enforce the gate at startup — the honest per-seat signal, probed in a clean subprocess so the
    CLI's own install() doesn't mask it), interpreter counts, and {model, pct} drift vs OpenRouter. No paths/$.
    Honors visibility + the contributor-email requirement. Graceful if the server lacks the endpoint."""
    c = conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    cok, cwhy = contributor_ok()
    if not cok:
        return {"skipped": cwhy}
    gated, total_g, total = None, 0, 0
    try:
        from . import setup
        import sys as _sys
        _ver, has, enf = setup._probe(_sys.executable)     # clean-subprocess: does this interpreter auto-gate?
        if has:
            gated = bool(enf); total = 1; total_g = 1 if enf else 0
    except Exception:
        pass
    drift = []
    try:                                                    # free, read-only OpenRouter price drift; tolerate offline
        from . import pricing
        rows, _m, _t = pricing.cross_check_openrouter()
        for model, oi, ri, oo, ro, flag in rows:
            if flag == "DRIFT":
                base = ri or oi or 1e-9
                drift.append({"model": model, "pct": round(100 * abs((oi or 0) - (ri or 0)) / base)})
    except Exception:
        pass
    payload = {"member_ref": contributor(), "gated": gated,
               "interpreters": {"gated": total_g, "total": total}, "drift": drift[:50], "client": _client_version()}
    if dry:
        return payload
    try:
        return _request("POST", "/v1/status", payload)
    except RuntimeError as e:
        if " 404" in str(e) or " 405" in str(e):
            return {"skipped": "server has no /v1/status endpoint yet"}
        raise


def pull_insights(scope="team"):
    """Pull pooled (scrubbed) learnings as LOW-TRUST priors needing local corroboration."""
    return _request("GET", f"/v1/insights?scope={scope}")


# ── server-triggered work (PULL model): the server enqueues intents; we drain + run them locally on sync ──
def pull_commands():
    """Pending commands the server enqueued for this key's scope (reconcile / retag / review / full)."""
    return _request("GET", "/v1/commands")


def complete_command(cmd_id, result):
    """Report a command's (scrubbed) outcome and mark it done."""
    return _request("POST", "/v1/commands/complete", {"id": cmd_id, "result": result})


def pull_taxonomy():
    """GET the org's CANONICAL org→team×project taxonomy → cache locally as chat.org_taxonomy (so `chat classify`
    uses the SAME structure as the rest of the org). Returns the server payload {version, taxonomy}."""
    try:
        t = _request("GET", "/v1/taxonomy")
    except Exception as e:
        return {"error": str(e)}
    if t and t.get("taxonomy"):
        import json as _j
        p = config.CONFIG_JSON
        try:
            cfg = _j.loads(p.read_text()) if p.exists() else {}
        except Exception:
            cfg = {}
        cfg.setdefault("chat", {})["org_taxonomy"] = {"version": t.get("version", 1), **(t["taxonomy"] or {})}
        config.HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(_j.dumps(cfg, indent=2))
        config._cfg._cache = None
    return t


def push_taxonomy():
    """POST the local chat.taxonomy UP as the org canonical (curator action — version bumps server-side)."""
    tx = config._cfg_get("chat", "taxonomy", None)
    if not tx:
        return {"skipped": "no local chat.taxonomy to push (run `spendguard chat discover --run` first)"}
    return _request("POST", "/v1/taxonomy", {"taxonomy": tx, "member_ref": contributor()})


def run_commands(since=None):
    """Drain the server's command queue and run each LOCALLY (the data + context live here), then report a
    SCRUBBED result. FREE today: reconcile (provider-vs-local leak) + deterministic re-tag. The LLM-residual
    re-tag is gated/estimate-first (tag.estimate_llm_retag) and never auto-runs here. `attribute` = an ORG REQUEST
    to run the chat-attribution loop: adopt the org taxonomy + run IF the member already consented (chat.enabled);
    otherwise record the request as awaiting consent (never force-enables a member's personal-session adapter)."""
    ok, reason = ready()
    if not ok:
        return {"skipped": f"not connected: {reason}"}
    try:
        cmds = (pull_commands() or {}).get("commands", [])
    except Exception as e:
        return {"error": str(e)}
    ran = []
    for c in cmds:
        kind = c.get("kind")
        res = {}
        try:
            if kind in ("reconcile", "full"):
                from . import ledger_sync
                rec = ledger_sync.reconcile_into_ledger(since=since)   # writes provider-truth gap into the ledger
                res["coverage"] = rec["coverage"]
                res["provider_total"] = rec["provider_total"]
                res["leak_usd"] = rec["ungoverned"]                   # ungoverned = the gap surfaced
            if kind in ("retag", "full"):
                from . import tag
                res["retagged"] = tag.retag_deterministic()
                res["ambiguous"] = tag.ambiguous_count()   # remainder an LLM pass could resolve (gated, separate)
            if kind == "attribute":                        # ORG REQUEST: run the chat-attribution loop (consent-gated)
                from . import chat as _chat
                pull_taxonomy()                            # adopt the org's canonical taxonomy first
                if _chat._enabled():
                    res["loop"] = _chat.loop(run=True, quiet=True)   # member already consented → run + sync
                    res["status"] = "running"
                else:
                    _set_state(chat_request_pending=True,
                               chat_requested_by=(c.get("params") or {}).get("by") or c.get("requested_by") or "")
                    res["status"] = "awaiting_consent"     # surfaced via `spendguard chat status`; consent = `chat accept`
            if kind in ("reconcile", "retag", "full"):
                push_rollup(since=since)                    # re-push the corrected/reconciled ledger
            complete_command(c["id"], res)
            ran.append({"id": c["id"], "kind": kind, "result": res})
        except Exception as e:
            ran.append({"id": c["id"], "kind": kind, "error": str(e)})
    return {"ran": ran} if ran else {"skipped": "no pending commands"}


# ── cadence: how often we push (config saas.sync_interval), tracked in saas_state.json ──
def _state():
    try:
        return json.loads(config.saas_state_path().read_text())
    except Exception:
        return {}


def _set_state(**kw):
    s = _state(); s.update(kw)
    try:
        config.saas_state_path().write_text(json.dumps(s, indent=2))
    except Exception:
        pass


def due():
    """(is_due, reason). Due if sync_interval != off AND (never synced OR interval elapsed since last_sync)."""
    interval = conn().get("sync_interval", "daily")
    secs = _INTERVAL_SECONDS.get(interval)
    if secs is None:
        return False, "sync_interval=off (manual only)"
    last = _state().get("last_sync", 0)
    elapsed = time.time() - last
    if elapsed >= secs:
        return True, ("never synced" if not last else f"{int(elapsed)//3600}h since last sync (interval {interval})")
    return False, f"next sync in ~{int((secs - elapsed)//3600)}h (interval {interval})"


def sync(if_due=False, since=None):
    """Push spend roll-up + insights + work-done. With if_due=True (cron/report), no-op unless the interval has
    elapsed. Always safe: not-connected / private / not-due all return a note instead of raising."""
    ok, reason = ready()
    if not ok:
        return {"skipped": f"not connected: {reason}"}
    try:                                                  # record GPU instances EVERY run (cheap, free) so a frequent
        from . import resources as _r                     # scheduler captures short-lived/destroyed instances even
        _r.snapshot()                                     # when the full push isn't due yet
    except Exception:
        pass
    if if_due:
        d, why = due()
        if not d:
            return {"skipped": why}
    cok, cwhy = contributor_ok()
    if not cok:
        return {"skipped": cwhy}   # one clear message, not three skip-notes
    try:
        from . import ledger_sync
        ledger_sync.reconcile_into_ledger(since=since)   # batch provider-truth gap → ledger
        ledger_sync.reconcile_realtime(since=since)      # gate's realtime history (realtime_log) → ledger
    except Exception:
        pass
    # work-done needs no gap-fill reconcile: it's re-derived from git + the call corpus (complete, idempotent push).
    try:                                                  # remote-compute (vast.ai GPU) → same org/project as LLM
        from . import resources as _resources
        res = _resources.sync()
    except Exception as e:
        res = {"skipped": f"resources unavailable: {str(e)[:80]}"}
    out = {"rollup": push_rollup(since=since), "insights": push_insights(),
           "workdone": push_workdone(since=since), "status": push_status(),
           "resources": res, "commands": run_commands(since=since)}
    try:                                                  # claude.ai chat attribution loop (only if opted in)
        from . import chat as _chat
        out["chat"] = _chat.loop(run=True, quiet=True) if _chat._enabled() else {"skipped": "chat not enabled"}
    except Exception as e:
        out["chat"] = {"skipped": f"chat loop: {str(e)[:80]}"}
    _set_state(last_sync=time.time())
    return out


def crosscheck(since=None):
    """Cross-check the LOCAL ledger against the SERVER, row by row, via the per-row uid. GET /v1/ledger (this
    key's scope) and diff vs locally-computed rows → matched · value-drift · local-only (pushed-but-missing or
    never pushed) · server-only (stale, should be pruned). The trust layer over the sync. Free (no spend)."""
    import datetime
    since = since or datetime.date.today().replace(day=1).isoformat()
    ok, reason = ready()
    if not ok:
        return {"error": f"not connected: {reason}"}
    local = {r["uid"]: r for r in _rollup_rows(since=since)}        # LLM ledger rows
    try:                                                            # + GPU rows (best-effort; hits vast.ai)
        from . import resources
        for r in resources.sync(dry=True).get("day_totals", []):
            local[r["uid"]] = r
    except Exception:
        pass
    try:
        resp = _request("GET", "/v1/ledger?since=" + since)
    except Exception as e:
        return {"error": str(e)}
    srv = {row["uid"]: row for row in (resp.get("rows") or [])}
    matched = 0
    drift, local_only, server_only = [], [], []
    for uid, lr in local.items():
        if uid in srv:
            sm, lm = int(srv[uid].get("spend_micros") or 0), int(lr.get("spend_micros") or 0)
            if abs(sm - lm) > 1:
                drift.append({"uid": uid, "project": lr.get("project"), "day": lr.get("day"),
                              "local_usd": round(lm / 1e6, 2), "server_usd": round(sm / 1e6, 2), "version": srv[uid].get("version")})
            else:
                matched += 1
        else:
            local_only.append({"uid": uid, "project": lr.get("project"), "day": lr.get("day"),
                               "usd": round(int(lr.get("spend_micros") or 0) / 1e6, 2)})
    for uid, sr in srv.items():
        if uid not in local:
            server_only.append({"uid": uid, "project": sr.get("project"), "day": str(sr.get("day")),
                                "usd": round(int(sr.get("spend_micros") or 0) / 1e6, 2), "version": sr.get("version")})
    return {"since": since, "local_rows": len(local), "server_rows": len(srv),
            "matched": matched, "value_drift": len(drift), "local_only": len(local_only), "server_only": len(server_only),
            "in_sync": not (drift or local_only or server_only),
            "samples": {"value_drift": drift[:10], "local_only": local_only[:10], "server_only": server_only[:10]}}


def status():
    c = conn()
    ok, reason = ready()
    _, why = due()
    print("spendguard SaaS (team/org roll-up) — client seam")
    print(f"  enabled      : {c.get('enabled')}")
    print(f"  url          : {c.get('url') or '(unset)'}")
    print(f"  api_key      : {'***set***' if c.get('api_key') else '(unset)'}  (server maps this key to your team/org)")
    print(f"  visibility   : {c.get('visibility', 'private')}  (private = nothing leaves this machine)")
    cok, cwhy = contributor_ok()
    print(f"  contributor  : {'🟢' if cok else '🔴'} {contributor() or '(unresolved)'}  (member_ref — your org email maps you to your SaaS member)"
          + ("" if cok else f"\n                 ⚠ {cwhy}"))
    print(f"  sync_interval: {c.get('sync_interval', 'daily')}  — {why}")
    print(f"  config file  : {config.saas_path()}")
    print(f"  status       : {'🟢 ' + reason if ok else '⚪ ' + reason}")
    print("  note         : the server is a SEPARATE repo (llmspendguard.com) — this is the ready-to-connect client.")
    return 0


def cmd(argv=None):
    argv = argv or []
    sub = argv[0] if argv else "status"
    if sub in ("status", ""):
        return status()
    if sub in ("ping", "test"):
        ok, reason = ready()
        if not ok:
            print(f"not connected: {reason}"); return 1
        try:
            print("server health:", ping()); return 0
        except Exception as e:
            print(f"ping failed: {e}"); return 1
    if sub == "sync":                                 # respects cadence with --if-due (cron/report-safe)
        r = sync(if_due="--if-due" in argv)
        print("sync:", r)
        return 0 if "skipped" not in r else 1
    if sub == "push":                                 # force a push now (ignores cadence); --dry = print payload, no send
        if "--dry" in argv:
            print(json.dumps(push_rollup(dry=True), indent=2)); return 0
        try:
            print("rollup:", push_rollup()); print("insights:", push_insights()); return 0
        except Exception as e:
            print(f"push failed: {e}"); return 1
    if sub == "reconcile":                            # reconcile the LOCAL ledger to provider-billed truth (free)
        from . import ledger_sync
        print("reconcile:", ledger_sync.reconcile_into_ledger())
        return 0
    if sub == "audit":                                # triple-check completeness: every batch accounted (free)
        from . import ledger_sync
        import json as _j
        print(_j.dumps(ledger_sync.audit_completeness(), indent=2))
        return 0
    if sub in ("crosscheck", "verify"):               # row-by-row local↔server diff via per-row uid (free)
        import json as _j
        print(_j.dumps(crosscheck(), indent=2))
        return 0
    if sub == "link":                                 # device-link: approve in browser → verified email = contributor
        r = link(open_browser="--no-open" not in argv)
        if "error" in r:
            print("link failed:", r["error"]); return 1
        return 0
    if sub == "commands":                             # drain + run server-enqueued work (reconcile / re-tag)
        print("commands:", run_commands())
        return 0
    if sub == "pull":
        scope = argv[1] if len(argv) > 1 else "team"
        try:
            r = pull_insights(scope); print(f"pulled {len(r.get('abstracts', []))} abstract(s) (scope={scope})"); return 0
        except Exception as e:
            print(f"pull failed: {e}"); return 1
    print("usage: spendguard saas [status|ping|link|sync [--if-due]|push [--dry]|reconcile|audit|crosscheck|commands|pull [team|org]]")
    return 1
