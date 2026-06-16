"""SaaS client seam — the (zero-dependency) bridge from this local install to the FUTURE separate
spendguard **server** repo (target domain: llmseg.ai).

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
import os
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
        return False, "saas.url is unset (point it at the server, e.g. https://api.llmseg.ai)"
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
    """Who this install attributes its spend to (member_ref), so the server rolls up per user → team → org.
    Each gated venv is normally one developer, so we stamp ONE contributor on this machine's rows.
    Order: saas.contributor / $SPENDGUARD_CONTRIBUTOR → git user.email → $USER@host. Use your ORG email so it
    matches your SaaS member (the server joins member_ref to members by email)."""
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
    try:
        import getpass
        import socket
        return f"{getpass.getuser()}@{socket.gethostname()}".lower()[:128]
    except Exception:
        return ""


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
    # (e.g. animepipe → Manga2Anime) don't double-count the shared provider-account gap.
    if c.get("owns_account"):
        base.add("llmseg")
        base.add("unattributed")
    return base


def _row_uid(row):
    """Client-side mirror of the server's core.mjs rowUid — MUST stay byte-identical so a local row and its server
    row share one id (cross-check). key = 'v1|member_ref|project(lower)|day|provider|model|kind|channel' → sha1[:24].
    Compute from the SAME normalized values we send (project already lowercased, kind/channel already defaulted)."""
    import hashlib
    key = "|".join(["v1", row.get("member_ref") or "", str(row.get("project") or "").lower(),
                    row.get("day") or "", row.get("provider") or "", row.get("model") or "",
                    row.get("kind") or "workload", row.get("channel") or "batch"])
    return hashlib.sha1(key.encode()).hexdigest()[:24]


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


def run_commands(since=None):
    """Drain the server's command queue and run each LOCALLY (the data + context live here), then report a
    SCRUBBED result. FREE today: reconcile (provider-vs-local leak) + deterministic re-tag. The LLM-residual
    re-tag is gated/estimate-first (tag.estimate_llm_retag) and never auto-runs here."""
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
    """Push roll-up + insights. With if_due=True (cron/report), no-op unless the interval has elapsed.
    Always safe: not-connected / private / not-due all return a note instead of raising."""
    ok, reason = ready()
    if not ok:
        return {"skipped": f"not connected: {reason}"}
    if if_due:
        d, why = due()
        if not d:
            return {"skipped": why}
    try:
        from . import ledger_sync
        ledger_sync.reconcile_into_ledger(since=since)   # make the ledger reflect provider truth BEFORE pushing
    except Exception:
        pass
    out = {"rollup": push_rollup(since=since), "insights": push_insights(), "commands": run_commands(since=since)}
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
    print(f"  contributor  : {contributor() or '(unresolved)'}  (member_ref — set to your org email so it maps to your SaaS member)")
    print(f"  sync_interval: {c.get('sync_interval', 'daily')}  — {why}")
    print(f"  config file  : {config.saas_path()}")
    print(f"  status       : {'🟢 ' + reason if ok else '⚪ ' + reason}")
    print("  note         : the server is a SEPARATE repo (llmseg.ai) — this is the ready-to-connect client.")
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
    if sub == "commands":                             # drain + run server-enqueued work (reconcile / re-tag)
        print("commands:", run_commands())
        return 0
    if sub == "pull":
        scope = argv[1] if len(argv) > 1 else "team"
        try:
            r = pull_insights(scope); print(f"pulled {len(r.get('abstracts', []))} abstract(s) (scope={scope})"); return 0
        except Exception as e:
            print(f"pull failed: {e}"); return 1
    print("usage: spendguard saas [status|ping|sync [--if-due]|push [--dry]|reconcile|audit|crosscheck|commands|pull [team|org]]")
    return 1
