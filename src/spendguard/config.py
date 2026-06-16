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


def class_cap(cls, window):
    """Resource-class spend cap ($) — cls in {total, llm, compute}, window in {daily, monthly}. None = off.
    Order: env GATE_{CLS}_{WINDOW} (e.g. GATE_LLM_DAILY, GATE_COMPUTE_MONTHLY, GATE_TOTAL_DAILY) → nested
    config caps.{cls}.{window} → (for total only) legacy flat caps.{window}. Splitting LLM vs remote-compute vs
    a total ceiling lets you set a tight LLM sub-limit under a higher overall cap."""
    env = os.getenv(f"GATE_{cls.upper()}_{window.upper()}")
    if env is not None:
        return float(env)
    caps = _cfg().get("caps") or {}
    flat = caps.get(f"{cls}.{window}")                         # how init/config stores it: caps["llm.daily"]
    if flat is not None:
        return float(flat)
    node = caps.get(cls)                                       # nested form: caps["llm"]["daily"]
    if isinstance(node, dict) and node.get(window) is not None:
        return float(node[window])
    if cls == "total" and caps.get(window) is not None:        # legacy flat caps.daily/monthly == the total ceiling
        return float(caps[window])
    return None


def daily_cap():    return class_cap("total", "daily")          # back-compat: the total daily ceiling
def monthly_cap():  return class_cap("total", "monthly")


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


def _project_saas():
    """Repo-local SaaS overlay: nearest `.spendguard.json` found walking up from CWD (stop at $HOME / fs root).
    Lets DIFFERENT repos on one machine push to different orgs/teams (e.g. lmm→Healiom/LMM, manga2anime→its org).
    Keep it gitignored — it holds the org/team api_key. Overlays the global saas.json; env still wins."""
    import json as _json
    try:
        d = Path.cwd().resolve()
    except Exception:
        return {}
    home = Path.home().resolve()
    for _ in range(40):
        p = d / ".spendguard.json"
        try:
            if p.exists():
                return _json.loads(p.read_text())
        except Exception:
            return {}
        if d == home or d.parent == d:
            break
        d = d.parent
    return {}


def saas_config():
    """SaaS / team roll-up connection. Precedence: global ~/.spendguard/saas.json < repo-local .spendguard.json
    (so each repo can target its own org/team) < env. Secrets (api_key) stay in those gitignored files or env —
    never the repo source. The key is the identity: the server maps it to user/team/org.
    Returns: enabled(bool), url, api_key, visibility, sync_interval, contributor."""
    import json as _json
    cfg = {}
    p = HOME / "saas.json"
    try:
        if p.exists():
            cfg.update(_json.loads(p.read_text()))
    except Exception:
        pass
    for k, v in _project_saas().items():       # repo-local overlay wins over the global config
        if v is not None and v != "":
            cfg[k] = v
    for key, env in (("enabled", "SPENDGUARD_SAAS"), ("url", "SPENDGUARD_SAAS_URL"),
                     ("api_key", "SPENDGUARD_SAAS_KEY"), ("visibility", "SPENDGUARD_VISIBILITY"),
                     ("sync_interval", "SPENDGUARD_SYNC_INTERVAL"), ("contributor", "SPENDGUARD_CONTRIBUTOR"),
                     ("project", "SPENDGUARD_PROJECT")):
        v = os.environ.get(env)
        if v is not None and v != "":
            cfg[key] = v
    cfg["enabled"] = str(cfg.get("enabled", "")).lower() in ("1", "true", "yes", "y")
    cfg.setdefault("visibility", "private")
    cfg.setdefault("sync_interval", "daily")
    return cfg


def saas_path(): return HOME / "saas.json"
def saas_state_path(): return HOME / "saas_state.json"   # last_sync timestamp (not the config; written each sync)


def disabled(): return os.getenv("GATE_DISABLE") == "1" or FLAG.exists()
def allow():    return os.getenv("GATE_ALLOW") == "1"


def api_key(name):
    """Resolve an API key: os.environ first, then a CHAIN of .env files — $SPENDGUARD_ENV, ./.env (cwd), and
    SPENDGUARD_HOME/.env. The last is cwd-INDEPENDENT, so keys resolve from any repo (spendguard moved out of lmm,
    so a cwd-only ./.env silently lost the keys — financial data must not depend on which directory you ran from)."""
    k = os.environ.get(name, "")
    if k:
        return k
    candidates = []
    if os.getenv("SPENDGUARD_ENV"):
        candidates.append(Path(os.getenv("SPENDGUARD_ENV")))
    candidates.append(Path.cwd() / ".env")
    candidates.append(HOME / ".env")           # stable home (~/.spendguard/.env) — found from any directory
    for envp in candidates:
        try:
            if envp.exists():
                for ln in envp.read_text().splitlines():
                    s = ln.strip()
                    if s.startswith(name + "="):
                        return s.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""
