"""SaaS client seam — the (zero-dependency) bridge from this local install to the FUTURE separate
spendguard **server** repo (target domain: llmseg.ai).

Design (decided with the user):
- **Partner, not supervisor.** Every user keeps their OWN ledger + sets their OWN caps locally. The server
  is opt-in *visibility + pooled learnings* roll-up — it NEVER pushes caps down or blocks a user.
- **The server is a SEPARATE repo.** This file is only the client: it reads the connection from
  `~/.spendguard/saas.json` (or env), and speaks a small, documented HTTP contract. Until the server
  exists, every call degrades gracefully ("not connected") instead of erroring.
- **Secrets never hit the repo.** `api_key` lives in `saas.json` (gitignored) or `SPENDGUARD_SAAS_KEY`.
- **Only SCRUBBED data leaves**, and only at the configured `visibility` (private = nothing). Reuses the
  same scrub as `share.py` (abstracts: task_class/regime/model/ratios — never $/intent/prompt text).

The HTTP contract the server repo will implement (versioned under {url}/v1):
    GET  /v1/health                      -> {"ok": true, "version": "..."}            (ping)
    POST /v1/ledger     {day_totals,...} -> {"accepted": N}                            (push spend roll-up)
    POST /v1/insights   {abstracts:[...]} -> {"accepted": N}                           (push scrubbed learnings)
    GET  /v1/insights?scope=team|org     -> {"abstracts": [...]}                       (pull pooled learnings)
All requests send `Authorization: Bearer <api_key>` and `X-Spendguard-Client: <version>`.
"""
import json
import urllib.request
import urllib.error

from . import config


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
    """Push this machine's per-day spend roll-up (NOT per-call, NOT prompts) for team/org visibility.
    Honors visibility: returns a no-op note if visibility=private."""
    c = conn()
    if c.get("visibility", "private") == "private":
        return {"skipped": "visibility=private — nothing leaves this machine"}
    from . import budget
    try:
        days = budget.by_day(since=since)
    except Exception:
        days = []
    return _request("POST", "/v1/ledger", {"team_id": c.get("team_id"), "org_id": c.get("org_id"),
                                           "visibility": c.get("visibility"), "day_totals": days})


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
    return _request("POST", "/v1/insights", {"team_id": c.get("team_id"), "org_id": c.get("org_id"),
                                             "abstracts": abstracts})


def pull_insights(scope="team"):
    """Pull pooled (scrubbed) learnings as LOW-TRUST priors needing local corroboration."""
    return _request("GET", f"/v1/insights?scope={scope}")


def status():
    c = conn()
    ok, reason = ready()
    print("spendguard SaaS (team/org roll-up) — client seam")
    print(f"  enabled    : {c.get('enabled')}")
    print(f"  url        : {c.get('url') or '(unset)'}")
    print(f"  api_key    : {'***set***' if c.get('api_key') else '(unset)'}")
    print(f"  team_id    : {c.get('team_id') or '(none)'}")
    print(f"  org_id     : {c.get('org_id') or '(none)'}")
    print(f"  visibility : {c.get('visibility', 'private')}  (private = nothing leaves this machine)")
    print(f"  config file: {config.saas_path()}")
    print(f"  status     : {'🟢 ' + reason if ok else '⚪ ' + reason}")
    print("  note       : the server is a SEPARATE repo (llmseg.ai) — this is the ready-to-connect client.")
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
    if sub == "push":
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
    print("usage: spendguard saas [status|ping|push|pull [team|org]]")
    return 1
