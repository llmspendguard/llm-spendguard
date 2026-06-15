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
    POST /v1/ledger     {day_totals,...} -> {"accepted": N}                            (push spend roll-up)
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


def push_rollup(since=None):
    """Push this machine's per-day spend roll-up (NOT per-call, NOT prompts). The server derives team/org
    from the key. Honors visibility: returns a no-op note if visibility=private."""
    c = conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    from . import budget
    try:
        days = budget.by_day(since=since)
    except Exception:
        days = []
    return _request("POST", "/v1/ledger", {"visibility": c.get("visibility"), "day_totals": days})


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
    return _request("POST", "/v1/insights", {"abstracts": abstracts})


def pull_insights(scope="team"):
    """Pull pooled (scrubbed) learnings as LOW-TRUST priors needing local corroboration."""
    return _request("GET", f"/v1/insights?scope={scope}")


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
    out = {"rollup": push_rollup(since=since), "insights": push_insights()}
    _set_state(last_sync=time.time())
    return out


def status():
    c = conn()
    ok, reason = ready()
    _, why = due()
    print("spendguard SaaS (team/org roll-up) — client seam")
    print(f"  enabled      : {c.get('enabled')}")
    print(f"  url          : {c.get('url') or '(unset)'}")
    print(f"  api_key      : {'***set***' if c.get('api_key') else '(unset)'}  (server maps this key to your team/org)")
    print(f"  visibility   : {c.get('visibility', 'private')}  (private = nothing leaves this machine)")
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
    if sub == "push":                                 # force a push now (ignores cadence)
        try:
            print("rollup:", push_rollup()); print("insights:", push_insights()); return 0
        except Exception as e:
            print(f"push failed: {e}"); return 1
    if sub == "pull":
        scope = argv[1] if len(argv) > 1 else "team"
        try:
            r = pull_insights(scope); print(f"pulled {len(r.get('abstracts', []))} abstract(s) (scope={scope})"); return 0
        except Exception as e:
            print(f"pull failed: {e}"); return 1
    print("usage: spendguard saas [status|ping|sync [--if-due]|push|pull [team|org]]")
    return 1
