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
KEYS_ENV = HOME / "keys.env"                    # SECRETS: LLM/compute/org keys (KEY=value lines), loaded at import


def _iter_env_file(path):
    """Yield (key, value) from a KEY=value dotenv file — tolerant of comments, blanks, `export `, and quotes.
    Never raises (financial setup must not crash on a malformed line)."""
    try:
        if not path.exists():
            return
        for ln in path.read_text().splitlines():
            s = ln.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[len("export "):].strip()
            yield k, v.strip().strip('"').strip("'")
    except Exception:
        return


def load_key_files():
    """Load the secret files (~/.spendguard/keys.env, then legacy ~/.spendguard/.env, then $SPENDGUARD_ENV) into
    os.environ, so BOTH spendguard AND the user's OWN clients — openai.OpenAI() / anthropic.Anthropic(), which
    read their key from the environment — pick the keys up after a plain `import spendguard`. A REAL environment
    variable ALWAYS wins (a set var is never overwritten) and blank placeholders are skipped, so prod / CI /
    secret-managers are never clobbered. Idempotent; fail-open (never raises at import)."""
    for p in (KEYS_ENV, HOME / ".env", *( [Path(os.environ["SPENDGUARD_ENV"])] if os.environ.get("SPENDGUARD_ENV") else [] )):
        for k, v in _iter_env_file(p):
            if k and v and k not in os.environ:          # real env wins; keys.env wins over legacy .env; blanks skipped
                os.environ[k] = v


load_key_files()      # at import — keys are in the environment before any provider client is constructed


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


_GITROOT_CACHE = {}


def git_root_project(cwd):
    """Repo name for a cwd = the git-root basename, lowercased (so a session's SUBDIR — lmm/scripts/fanout —
    collapses to the repo, `lmm`, instead of fragmenting into `fanout`). Matches how the gate tags actual-$ charges
    (budget._project). Cached per dir; returns None when cwd isn't inside a git repo (caller falls back to basename)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GITROOT_CACHE:
        return _GITROOT_CACHE[key]
    out = None
    try:
        import subprocess
        root = subprocess.run(["git", "-C", key, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=2).stdout.strip()
        if root:
            out = os.path.basename(root).strip().lower() or None
    except Exception:
        out = None
    _GITROOT_CACHE[key] = out
    return out


def cap():
    """Per-batch hard cap ($). env GATE_CAP → config.json caps.per_batch → 75."""
    v = os.getenv("GATE_CAP")
    return float(v) if v is not None else float(_cfg_get("caps", "per_batch", 75))


def rt_budget():
    """Cumulative real-time cap ($). env GATE_RT_BUDGET → config.json caps.realtime → 50."""
    v = os.getenv("GATE_RT_BUDGET")
    return float(v) if v is not None else float(_cfg_get("caps", "realtime", 50))


def _policy_cap(cls, window):
    """The org/team's SERVER-pushed cap for (cls, window): {"usd", "mode"} or None. Cached in config.json `policy`
    by `spendguard saas sync` (saas.pull_policy). mode = advisory | enforced."""
    try:
        node = ((_cfg().get("policy") or {}).get("caps") or {}).get(cls) or {}
        v = node.get(window)
        if isinstance(v, dict) and v.get("usd") is not None:
            return {"usd": float(v["usd"]), "mode": v.get("mode", "advisory")}
    except Exception:
        pass
    return None


def policy_caps():
    """The full server-pushed policy {caps:{cls:{window:{usd,mode}}}, asof, pulled_at} — for doctor/receipt to
    surface an advisory org suggestion or an enforced ceiling. Empty dict when none pulled."""
    return _cfg().get("policy") or {}


def class_cap(cls, window):
    """Resource-class spend cap ($) — cls in {total, llm, compute}, window in {daily, monthly}. None = off.
    LOCAL order: env GATE_{CLS}_{WINDOW} (e.g. GATE_LLM_DAILY) → nested config caps.{cls}.{window} → (total only)
    legacy flat caps.{window}. Then the SERVER policy (central caps): an ENFORCED org/team cap is a hard ceiling —
    effective = min(local, enforced), applied even with no local cap (local may only TIGHTEN it, never loosen). An
    ADVISORY policy cap is a suggestion only (surfaced by doctor/receipt) — it does NOT change the effective cap
    (partner, not supervisor)."""
    local = None
    env = os.getenv(f"GATE_{cls.upper()}_{window.upper()}")
    if env is not None:
        local = float(env)
    else:
        caps = _cfg().get("caps") or {}
        flat = caps.get(f"{cls}.{window}")                    # how init/config stores it: caps["llm.daily"]
        if flat is not None:
            local = float(flat)
        else:
            node = caps.get(cls)                              # nested form: caps["llm"]["daily"]
            if isinstance(node, dict) and node.get(window) is not None:
                local = float(node[window])
            elif cls == "total" and caps.get(window) is not None:   # legacy flat caps.daily/monthly == total ceiling
                local = float(caps[window])
    pol = _policy_cap(cls, window)
    if pol and pol.get("mode") == "enforced":                 # org-enforced ceiling; local may only tighten it
        return pol["usd"] if local is None else min(local, pol["usd"])
    return local


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


def recall_model():
    """Model for the AGENTIC RECALL pass (conv.classify_evidence — "is this chunk spend evidence / a cost lesson?").
    A high-volume, simple yes/no classification over the whole corpus, so default to the CHEAPEST capable model
    (gpt-5-nano, $0.05/1M in) — whole-corpus recall lands <10c (~free), which is what lets it replace the keyword
    pre-filters everywhere (incl. the old 'free' index stages) instead of preserving them. Capped by caps.meta.
    Configurable: env SPENDGUARD_RECALL_MODEL > config.json advisor.recall_model > default (gpt-5-nano)."""
    return os.getenv("SPENDGUARD_RECALL_MODEL") or _cfg_get("advisor", "recall_model", "gpt-5-nano")


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
    Lets DIFFERENT repos on one machine push to different orgs/teams (e.g. nlp-pipeline→Acme/NLP, vision-pipeline→its org).
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
    cfg.setdefault("url", "https://llmspendguard.com")   # the only roll-up destination (the hosted aggregator)
    return cfg


def identity_path(): return HOME / "identity.json"


def machine_id():
    """Stable, persisted anonymous contributor id (`usr_<hex>`) for this user/machine — the fallback identity when
    no email is set, so spend is NEVER unattributed and per-user roll-up + billing always have someone to count.
    Generated once, written to ~/.spendguard/identity.json, reused forever. (Replaces the old user@host fallback,
    which leaked the OS username + wasn't a stable id.)"""
    import json as _json
    p = identity_path()
    try:
        if p.exists():
            v = (_json.loads(p.read_text()).get("contributor") or "").strip()
            if v:
                return v
    except Exception:
        pass
    import uuid
    v = "usr_" + uuid.uuid4().hex[:12]
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"contributor": v}))
    except Exception:
        pass
    return v


def is_email(s):
    """True if the contributor string is an email (→ it can double as the alert target). Else it's an anonymous id."""
    import re
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (s or "").strip()))


def saas_path(): return HOME / "saas.json"
def saas_state_path(): return HOME / "saas_state.json"   # last_sync timestamp (not the config; written each sync)


def disabled(): return os.getenv("GATE_DISABLE") == "1" or FLAG.exists()
def allow():    return os.getenv("GATE_ALLOW") == "1"


def api_key(name):
    """Resolve an API key: os.environ first, then a CHAIN of .env files — $SPENDGUARD_ENV, ./.env (cwd), and
    SPENDGUARD_HOME/.env. The last is cwd-INDEPENDENT, so keys resolve from any repo (if spendguard lives outside a
    consumer repo, a cwd-only ./.env silently loses the keys — financial data must not depend on which directory you ran from)."""
    k = os.environ.get(name, "")
    if k:
        return k
    candidates = []
    if os.getenv("SPENDGUARD_ENV"):
        candidates.append(Path(os.getenv("SPENDGUARD_ENV")))
    candidates.append(Path.cwd() / ".env")
    candidates.append(HOME / "keys.env")       # the scaffolded secrets file (primary) — found from any directory
    candidates.append(HOME / ".env")           # legacy stable-home .env (still honored for existing installs)
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
