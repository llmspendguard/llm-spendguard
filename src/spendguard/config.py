"""Central config for spendguard — paths, knobs, key loading.

Decoupled from any host repo so the package is portable. Data (gate log, kill-switch
flag, reconcile cache) lives under SPENDGUARD_HOME (default ~/.spendguard). API keys
resolve from the environment first, then SPENDGUARD_ENV or ./.env.
"""
import os
from pathlib import Path

HOME = Path(os.getenv("SPENDGUARD_HOME") or (Path.home() / ".spendguard"))
try:
    HOME.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

FLAG = HOME / "disabled"                      # persistent kill switch (touch to disable)
LOG = HOME / "gate_log.jsonl"                 # batch-gate audit trail
ANTHROPIC_CACHE = HOME / "anthropic_usage_cache.json"
RT_LOG = HOME / "realtime_log.jsonl"          # real-time spend log (per-day-per-model rollup)


CONFIG_JSON = HOME / "config.json"             # operational (non-secret) config: caps, budget, emit


def _cfg():
    """~/.spendguard/config.json (cached). Operational settings; env vars override per-knob."""
    import json as _json
    if getattr(_cfg, "_cache", None) is None:
        c = {}
        try:
            if CONFIG_JSON.exists():
                c = _json.loads(CONFIG_JSON.read_text())
        except Exception:
            pass
        _cfg._cache = c
    return _cfg._cache


def _cfg_get(section, key, default=None):
    return (_cfg().get(section) or {}).get(key, default)


def cap():
    """Per-batch hard cap ($). env GATE_CAP → config.json caps.per_batch → 75."""
    v = os.getenv("GATE_CAP")
    return float(v) if v is not None else float(_cfg_get("caps", "per_batch", 75))


def rt_budget():
    """Cumulative real-time cap ($). env GATE_RT_BUDGET → config.json caps.realtime → 50."""
    v = os.getenv("GATE_RT_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "realtime", 50))


def daily_cap():
    v = _cfg_get("caps", "daily", None)
    return float(v) if v is not None else None


def monthly_cap():
    v = _cfg_get("caps", "monthly", None)
    return float(v) if v is not None else None


def meta_cap():
    """Separate daily $ cap for spendguard's OWN advisor LLM use (intent spendguard:*). Default $2/day."""
    v = os.getenv("GATE_META_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "meta", 2.0))


def advisor_model():
    """Model for the advisor's REASONING (insight synthesis + `optimize`). Realtime; capped by caps.meta.
    Configurable: env SPENDGUARD_ADVISOR_MODEL > config.json advisor.model > default (Opus 4.8)."""
    return os.getenv("SPENDGUARD_ADVISOR_MODEL") or _cfg_get("advisor", "model", "claude-opus-4-8")


def advisor_judge_model():
    """Model for BULK quality reconstruction / judging. Batch API; capped by caps.meta.
    Configurable: env SPENDGUARD_ADVISOR_JUDGE_MODEL > config.json advisor.judge_model > default (Haiku 4.5)."""
    return os.getenv("SPENDGUARD_ADVISOR_JUDGE_MODEL") or _cfg_get("advisor", "judge_model", "claude-haiku-4-5")


def validate_advisor():
    """Both advisor models MUST be priced in pricing.py (else the meta estimate/cap can't be computed).
    Returns a list of human-readable problems (empty = OK)."""
    from . import pricing
    problems = []
    for role, m in (("advisor.model", advisor_model()), ("advisor.judge_model", advisor_judge_model())):
        try:
            pricing.price(m)
        except Exception as e:
            problems.append(f"{role}={m!r}: {e}")
    return problems


def budget_backend():
    return _cfg_get("budget", "backend", "memory")


def db_path():
    p = _cfg_get("budget", "db_path", None)
    return p if p else str(HOME / "spend.db")


def ssl_context():
    """SSL context that works under bare venvs too (urllib otherwise can't find CA certs on macOS)."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def email_config():
    """SMTP/recipient config from ~/.spendguard/email.json, overlaid by env. Secrets stay here
    (gitignored) or in env — never in the repo."""
    import json as _json
    cfg = {}
    p = HOME / "email.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for key, env in (("host", "SPENDGUARD_SMTP_HOST"), ("port", "SPENDGUARD_SMTP_PORT"),
                     ("user", "SPENDGUARD_SMTP_USER"), ("password", "SPENDGUARD_SMTP_PASS"),
                     ("from_", "SPENDGUARD_EMAIL_FROM"), ("to", "SPENDGUARD_EMAIL_TO"),
                     ("provider", "SPENDGUARD_EMAIL_PROVIDER"), ("api_key", "SPENDGUARD_RESEND_KEY")):
        v = os.environ.get(env)
        if v:
            cfg[key] = v
    return cfg


def saas_config():
    """SaaS / team roll-up connection from ~/.spendguard/saas.json, overlaid by env. The client seam to the
    FUTURE separate server repo (llmseg.ai). Secrets (api_key) stay here (gitignored) or in env — never the repo.
    Returns a dict with: enabled(bool), url, api_key, team_id, org_id, visibility."""
    import json as _json
    cfg = {}
    p = HOME / "saas.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for key, env in (("enabled", "SPENDGUARD_SAAS"), ("url", "SPENDGUARD_SAAS_URL"),
                     ("api_key", "SPENDGUARD_SAAS_KEY"), ("team_id", "SPENDGUARD_TEAM_ID"),
                     ("org_id", "SPENDGUARD_ORG_ID"), ("visibility", "SPENDGUARD_VISIBILITY")):
        v = os.environ.get(env)
        if v is not None and v != "":
            cfg[key] = v
    cfg["enabled"] = str(cfg.get("enabled", "")).lower() in ("1", "true", "yes", "y")
    cfg.setdefault("visibility", "private")
    return cfg


def saas_path(): return HOME / "saas.json"


def disabled(): return os.getenv("GATE_DISABLE") == "1" or FLAG.exists()
def allow():    return os.getenv("GATE_ALLOW") == "1"


def api_key(name):
    """Resolve an API key: os.environ first, then SPENDGUARD_ENV or ./.env."""
    k = os.environ.get(name, "")
    if not k:
        envp = Path(os.getenv("SPENDGUARD_ENV") or (Path.cwd() / ".env"))
        try:
            if envp.exists():
                for ln in envp.read_text().splitlines():
                    if ln.startswith(name + "="):
                        k = ln.split("=", 1)[1].strip()
                        break
        except Exception:
            pass
    return k
